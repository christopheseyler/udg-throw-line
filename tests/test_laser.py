"""Laser outputs.

GPIO uAPI v2 numbers from a C program compiled against linux/gpio.h:
    sizeof gpio_v2_line_request 592, gpio_v2_line_config 272,
    gpio_v2_line_config_attribute 24, gpio_v2_line_values 16;
    offsetof request.consumer 256, .config 288, .num_lines 560, .fd 588;
    offsetof config.num_attrs 8, .attrs 32;
    GPIO_V2_GET_LINE_IOCTL 0xc250b407, GPIO_V2_LINE_SET_VALUES_IOCTL 0xc010b40f,
    FLAG_OUTPUT 8, FLAG_ACTIVE_LOW 2, ATTR_ID_OUTPUT_VALUES 2.
"""

import errno
import os
import struct
import unittest

from udg_throw_line import laser
from udg_throw_line.laser import GpioLaser, LedLaser, NullLaser, line_request, line_values, make_laser

from .support import TempDirTest, make_cfg


class RequestLayoutTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(laser.GPIO_V2_LINE_REQUEST_SIZE, 592)
        self.assertEqual(laser.GPIO_V2_GET_LINE_IOCTL, 0xC250B407)
        self.assertEqual(laser.GPIO_V2_LINE_SET_VALUES_IOCTL, 0xC010B40F)
        self.assertEqual(laser.GPIO_V2_LINE_FLAG_OUTPUT, 8)
        self.assertEqual(laser.GPIO_V2_LINE_FLAG_ACTIVE_LOW, 2)

    def test_request_bytes(self):
        req = line_request(12, active_low=True, value=False)
        self.assertEqual(len(req), 592)
        self.assertEqual(struct.unpack_from("<I", req, 0)[0], 12)              # offsets[0]
        self.assertEqual(req[256:256 + 14], b"udg-throw-line")                 # consumer
        self.assertEqual(req[256 + 14], 0)
        self.assertEqual(struct.unpack_from("<Q", req, 288)[0], 8 | 2)        # config.flags
        self.assertEqual(struct.unpack_from("<I", req, 296)[0], 1)            # config.num_attrs
        # attrs[0]: id = OUTPUT_VALUES, values = 0 (inactive), mask = line 0
        self.assertEqual(struct.unpack_from("<IIQQ", req, 320), (2, 0, 0, 1))
        self.assertEqual(struct.unpack_from("<I", req, 560)[0], 1)            # num_lines
        self.assertEqual(struct.unpack_from("<i", req, 588)[0], 0)            # fd (output)
        on = line_request(3, active_low=False, value=True)
        self.assertEqual(struct.unpack_from("<Q", on, 288)[0], 8)
        self.assertEqual(struct.unpack_from("<Q", on, 328)[0], 1)

    def test_values(self):
        self.assertEqual(line_values(True), struct.pack("<QQ", 1, 1))
        self.assertEqual(line_values(False), struct.pack("<QQ", 0, 1))


class FakeGpio:
    CHIP_FD = 911
    LINE_FD = 912

    def __init__(self, busy=False):
        self.busy = busy
        self.calls = []
        self.level = None

    def opener(self, path, flags):
        self.calls.append(("open", path))
        return self.CHIP_FD

    def ioctl(self, fd, request, arg):
        if request == laser.GPIO_V2_GET_LINE_IOCTL:
            if self.busy:
                raise OSError(errno.EBUSY, "Device or resource busy")
            self.calls.append(("request", struct.unpack_from("<I", arg, 0)[0]))
            self.level = struct.unpack_from("<Q", arg, 328)[0]
            struct.pack_into("<i", arg, 588, self.LINE_FD)
        elif request == laser.GPIO_V2_LINE_SET_VALUES_IOCTL:
            self.assertFd(fd)
            self.level = struct.unpack("<QQ", arg)[0]
            self.calls.append(("set", self.level))
        else:
            raise OSError(errno.ENOTTY, "bad ioctl")

    def assertFd(self, fd):
        if fd != self.LINE_FD:
            raise AssertionError("values set on fd %d" % fd)


