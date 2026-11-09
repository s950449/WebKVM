"""擷取卡 (YUYV) → ffmpeg H.264 編碼 → 以存取單元 (access unit) 為單位分送。"""
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
    cmd += ["-fflags", "nobuffer", "-flags", "low_delay",
            "-f", "v4l2", "-input_format", "yuyv422",
            "-video_size", f"{cfg.width}x{cfg.height}", "-framerate", str(cfg.fps),
            "-i", device]
    if encoder == "vaapi":
        # Upload YUYV first so VAAPI VPP performs the conversion to NV12.
        cmd += ["-vf", "hwupload,scale_vaapi=format=nv12", "-c:v", "h264_vaapi", "-profile:v", "high",
                "-async_depth", "1", "-aud", "1", "-rc_mode", "VBR",
                "-b:v", f"{br}k", "-maxrate", f"{br * 3 // 2}k"]
    else:
        cmd += ["-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-x264-params", "aud=1:repeat-headers=1",
                "-b:v", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{br // 2}k"]
    cmd += ["-bf", "0", "-g", str(gop), "-flush_packets", "1", "-f", "h264", "-"]
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
                "encoder": self.encoder, "device": self.device}

    def _parser(self):
        return annexb_splitter()

    def _on_failure(self, units, msg):
        if self.encoder == "vaapi" and units == 0 and self.cfg.encoder == "auto":
            self._vaapi_failed = True
            msg += "\nVAAPI 編碼失敗，改用 libx264 重試"
        return msg
