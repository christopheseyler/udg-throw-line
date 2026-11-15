"""Laser line on/off output.

Three backends, chosen by [laser] backend:

- led (default, production): a gpio-leds entry of the device tree, written
  through /sys/class/leds/<name>/brightness - the same scheme as the cabinet's
  on/off lights (udg-yocto doc/LIGHTING-INTERFACE.md). The kernel holds the
  line from boot with default-state = "off", and the line stays where it is
  if the service dies, so ExecStopPost= can still switch it off.
- gpio (bring-up): a line of a /dev/gpiochipN requested through the GPIO
  character device (uAPI v2) - any header pin can be tried from the /data
  configuration, without a new device tree and a reflash. The line is only
  driven while the service runs; when the process ends the kernel releases
  it but the RK3588 keeps the last level, hence the explicit "off" on exit
  and `udg-throw-line-service --laser-off` in ExecStopPost=.
- none: no output (laser wired permanently on, PC, tests).

`lit` is what the hardware was last successfully set to; the service only
watches the line while the laser is lit.
"""

import errno
import os
import struct

from . import log

# include/uapi/linux/gpio.h (v2 uAPI, Linux >= 5.10). Sizes and offsets
# checked against the headers (tests/test_laser.py).
GPIO_V2_LINE_REQUEST_SIZE = 592
GPIO_V2_GET_LINE_IOCTL = 0xC250B407
GPIO_V2_LINE_SET_VALUES_IOCTL = 0xC010B40F
GPIO_V2_LINE_FLAG_ACTIVE_LOW = 1 << 1
GPIO_V2_LINE_FLAG_OUTPUT = 1 << 3
GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES = 2
_REQ_CONSUMER = 256        # char consumer[32]
_REQ_CONFIG = 288          # struct gpio_v2_line_config: flags u64, num_attrs u32, padding[5]
_REQ_ATTRS = _REQ_CONFIG + 32   # attrs[10] of {id u32, pad u32, value u64, mask u64}
_REQ_NUM_LINES = 560
_REQ_FD = 588
CONSUMER = b"udg-throw-line"


def _default_ioctl(fd, request, arg):
    import fcntl  # Linux only
    return fcntl.ioctl(fd, request, arg, True)


class Laser:
    kind = "none"

    def __init__(self):
        self.lit = False
        self.available = True
        self.reason = ""

    def set(self, on):
        """Returns True when the output was set as asked."""
        self.lit = bool(on)
        return True

    def describe(self):
        return self.kind

    def close(self):
        pass


class NullLaser(Laser):
    """No output at all: the laser cannot be switched (calibration then
    works without laser-off frames)."""


class SimLaser(Laser):
    """Output of the simulated scene (`--simulate`): switchable, the
    synthetic camera draws the line only while it is lit."""
    kind = "sim"


class LedLaser(Laser):
    kind = "led"

    def __init__(self, name, root="/sys/class/leds"):
        super().__init__()
        self.name = name
        self.directory = name if os.path.isabs(name) else os.path.join(root, name)
        self._once = log.Once()

    def _max_brightness(self):
        try:
            with open(os.path.join(self.directory, "max_brightness")) as handle:
                return max(1, int(handle.read().strip() or "1"))
        except (OSError, ValueError):
            return 1

    def set(self, on):
        path = os.path.join(self.directory, "brightness")
        value = self._max_brightness() if on else 0
        try:
            with open(path, "w") as handle:
                handle.write("%d\n" % value)
        except OSError as exc:
            self.available = False
            self.lit = False
            if exc.errno == errno.ENOENT:
                self.reason = "%s missing (no gpio-leds entry '%s' in the device tree?)" % (path, self.name)
            else:
                self.reason = "%s: %s" % (path, exc.strerror or exc)
            self._once(log.error, "laser: " + self.reason)
            return False
        if not self.available:
            log.info("laser: %s is back" % path)
        self.available = True
        self.reason = ""
        self._once.reset()
        self.lit = bool(on)
        return True

    def describe(self):
        return "led:%s%s" % (self.name, "" if self.available else "(unavailable)")


def line_request(offset, active_low, value=False):
    """struct gpio_v2_line_request for one output line, initial level `value`
    (logical: True = active)."""
    req = bytearray(GPIO_V2_LINE_REQUEST_SIZE)
    struct.pack_into("<I", req, 0, offset)
    req[_REQ_CONSUMER:_REQ_CONSUMER + len(CONSUMER)] = CONSUMER
    flags = GPIO_V2_LINE_FLAG_OUTPUT | (GPIO_V2_LINE_FLAG_ACTIVE_LOW if active_low else 0)
    struct.pack_into("<QI", req, _REQ_CONFIG, flags, 1)
    # One attribute: the initial output value of line 0 of the request, so the
    # laser never flashes on while the line switches to output.
    struct.pack_into("<IIQQ", req, _REQ_ATTRS, GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES, 0, int(bool(value)), 1)
    struct.pack_into("<I", req, _REQ_NUM_LINES, 1)
    return req


def line_values(value):
    """struct gpio_v2_line_values {bits, mask} for line 0 of a request."""
    return bytearray(struct.pack("<QQ", int(bool(value)), 1))


class GpioLaser(Laser):
    kind = "gpio"

    def __init__(self, chip, line, active_low=False, ioctl=None, opener=None):
        super().__init__()
        self.chip = chip if chip.startswith("/") else "/dev/" + chip
        self.line = line
        self.active_low = active_low
        self._ioctl = ioctl or _default_ioctl
        self._open = opener or (lambda path, flags: os.open(path, flags))
        self.fd = None
        self._once = log.Once()

    def _request(self, value):
        if self.line < 0:
            raise OSError(errno.EINVAL, "[laser] line is not set")
        chip_fd = self._open(self.chip, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        try:
            req = line_request(self.line, self.active_low, value)
            self._ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, req)
            fd = struct.unpack_from("<i", req, _REQ_FD)[0]
        finally:
            # The line request has its own descriptor: the chip one is done.
            try:
                os.close(chip_fd)
            except OSError:
                pass
        if fd < 0:
            raise OSError(errno.EIO, "no line descriptor returned")
        return fd

    def set(self, on):
        try:
            if self.fd is None:
                # The request itself sets the level: no glitch.
                self.fd = self._request(on)
            else:
                self._ioctl(self.fd, GPIO_V2_LINE_SET_VALUES_IOCTL, line_values(on))
        except OSError as exc:
            self.available = False
            self.lit = False
            self.reason = "%s line %d: %s" % (self.chip, self.line, exc.strerror or exc)
            if exc.errno == errno.EBUSY:
                self.reason += " (line already used: check `gpioinfo`)"
            self._once(log.error, "laser: " + self.reason)
            self._drop()
            return False
        if not self.available:
            log.info("laser: %s line %d is back" % (self.chip, self.line))
        self.available = True
        self.reason = ""
        self._once.reset()
        self.lit = bool(on)
        return True

    def _drop(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def close(self):
        if self.fd is not None:
            self.set(False)
        self._drop()

    def describe(self):
        return "gpio:%s:%d%s%s" % (self.chip, self.line, ":active-low" if self.active_low else "",
                                   "" if self.available else "(unavailable)")


def make_laser(cfg):
    if cfg.laser_backend == "led":
        return LedLaser(cfg.laser_led)
    if cfg.laser_backend == "gpio":
        return GpioLaser(cfg.laser_chip, cfg.laser_line, cfg.laser_active_low)
    return NullLaser()
