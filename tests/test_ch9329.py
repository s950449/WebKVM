import unittest
from unittest.mock import Mock

from webkvm.ch9329 import CH9329, CH9329Error, CMD_GET_INFO, CMD_MS_REL


def response(command, data, address=0):
    packet = bytes([0x57, 0xAB, address, command, len(data)]) + data
    return packet[:5], packet[5:] + bytes([sum(packet) & 0xFF])


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.chip = CH9329()
        self.chip._ser = Mock()

    def test_short_info_does_not_crash_and_next_poll_recovers(self):
        for data in (b"", b"\x30", b"\x30\x01"):
            with self.subTest(data=data):
                self.chip._set(chip=True, usb=True)
                self.chip._ser.read.side_effect = response(0x81, data)
                self.chip._poll_info()
                self.assertFalse(self.chip.status()["chip"])
                self.assertFalse(self.chip.status()["usb"])
        self.chip._ser.read.side_effect = response(0x81, b"\x30\x01\x07")
        self.chip._poll_info()
        status = self.chip.status()
        for key in ("chip", "usb", "num", "caps", "scroll"):
            self.assertTrue(status[key])
        self.assertEqual(status["version"], "V1.0")

    def test_rejects_wrong_command_or_address(self):
        for command, address in ((0x82, 0), (0x81, 1)):
            with self.subTest(command=command, address=address):
                self.chip._ser.read.side_effect = response(command, b"\x00", address)
                with self.assertRaises(CH9329Error):
                    self.chip._xfer(CMD_GET_INFO, b"")

    def test_empty_error_response_is_protocol_error(self):
        self.chip._ser.read.side_effect = response(0xC1, b"")
        with self.assertRaises(CH9329Error):
            self.chip._xfer(CMD_GET_INFO, b"")


class JiggleTests(unittest.TestCase):
    def setUp(self):
        self.chip = CH9329(jiggle=True, jiggle_interval=60)
        self.chip._set(serial=True, chip=True, usb=True)
        self.chip._last_input = 0.0

    def test_moves_and_returns_when_idle(self):
        self.assertTrue(self.chip.jiggle_tick(now=60.0))
        items = list(self.chip._items)
        self.assertEqual([c for c, _ in items], [CMD_MS_REL, CMD_MS_REL])
        dx = [d[2] - 256 if d[2] > 127 else d[2] for _, d in items]
        self.assertEqual(sum(dx), 0)
        self.assertTrue(all(d[1] == 0 for _, d in items))
        self.assertFalse(self.chip.jiggle_tick(now=61.0))  # 剛移動過，重新計時

    def test_skips_when_not_idle_disabled_or_unplugged(self):
        self.assertFalse(self.chip.jiggle_tick(now=59.0))
        self.chip._set(usb=False)
        self.assertFalse(self.chip.jiggle_tick(now=120.0))
        self.chip._set(usb=True)
        self.chip.set_jiggle(False)
        self.assertFalse(self.chip.jiggle_tick(now=1e9))
        self.assertFalse(self.chip._items)

    def test_skips_while_key_or_button_held(self):
        self.chip._keys = [0x04]
        self.assertFalse(self.chip.jiggle_tick(now=120.0))
        self.chip._keys, self.chip._buttons = [], 1
        self.assertFalse(self.chip.jiggle_tick(now=120.0))

    def test_user_input_resets_idle_timer(self):
        self.chip.key(0x04, True)
        self.chip.key(0x04, False)
        self.chip._items.clear()
        self.assertFalse(self.chip.jiggle_tick())

    def test_interval_is_clamped(self):
        self.assertEqual(CH9329(jiggle_interval=1).status()["jiggle_interval"], 5)


if __name__ == "__main__":
    unittest.main()
