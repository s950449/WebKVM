"""CH9329 UART→USB HID 驅動。所有序列埠 I/O 都在單一背景執行緒上進行。"""
import glob
import logging
import threading
import time
from collections import deque

import serial

log = logging.getLogger(__name__)

CMD_GET_INFO = 0x01
CMD_KB_GENERAL = 0x02
CMD_MS_ABS = 0x04
CMD_MS_REL = 0x05

ABS_MAX = 4095
POLL_INTERVAL = 1.0
MAX_TEXT = 5000

# US 鍵盤配置下的 ASCII → (HID usage, 是否需要 Shift)
_SHIFT_DIGITS = ")!@#$%^&*("
_PUNCT = {
    " ": (0x2C, False), "\n": (0x28, False), "\t": (0x2B, False),
    "-": (0x2D, False), "_": (0x2D, True), "=": (0x2E, False), "+": (0x2E, True),
    "[": (0x2F, False), "{": (0x2F, True), "]": (0x30, False), "}": (0x30, True),
    "\\": (0x31, False), "|": (0x31, True), ";": (0x33, False), ":": (0x33, True),
    "'": (0x34, False), '"': (0x34, True), "`": (0x35, False), "~": (0x35, True),
    ",": (0x36, False), "<": (0x36, True), ".": (0x37, False), ">": (0x37, True),
    "/": (0x38, False), "?": (0x38, True),
}


def char_to_hid(ch):
    if "a" <= ch <= "z":
        return 0x04 + ord(ch) - ord("a"), False
    if "A" <= ch <= "Z":
        return 0x04 + ord(ch) - ord("A"), True
    if "1" <= ch <= "9":
        return 0x1D + int(ch), False
    if ch == "0":
        return 0x27, False
    if ch in _SHIFT_DIGITS:
        d = _SHIFT_DIGITS.index(ch)
        return (0x27 if d == 0 else 0x1D + d), True
    return _PUNCT.get(ch)


def find_port():
    ports = sorted(glob.glob("/dev/serial/by-id/usb-1a86_*")) or sorted(glob.glob("/dev/ttyUSB*"))
    return ports[0] if ports else None


def _s8(v):
    return max(-127, min(127, int(v))) & 0xFF


class CH9329Error(Exception):
    pass


