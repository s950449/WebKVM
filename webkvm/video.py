"""擷取卡 (YUYV) → ffmpeg H.264 編碼 → 以存取單元 (access unit) 為單位分送給 WebSocket 客戶端。"""
import asyncio
import collections
import functools
import glob
import json
import logging
import os
import re
import shlex
import struct
import subprocess
import time

log = logging.getLogger(__name__)

AUD = b"\x00\x00\x01\x09"
FLAG_KEY = 0x01
CLIENT_QUEUE = 15     # 約 0.25 秒；客戶端跟不上就丟到下一個關鍵影格
IDLE_STOP = 5.0       # 最後一個觀看者離開後多久停止 ffmpeg
RETRY_DELAY = 2.0


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
        cmd += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-profile:v", "high",
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


class Client:
    __slots__ = ("queue", "need_key")

    def __init__(self):
        self.queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.need_key = True


class VideoPipeline:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clients = set()
        self.device = None
        self.encoder = None
        self.running = False
        self.last_error = None
        self._task = None
        self._proc = None
        self._stop_handle = None
        self._restart = False
        self._vaapi_failed = False
        self._t0 = time.monotonic()

    def describe(self):
        c = self.cfg
        return {"device": c.device, "resolved": self.device or resolve_device(c.device),
                "width": c.width, "height": c.height, "fps": c.fps, "bitrate": c.bitrate,
                "encoder": self.encoder, "running": self.running, "error": self.last_error}

    def subscribe(self):
        c = Client()
        self.clients.add(c)
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        elif self.running:
            c.queue.put_nowait(self._info_msg())
        return c

    def unsubscribe(self, c):
        self.clients.discard(c)
        if not self.clients and self._task and not self._stop_handle:
            self._stop_handle = asyncio.get_running_loop().call_later(IDLE_STOP, self._stop_if_idle)

    def restart(self):
        """套用新參數：結束目前的 ffmpeg，主迴圈會立刻以新設定重啟。"""
        self._vaapi_failed = False
        self.last_error = None
        for c in self.clients:
            c.need_key = True
        if self._proc and self._proc.returncode is None:
            self._restart = True
            self._proc.terminate()

    async def shutdown(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _stop_if_idle(self):
        self._stop_handle = None
        if not self.clients and self._task:
            log.info("沒有觀看者，停止擷取")
            self._task.cancel()

    def _info_msg(self):
        c = self.cfg
        return json.dumps({"t": "info", "width": c.width, "height": c.height, "fps": c.fps,
                           "encoder": self.encoder, "device": self.device})

    def _broadcast_text(self, msg):
        for c in self.clients:
            try:
                c.queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def _publish(self, au):
        key = is_keyframe(au)
        pkt = struct.pack("!BQ", FLAG_KEY if key else 0,
                          int((time.monotonic() - self._t0) * 1e6)) + au
        for c in self.clients:
            if c.need_key:
                if not key:
                    continue
                c.need_key = False
            try:
                c.queue.put_nowait(pkt)
            except asyncio.QueueFull:
                # 客戶端跟不上：清空佇列，等下一個關鍵影格重新同步
                c.need_key = True
                while not c.queue.empty():
                    c.queue.get_nowait()

    def _pick_encoder(self):
        enc = self.cfg.encoder
        if enc == "auto":
            vaapi_ok = (os.path.exists(self.cfg.vaapi_device)
                        and " h264_vaapi " in _ffmpeg_encoders()
                        and not self._vaapi_failed)
            enc = "vaapi" if vaapi_ok else "libx264"
        return enc

    async def _kill(self):
        p = self._proc
        if p and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 2)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()

    async def _run(self):
        try:
            while self.clients:
                await self._run_once()
                if self._restart:
                    self._restart = False
                    continue
                if self.clients:
                    await asyncio.sleep(RETRY_DELAY)
        except asyncio.CancelledError:
            pass
        finally:
            await self._kill()
            self.running = False
            self._proc = None

    async def _run_once(self):
        self.device = resolve_device(self.cfg.device)
        enc = self._pick_encoder()
        cmd = build_cmd(self.cfg, self.device, enc)
        log.info("啟動 ffmpeg: %s", shlex.join(cmd))
        self._proc = proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.encoder = enc
        self.running = True
        for c in self.clients:
            c.need_key = True
        self._broadcast_text(self._info_msg())

        tail = collections.deque(maxlen=15)
        err_task = asyncio.create_task(self._drain_stderr(proc, tail))
        frames = 0
        buf = bytearray()
        try:
            while chunk := await proc.stdout.read(1 << 16):
                buf += chunk
                while (i := buf.find(AUD, 5)) >= 0:
                    if buf[i - 1] == 0:
                        i -= 1
                    self._publish(bytes(buf[:i]))
                    del buf[:i]
                    frames += 1
        finally:
            await self._kill()
            await err_task
            self.running = False

        if self._restart:
            return
        msg = "\n".join(tail) or f"ffmpeg 結束 (code {proc.returncode})"
        if enc == "vaapi" and frames == 0 and self.cfg.encoder == "auto":
            self._vaapi_failed = True
            msg += "\nVAAPI 編碼失敗，改用 libx264 重試"
        log.warning("擷取中斷: %s", msg)
        self.last_error = msg
        self._broadcast_text(json.dumps({"t": "error", "msg": msg}))

    @staticmethod
    async def _drain_stderr(proc, tail):
        async for line in proc.stderr:
            s = line.decode(errors="replace").strip()
            if s:
                tail.append(s)
                log.info("ffmpeg: %s", s)
