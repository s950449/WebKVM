"""共用的 ffmpeg 串流管線：有觀看者時才啟動、失敗自動重試、把切好的封包分送給 WebSocket 客戶端。"""
import asyncio
import collections
import json
import logging
import shlex
import struct
import time

log = logging.getLogger(__name__)

FLAG_KEY = 0x01
CLIENT_QUEUE = 15     # 客戶端跟不上時丟棄並等下一個關鍵封包
IDLE_STOP = 5.0       # 最後一個觀看者離開後多久停止 ffmpeg
RETRY_DELAY = 2.0


class PipelineError(Exception):
    pass


class Client:
    __slots__ = ("queue", "need_key")

    def __init__(self):
        self.queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.need_key = True


class FFmpegPipeline:
    """子類別實作 _command()、_info()、_parser()，可選擇覆寫 _on_failure()。"""

    name = "stream"

    def __init__(self):
        self.clients = set()
        self.running = False
        self.last_error = None
        self._task = None
        self._proc = None
        self._stop_handle = None
        self._restart = False
        self._t0 = time.monotonic()

    # ---------- 子類別介面 ----------

    def _command(self):
        """回傳 ffmpeg 參數列；無法啟動時丟出 PipelineError。"""
        raise NotImplementedError

    def _info(self):
        raise NotImplementedError

    def _parser(self):
        """回傳 feed(bytes) -> [(payload, is_key), ...] 的函式，每次啟動 ffmpeg 都會重新建立。"""
        raise NotImplementedError

    def _on_failure(self, units, msg):
        return msg

    # ---------- 對外 API ----------

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

    # ---------- 內部 ----------

    def _stop_if_idle(self):
        self._stop_handle = None
        if not self.clients and self._task:
            log.info("%s: 沒有觀看者，停止擷取", self.name)
            self._task.cancel()

    def _info_msg(self):
        return json.dumps({"t": "info", **self._info()})

    def _broadcast_text(self, msg):
        for c in self.clients:
            try:
                c.queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def _publish(self, payload, key):
        pkt = struct.pack("!BQ", FLAG_KEY if key else 0,
                          int((time.monotonic() - self._t0) * 1e6)) + payload
        for c in self.clients:
            if c.need_key:
                if not key:
                    continue
                c.need_key = False
            try:
                c.queue.put_nowait(pkt)
            except asyncio.QueueFull:
                c.need_key = True
                while not c.queue.empty():
                    c.queue.get_nowait()

    def _fail(self, msg):
        log.warning("%s 擷取中斷: %s", self.name, msg)
        self.last_error = msg
        self._broadcast_text(json.dumps({"t": "error", "msg": msg}))

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
        try:
            cmd = self._command()
        except PipelineError as e:
            self._fail(str(e))
            return
        log.info("%s: 啟動 ffmpeg: %s", self.name, shlex.join(cmd))
        self._proc = proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.running = True
        for c in self.clients:
            c.need_key = True
        self._broadcast_text(self._info_msg())

        tail = collections.deque(maxlen=15)
        err_task = asyncio.create_task(self._drain_stderr(proc, tail))
        feed = self._parser()
        units = 0
        try:
            while chunk := await proc.stdout.read(1 << 16):
                for payload, key in feed(chunk):
                    self._publish(payload, key)
                    units += 1
        finally:
            await self._kill()
            await err_task
            self.running = False

        if self._restart:
            return
        self._fail(self._on_failure(units, "\n".join(tail) or f"ffmpeg 結束 (code {proc.returncode})"))

    async def _drain_stderr(self, proc, tail):
        async for line in proc.stderr:
            s = line.decode(errors="replace").strip()
            if s:
                tail.append(s)
                log.info("%s ffmpeg: %s", self.name, s)