class CH9329:
    def __init__(self, port="auto", baud=115200, address=0):
        self.port_cfg = port
        self.baud = baud
        self.addr = address
        self._cv = threading.Condition()
        self._items = deque()
        self._move = None  # 只保留最新一筆尚未送出的滑鼠移動
        self._ser = None
        self._open_err = None
        self._stop = threading.Event()
        self._status = {"serial": False, "chip": False, "usb": False,
                        "num": False, "caps": False, "scroll": False,
                        "port": None, "version": None}
        # 以下狀態只會在 asyncio 執行緒上存取
        self._mods = 0
        self._keys = []
        self._buttons = 0
        self._pos = (ABS_MAX // 2, ABS_MAX // 2)
        self._abs = True
        self._thread = threading.Thread(target=self._run, name="ch9329", daemon=True)

    # ---------- 對外 API（從 asyncio 執行緒呼叫） ----------

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        with self._cv:
            self._cv.notify()
        self._thread.join(timeout=2)
        self._close_serial()

    def status(self):
        with self._cv:
            return dict(self._status)

    def key(self, usage, down):
        usage = int(usage) & 0xFF
        if 0xE0 <= usage <= 0xE7:
            bit = 1 << (usage - 0xE0)
            self._mods = self._mods | bit if down else self._mods & ~bit
        elif down:
            if usage not in self._keys and len(self._keys) < 6:
                self._keys.append(usage)
        elif usage in self._keys:
            self._keys.remove(usage)
        self._send_kb(self._mods, self._keys)

    def combo(self, usages):
        """同時按下一組按鍵後全部放開，例如 Ctrl+Alt+Del。"""
        mods, keys = 0, []
        for u in list(usages)[:8]:
            u = int(u) & 0xFF
            if 0xE0 <= u <= 0xE7:
                mods |= 1 << (u - 0xE0)
            elif len(keys) < 6:
                keys.append(u)
        self._send_kb(mods, keys)
        self._send_kb(0, [])
        self._send_kb(self._mods, self._keys)

    def type_text(self, text):
        """以 US 鍵盤配置逐字輸入，回傳略過的字元數。"""
        skipped = 0
        for ch in str(text)[:MAX_TEXT]:
            if ch == "\r":
                continue
            k = char_to_hid(ch)
            if k is None:
                skipped += 1
                continue
            usage, shift = k
            self._send_kb(0x02 if shift else 0, [usage])
            self._send_kb(0, [])
        self._send_kb(self._mods, self._keys)
        return skipped

    def release_all(self):
        self._mods, self._keys = 0, []
        self._send_kb(0, [])
        if self._buttons:
            self._buttons = 0
            if self._abs:
                self.mouse_abs(*self._pos, 0)
            else:
                self.mouse_rel(0, 0, 0)

    def mouse_abs(self, x, y, buttons, wheel=0):
        x = max(0, min(ABS_MAX, int(x)))
        y = max(0, min(ABS_MAX, int(y)))
        buttons = int(buttons) & 0x07
        coalesce = wheel == 0 and buttons == self._buttons and self._abs
        self._pos, self._buttons, self._abs = (x, y), buttons, True
        data = bytes([0x02, buttons, x & 0xFF, x >> 8, y & 0xFF, y >> 8, _s8(wheel)])
        self._submit(CMD_MS_ABS, data, coalesce)

    def mouse_rel(self, dx, dy, buttons, wheel=0):
        dx, dy, buttons = int(dx), int(dy), int(buttons) & 0x07
        self._buttons, self._abs = buttons, False
        while True:
            sx, sy = max(-127, min(127, dx)), max(-127, min(127, dy))
            self._submit(CMD_MS_REL, bytes([0x01, buttons, _s8(sx), _s8(sy), _s8(wheel)]))
            dx, dy, wheel = dx - sx, dy - sy, 0
            if not dx and not dy:
                break

    def mouse_wheel(self, steps):
        steps = max(-127, min(127, int(steps)))
        if self._abs:
            self.mouse_abs(*self._pos, self._buttons, steps)
        else:
            self.mouse_rel(0, 0, self._buttons, steps)

    # ---------- 內部 ----------

    def _send_kb(self, mods, keys):
        data = bytes([mods & 0xFF, 0] + list(keys[:6]) + [0] * (6 - len(keys[:6])))
        self._submit(CMD_KB_GENERAL, data)

    def _submit(self, cmd, data, coalesce=False):
        with self._cv:
            if not self._status["serial"]:
                return
            if coalesce:
                self._move = (cmd, data)
            else:
                if self._move:
                    self._items.append(self._move)
                    self._move = None
                self._items.append((cmd, data))
            self._cv.notify()

    def _take(self, timeout):
        with self._cv:
            if not self._items and self._move is None and timeout > 0:
                self._cv.wait(timeout)
            if self._items:
                return self._items.popleft()
            if self._move:
                m, self._move = self._move, None
                return m
            return None

    def _set(self, **kw):
        with self._cv:
            self._status.update(kw)

    def _open(self):
        port = find_port() if self.port_cfg == "auto" else self.port_cfg
        try:
            if not port:
                raise CH9329Error("找不到 CH340 序列埠")
            self._ser = serial.Serial(port, self.baud, timeout=0.1, write_timeout=0.5)
        except (serial.SerialException, OSError, CH9329Error) as e:
            if str(e) != self._open_err:
                log.warning("無法開啟 CH9329 序列埠: %s", e)
                self._open_err = str(e)
            return False
        self._open_err = None
        log.info("CH9329 序列埠已開啟: %s @ %d", port, self.baud)
        self._set(serial=True, port=port)
        return True

    def _close_serial(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
            self._ser = None
        with self._cv:
            self._items.clear()
            self._move = None
            self._status.update(serial=False, chip=False, usb=False)

    def _xfer(self, cmd, data):
        pkt = bytes([0x57, 0xAB, self.addr, cmd, len(data)]) + data
        self._ser.write(pkt + bytes([sum(pkt) & 0xFF]))
        hdr = self._ser.read(5)
        if len(hdr) < 5 or hdr[:2] != b"\x57\xab":
            self._ser.reset_input_buffer()
            raise CH9329Error(f"指令 0x{cmd:02X} 無回應或格式錯誤: {hdr.hex(' ')}")
        body = self._ser.read(hdr[4] + 1)
        if len(body) != hdr[4] + 1 or (sum(hdr + body[:-1]) & 0xFF) != body[-1]:
            self._ser.reset_input_buffer()
            raise CH9329Error(f"指令 0x{cmd:02X} 回應校驗失敗")
        if hdr[2] != self.addr or hdr[3] not in (cmd | 0x80, cmd | 0xC0):
            self._ser.reset_input_buffer()
            raise CH9329Error(f"指令 0x{cmd:02X} 回應位址或指令不符: {hdr.hex(' ')}")
        if hdr[3] == cmd | 0xC0:
            if not hdr[4]:
                raise CH9329Error(f"指令 0x{cmd:02X} 錯誤回應缺少狀態碼")
            raise CH9329Error(f"指令 0x{cmd:02X} 失敗，狀態碼 0x{body[0]:02X}")
        return body[:-1]

    def _poll_info(self):
        try:
            d = self._xfer(CMD_GET_INFO, b"")
            if len(d) < 3:
                raise CH9329Error(f"晶片資訊回應過短: {d.hex(' ')}")
        except CH9329Error as e:
            log.debug("%s", e)
            self._set(chip=False, usb=False)
            return
        self._set(chip=True, usb=bool(d[1]), num=bool(d[2] & 1), caps=bool(d[2] & 2),
                  scroll=bool(d[2] & 4), version=f"V{(d[0] - 0x30) / 10 + 1:.1f}")

    def _run(self):
        next_poll = 0.0
        while not self._stop.is_set():
            if self._ser is None and not self._open():
                self._stop.wait(2)
                continue
            item = self._take(next_poll - time.monotonic())
            try:
                if item:
                    try:
                        self._xfer(*item)
                    except CH9329Error as e:
                        log.debug("%s", e)
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + POLL_INTERVAL
                    self._poll_info()
            except (serial.SerialException, OSError) as e:
                log.warning("CH9329 序列埠錯誤: %s", e)
                self._close_serial()