class GpioLaserTest(unittest.TestCase):
    def setUp(self):
        from .support import quiet_logs
        quiet_logs()

    def test_request_then_set(self):
        fake = FakeGpio()
        out = GpioLaser("gpiochip1", 4, ioctl=fake.ioctl, opener=fake.opener)
        self.assertTrue(out.set(True))
        self.assertEqual(fake.calls[:2], [("open", "/dev/gpiochip1"), ("request", 4)])
        self.assertEqual(fake.level, 1)     # the request itself sets the level
        self.assertTrue(out.lit)
        self.assertTrue(out.set(False))
        self.assertEqual(fake.calls[-1], ("set", 0))
        self.assertFalse(out.lit)
        self.assertEqual(out.describe(), "gpio:/dev/gpiochip1:4")

    def test_busy_line(self):
        fake = FakeGpio(busy=True)
        out = GpioLaser("/dev/gpiochip1", 4, ioctl=fake.ioctl, opener=fake.opener)
        self.assertFalse(out.set(True))
        self.assertFalse(out.lit)
        self.assertFalse(out.available)
        self.assertIn("gpioinfo", out.reason)
        self.assertIn("unavailable", out.describe())
        fake.busy = False
        self.assertTrue(out.set(True))
        self.assertTrue(out.available)

    def test_unset_line(self):
        out = GpioLaser("/dev/gpiochip1", -1, ioctl=FakeGpio().ioctl, opener=FakeGpio().opener)
        self.assertFalse(out.set(True))
        self.assertIn("line is not set", out.reason)

    def test_close_switches_off(self):
        fake = FakeGpio()
        out = GpioLaser("/dev/gpiochip1", 4, active_low=True, ioctl=fake.ioctl, opener=fake.opener)
        out.set(True)
        self.assertEqual(out.fd, FakeGpio.LINE_FD)
        out.close()     # closing the fake descriptor fails quietly (EBADF)
        self.assertEqual(fake.calls[-1], ("set", 0))
        self.assertIsNone(out.fd)


class LedLaserTest(TempDirTest):
    def make_led(self, max_brightness="1"):
        directory = self.path("leds", "udg-laser")
        os.makedirs(directory)
        self.write(os.path.join("leds", "udg-laser", "max_brightness"), max_brightness + "\n")
        self.write(os.path.join("leds", "udg-laser", "brightness"), "0\n")
        return directory

    def read_brightness(self):
        with open(self.path("leds", "udg-laser", "brightness")) as handle:
            return handle.read().strip()

    def test_on_off(self):
        self.make_led("255")
        out = LedLaser("udg-laser", root=self.path("leds"))
        self.assertTrue(out.set(True))
        self.assertEqual(self.read_brightness(), "255")
        self.assertTrue(out.lit)
        self.assertTrue(out.set(False))
        self.assertEqual(self.read_brightness(), "0")
        self.assertEqual(out.describe(), "led:udg-laser")

    def test_missing_then_back(self):
        out = LedLaser("udg-laser", root=self.path("leds"))
        self.assertFalse(out.set(True))
        self.assertFalse(out.lit)
        self.assertIn("device tree", out.reason)
        self.assertEqual(out.describe(), "led:udg-laser(unavailable)")
        self.make_led()
        self.assertTrue(out.set(True))
        self.assertEqual(self.read_brightness(), "1")

    def test_make_laser(self):
        self.assertIsInstance(make_laser(make_cfg(laser_backend="none")), NullLaser)
        self.assertIsInstance(make_laser(make_cfg(laser_backend="led")), LedLaser)
        self.assertIsInstance(make_laser(make_cfg(laser_backend="gpio", laser_line=3)), GpioLaser)


if __name__ == "__main__":
    unittest.main()
