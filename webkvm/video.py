"""擷取卡 (YUYV) → ffmpeg AV1 / H.264 編碼 → 逐影格分送。"""
import functools
import glob
import logging
import os
import re
import subprocess

from .pipeline import FFmpegPipeline

log = logging.getLogger(__name__)

AUD = b"\x00\x00\x01\x09"


def list_devices():
    out = []
    for p in sorted(glob.glob("/dev/v4l/by-id/*-video-index0")):
        name = os.path.basename(p).removeprefix("usb-").removesuffix("-video-index0")
        out.append({"path": p, "name": name})
    return out


def resolve_device(dev):
    if dev != "auto":
        return dev
    devs = list_devices()
    for d in devs:
        if "MACROSILICON" in d["name"].upper():
            return d["path"]
    return devs[0]["path"] if devs else "/dev/video0"


def list_modes(dev):
    """列出裝置支援的 YUYV 解析度與畫面更新率。"""
    try:
        out = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    modes, in_yuyv, cur = [], False, None
    for line in out.splitlines():
        if m := re.search(r"\[\d+\]: '(\w+)'", line):
            in_yuyv = m.group(1) == "YUYV"
            continue
        if not in_yuyv:
            continue
        if m := re.search(r"Size: Discrete (\d+)x(\d+)", line):
            cur = {"width": int(m.group(1)), "height": int(m.group(2)), "fps": []}
            modes.append(cur)
        elif (m := re.search(r"\(([\d.]+) fps\)", line)) and cur is not None:
            cur["fps"].append(round(float(m.group(1))))
    return modes


@functools.cache
def _ffmpeg_encoders():
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def build_cmd(cfg, device, encoder):
    gop = max(1, round(cfg.fps * cfg.gop))
    br = int(cfg.bitrate)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if encoder == "vaapi":
        cmd += ["-vaapi_device", cfg.vaapi_device]
    elif encoder == "qsv":
        cmd += ["-init_hw_device", f"qsv=qs:hw,child_device={cfg.qsv_device}",
                "-filter_hw_device", "qs"]
    cmd += ["-fflags", "nobuffer", "-flags", "low_delay",
            "-f", "v4l2", "-input_format", "yuyv422",
            "-video_size", f"{cfg.width}x{cfg.height}", "-framerate", str(cfg.fps),
            "-i", device]
    if encoder == "qsv":
        cmd += ["-vf", "format=nv12,hwupload=extra_hw_frames=16",
                "-c:v", "av1_qsv", "-preset", "veryfast", "-async_depth", "1",
                "-low_delay_brc", "1", "-look_ahead_depth", "0",
                "-b:v", f"{br}k", "-maxrate", f"{br * 3 // 2}k"]
    elif encoder == "vaapi":
        cmd += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-profile:v", "high",
                "-async_depth", "1", "-aud", "1", "-rc_mode", "VBR",
                "-b:v", f"{br}k", "-maxrate", f"{br * 3 // 2}k"]
    else:
        cmd += ["-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-x264-params", "aud=1:repeat-headers=1",
                "-b:v", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{br // 2}k"]
    cmd += ["-bf", "0", "-g", str(gop), "-flush_packets", "1",
            "-f", "ivf" if encoder == "qsv" else "h264", "-"]
    return cmd


def is_keyframe(au):
    i = au.find(b"\x00\x00\x01")
    while 0 <= i < len(au) - 3:
        t = au[i + 3] & 0x1F
        if t == 5:
            return True
        if t == 1:
            return False
        i = au.find(b"\x00\x00\x01", i + 3)
    return False


def av1_is_keyframe(frame):
    """QSV 的隨機存取影格會包含 sequence header OBU。"""
    pos = 0
    while pos < len(frame):
        header = frame[pos]
        pos += 1
        obu_type = (header >> 3) & 15
        if header & 4:
            pos += 1
        if not header & 2:
            return obu_type == 1
        size = 0
        for shift in range(0, 56, 7):
            if pos >= len(frame):
                return False
            value = frame[pos]
            pos += 1
            size |= (value & 127) << shift
            if not value & 128:
                break
        else:
            return False
        if pos + size > len(frame):
            return False
        if obu_type == 1:
            return True
        pos += size
    return False


def ivf_splitter():
    """拆除 IVF 標頭，容許任意 stdout 區塊邊界。"""
    buf = bytearray()
    header_done = False

    def feed(chunk):
        nonlocal header_done
        buf.extend(chunk)
        if not header_done:
            if len(buf) < 32:
                return []
            if buf[:4] != b"DKIF" or buf[8:12] != b"AV01":
                raise ValueError("預期 AV1 IVF 串流")
            header_size = int.from_bytes(buf[6:8], "little")
            if header_size < 32:
                raise ValueError("無效的 IVF 標頭")
            if len(buf) < header_size:
                return []
            del buf[:header_size]
            header_done = True
        out = []
        while len(buf) >= 12:
            size = int.from_bytes(buf[:4], "little")
            if len(buf) < 12 + size:
                break
            frame = bytes(buf[12:12 + size])
            del buf[:12 + size]
            out.append((frame, av1_is_keyframe(frame)))
        return out

    return feed


def annexb_splitter():
    """以 AUD 切分 Annex-B 串流；一個存取單元要等到下一個 AUD 出現才送出。"""
    buf = bytearray()

    def feed(chunk):
        buf.extend(chunk)
        out = []
        while (i := buf.find(AUD, 5)) >= 0:
            if buf[i - 1] == 0:
                i -= 1
            au = bytes(buf[:i])
            out.append((au, is_keyframe(au)))
            del buf[:i]
        return out

    return feed


class VideoPipeline(FFmpegPipeline):
    name = "video"

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.device = None
        self.encoder = None
        self._vaapi_failed = False

    def describe(self):
        c = self.cfg
        return {"device": c.device, "resolved": self.device or resolve_device(c.device),
                "width": c.width, "height": c.height, "fps": c.fps, "bitrate": c.bitrate,
                "encoder": self.encoder, "running": self.running, "error": self.last_error}

    def restart(self):
        self._vaapi_failed = False
        super().restart()

    def _pick_encoder(self):
        enc = self.cfg.encoder
        if enc == "auto":
            vaapi_ok = (os.path.exists(self.cfg.vaapi_device)
                        and " h264_vaapi " in _ffmpeg_encoders()
                        and not self._vaapi_failed)
            enc = "vaapi" if vaapi_ok else "libx264"
        return enc

    def _command(self):
        self.device = resolve_device(self.cfg.device)
        self.encoder = self._pick_encoder()
        return build_cmd(self.cfg, self.device, self.encoder)

    def _info(self):
        c = self.cfg
        return {"width": c.width, "height": c.height, "fps": c.fps,
                "encoder": self.encoder, "device": self.device,
                "codec": "av01.0.08M.08" if self.encoder == "qsv" else "h264"}

    def _parser(self):
        return ivf_splitter() if self.encoder == "qsv" else annexb_splitter()

    def _on_failure(self, units, msg):
        if self.encoder == "vaapi" and units == 0 and self.cfg.encoder == "auto":
            self._vaapi_failed = True
            msg += "\nVAAPI 編碼失敗，改用 libx264 重試"
        return msg
