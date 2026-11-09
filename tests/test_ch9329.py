import unittest
from unittest.mock import Mock

from webkvm.ch9329 import CH9329, CH9329Error, CMD_GET_INFO


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


if __name__ == "__main__":
    unittest.main()
