"""V4L2 capture of the CSI camera, in pure Python (struct + ioctl + mmap).

Pipeline on the ROCK 5C (udg-yocto device tree patch 0002): OV5647 sensor ->
CSI2 D-PHY0 -> MIPI2 CSI2 -> CIF ("rkcif"). The CIF capture nodes
(`stream_cif_mipi_id0`...) write the sensor's raw Bayer data to memory, with
no ISP and no rkaiq, and only announce the multi-planar capture API
(V4L2_CAP_VIDEO_CAPTURE_MPLANE).

What has to be done by hand, because the CIF does not do it:

- the sensor mode is chosen on the sensor sub-device (VIDIOC_SUBDEV_S_FMT):
  the CIF reads the sensor's current format at S_FMT time and only crops;
- the OV5647's Bayer order changes with its flip controls (the driver
  reports SGBRG10 with the default flips, not the SBGGR10 of its mode
  table), so flips are applied first and the order is read back afterwards;
- on the RK3588 the CIF stores RAW10 "compact" by default, a packing whose
  exact bit order is not documented. The rkcif private ioctl
  RKCIF_CMD_SET_CSI_MEMORY_MODE switches the stream to one 16-bit word per
  pixel (low-aligned), which removes the question entirely; it must come
  after a first S_FMT (the CIF needs to know the sensor) and before the S_FMT
  that computes bytesperline.

Structure sizes and ioctl numbers were checked against the Linux uapi headers
(see tests/test_v4l2.py); aarch64 and x86_64 share them (LP64).
"""

import errno
import os
import struct
from array import array

from . import log
from .frame import FormatError, Frame, FrameFormat

# --- ioctl numbers (asm-generic encoding, shared by arm64 and x86) ----------

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, kind, nr, size):
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | nr


def _ior(kind, nr, size):
    return _ioc(_IOC_READ, kind, nr, size)


def _iow(kind, nr, size):
    return _ioc(_IOC_WRITE, kind, nr, size)


def _iowr(kind, nr, size):
    return _ioc(_IOC_READ | _IOC_WRITE, kind, nr, size)


CAPABILITY = struct.Struct("<16s32s32sIII12x")                 # struct v4l2_capability
FORMAT_SIZE = 208                                              # struct v4l2_format
REQUESTBUFFERS = struct.Struct("<IIIIB3x")                     # struct v4l2_requestbuffers
BUFFER = struct.Struct("<5I4x2q16s2IQ2Ii4x")                   # struct v4l2_buffer
PLANE = struct.Struct("<IIQI44x")                              # struct v4l2_plane
CONTROL = struct.Struct("<Ii")                                 # struct v4l2_control
SUBDEV_FORMAT = struct.Struct("<II5I4H20x32x")                 # struct v4l2_subdev_format

VIDIOC_QUERYCAP = _ior("V", 0, CAPABILITY.size)
VIDIOC_G_FMT = _iowr("V", 4, FORMAT_SIZE)
VIDIOC_S_FMT = _iowr("V", 5, FORMAT_SIZE)
VIDIOC_REQBUFS = _iowr("V", 8, REQUESTBUFFERS.size)
VIDIOC_QUERYBUF = _iowr("V", 9, BUFFER.size)
VIDIOC_QBUF = _iowr("V", 15, BUFFER.size)
VIDIOC_DQBUF = _iowr("V", 17, BUFFER.size)
VIDIOC_STREAMON = _iow("V", 18, 4)
VIDIOC_STREAMOFF = _iow("V", 19, 4)
VIDIOC_G_CTRL = _iowr("V", 27, CONTROL.size)
VIDIOC_S_CTRL = _iowr("V", 28, CONTROL.size)
VIDIOC_SUBDEV_G_FMT = _iowr("V", 4, SUBDEV_FORMAT.size)
VIDIOC_SUBDEV_S_FMT = _iowr("V", 5, SUBDEV_FORMAT.size)

# include/uapi/linux/rkcif-config.h (vendor kernel): BASE_VIDIOC_PRIVATE = 192.
RKCIF_CMD_GET_CSI_MEMORY_MODE = _ior("V", 192 + 0, 4)
RKCIF_CMD_SET_CSI_MEMORY_MODE = _iow("V", 192 + 1, 4)
CSI_LVDS_MEM_COMPACT = 0
CSI_LVDS_MEM_WORD_LOW_ALIGN = 1
CSI_LVDS_MEM_WORD_HIGH_ALIGN = 2
MEMORY_MODES = {
    "compact": CSI_LVDS_MEM_COMPACT,
    "word_low": CSI_LVDS_MEM_WORD_LOW_ALIGN,
    "word_high": CSI_LVDS_MEM_WORD_HIGH_ALIGN,
}

V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_CAP_STREAMING = 0x04000000
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 9
V4L2_MEMORY_MMAP = 1
V4L2_BUF_FLAG_ERROR = 0x00000040
V4L2_FIELD_ANY = 0
V4L2_SUBDEV_FORMAT_ACTIVE = 1

V4L2_CID_EXPOSURE = 0x00980911
V4L2_CID_AUTOGAIN = 0x00980912
V4L2_CID_HFLIP = 0x00980914
V4L2_CID_VFLIP = 0x00980915
V4L2_CID_EXPOSURE_AUTO = 0x009A0901
V4L2_CID_ANALOGUE_GAIN = 0x009E0903
V4L2_EXPOSURE_MANUAL = 1

# Offsets inside struct v4l2_format / v4l2_pix_format_mplane.
_FMT_PIX = 8                    # union fmt (after __u32 type + padding)
_FMT_PLANES = _FMT_PIX + 20     # plane_fmt[8], 20 bytes each: sizeimage, bytesperline, reserved
_FMT_NUM_PLANES = _FMT_PIX + 180


def fourcc(code):
    return ord(code[0]) | ord(code[1]) << 8 | ord(code[2]) << 16 | ord(code[3]) << 24


def fourcc_text(value):
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


# fourcc -> (Bayer order, bits per pixel, MIPI-packed)
PIXEL_FORMATS = {
    fourcc("BG10"): ("BGGR", 10, False),
    fourcc("GB10"): ("GBRG", 10, False),
    fourcc("BA10"): ("GRBG", 10, False),
    fourcc("RG10"): ("RGGB", 10, False),
    fourcc("BA81"): ("BGGR", 8, False),
    fourcc("GBRG"): ("GBRG", 8, False),
    fourcc("GRBG"): ("GRBG", 8, False),
    fourcc("RGGB"): ("RGGB", 8, False),
    fourcc("pBAA"): ("BGGR", 10, True),
    fourcc("pGAA"): ("GBRG", 10, True),
    fourcc("pgAA"): ("GRBG", 10, True),
    fourcc("pRAA"): ("RGGB", 10, True),
}
UNPACKED = {(bayer, bits): code for code, (bayer, bits, packed) in PIXEL_FORMATS.items() if not packed}

# media bus code -> (Bayer order, bits)
MBUS_CODES = {
    0x3007: ("BGGR", 10),
    0x300E: ("GBRG", 10),
    0x300A: ("GRBG", 10),
    0x300F: ("RGGB", 10),
    0x3001: ("BGGR", 8),
    0x3013: ("GBRG", 8),
    0x3002: ("GRBG", 8),
    0x3014: ("RGGB", 8),
}

SYSFS_V4L = "/sys/class/video4linux"
# Video node names of the CIF MIPI capture streams (drivers/media/platform/
# rockchip/cif/dev.h: CIF_VIDEODEVICE_NAME "_mipi_id0"...). id0 carries the
# sensor's main (only) virtual channel.
CIF_NODE_NAMES = ("stream_cif_mipi_id0",)
SENSOR_NAME = "ov5647"


class CameraError(Exception):
    """Camera not usable; `state` is "absent" (no node) or "error"."""

    def __init__(self, message, state="error"):
        super().__init__(message)
        self.state = state


def _default_ioctl(fd, request, arg):
    import fcntl  # Linux only; imported here so the package loads on Windows
    return fcntl.ioctl(fd, request, arg, True)


def _default_mapper(fd, length, offset):
    import mmap
    return mmap.mmap(fd, length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ, offset=offset)


def _read_name(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _node_number(entry):
    digits = "".join(ch for ch in entry if ch.isdigit())
    return int(digits) if digits else 0


def find_nodes(sysfs, prefix, match):
    """/dev paths of the sysfs video4linux entries `prefix*` whose name
    satisfies match(name), lowest number first."""
    try:
        entries = os.listdir(sysfs)
    except OSError:
        return []
    found = []
    for entry in sorted((e for e in entries if e.startswith(prefix)), key=_node_number):
        name = _read_name(os.path.join(sysfs, entry, "name"))
        if name is not None and match(name):
            found.append(("/dev/" + entry, name))
    return found


class V4L2Camera:
    kind = "v4l2"

    def __init__(self, cfg, ioctl=None, opener=None, mapper=None, sysfs=SYSFS_V4L):
        self.cfg = cfg
        self._ioctl = ioctl or _default_ioctl
        self._open_node = opener or (lambda path, flags: os.open(path, flags))
        self._map = mapper or _default_mapper
        self.sysfs = sysfs
        self.state = "stopped"
        self.reason = ""
        self.fd = None
        self.subdev_fd = None
        self.video_path = None
        self.subdev_path = None
        self.fmt = None
        self.pixelformat = 0
        self.signature = None
        self.buffers = []
        self.num_planes = 1
        self.controls = {}
        # Bumped at each successful open: the service loop re-registers the
        # file descriptor even if the new one got the same number.
        self.generation = 0
        self._planes = None
        self._queued = set()

    # --- lifecycle -----------------------------------------------------------

    def open(self, now=0.0):
        """Open, configure and start streaming. Raises CameraError."""
        try:
            self._open()
        except CameraError as exc:
            self.close()
            self.state = exc.state
            self.reason = str(exc)
            raise
        except OSError as exc:
            self.close()
            self.state = "error"
            self.reason = "%s: %s" % (self.video_path or "camera", exc.strerror or exc)
            raise CameraError(self.reason) from exc
        self.state = "streaming"
        self.reason = ""
        self.generation += 1

    def _open(self):
        self.video_path = self._resolve_video()
        self.subdev_path = self._resolve_subdev()
        sensor = None
        if self.subdev_path:
            sensor = self._setup_sensor()
        flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        self.fd = self._open_node(self.video_path, flags)
        self._check_caps()
        width, height = (sensor[0], sensor[1]) if sensor else (self.cfg.width, self.cfg.height)
        sensor_bayer = sensor[2] if sensor else None
        bits = sensor[3] if sensor else 10
        wanted = UNPACKED.get((sensor_bayer or self._configured_bayer() or "BGGR", bits), fourcc("BG10"))
        self._set_format(width, height, wanted)
        mode = self._set_memory_mode()
        if mode is not None:
            # bytesperline is computed by S_FMT from the memory mode.
            self._set_format(width, height, wanted)
        self.fmt = self._read_format(sensor_bayer, mode)
        self._start_streaming()
        self.signature = "%dx%d %s hflip=%d vflip=%d" % (
            self.fmt.width, self.fmt.height, fourcc_text(self.pixelformat), self.cfg.hflip, self.cfg.vflip)

    def close(self):
        if self.fd is not None:
            try:
                self._ioctl(self.fd, VIDIOC_STREAMOFF, bytearray(struct.pack("<I", V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)))
            except OSError:
                pass
        had_buffers = bool(self.buffers)
        for buffer in self.buffers:
            close = getattr(buffer, "close", None)
            if close is not None:
                try:
                    close()
                except (OSError, ValueError, BufferError) as exc:
                    # BufferError: a Frame was not released. Never fatal.
                    log.warning("camera buffer not unmapped: %s" % exc)
        self.buffers = []
        self._queued = set()
        if self.fd is not None and had_buffers:
            # Free the driver's buffers now (they would go with the fd anyway).
            try:
                self._ioctl(self.fd, VIDIOC_REQBUFS, bytearray(REQUESTBUFFERS.pack(
                    0, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, V4L2_MEMORY_MMAP, 0, 0)))
            except OSError:
                pass
        for attr in ("fd", "subdev_fd"):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)
        if self.state == "streaming":
            self.state = "stopped"

    def fileno(self):
        return self.fd if self.state == "streaming" else None

    def next_deadline(self, now):
        return float("inf")   # driven by the file descriptor

    def describe(self):
        if self.state == "streaming" and self.fmt is not None:
            return "v4l2:%s,%s,%s" % (self.video_path, fourcc_text(self.pixelformat), self.fmt.describe())
        if self.reason:
            return "v4l2:%s" % self.reason.replace(" ", "_")
        return "v4l2"

    # --- discovery -----------------------------------------------------------

    def _resolve_video(self):
        if self.cfg.video != "auto":
            return self.cfg.video
        nodes = find_nodes(self.sysfs, "video", lambda name: name in CIF_NODE_NAMES)
        if not nodes:
            raise CameraError("no CIF capture node (%s) in %s" % (", ".join(CIF_NODE_NAMES), self.sysfs), "absent")
        return nodes[0][0]

    def _resolve_subdev(self):
        if self.cfg.subdev == "none":
            return None
        if self.cfg.subdev != "auto":
            return self.cfg.subdev
        nodes = find_nodes(self.sysfs, "v4l-subdev", lambda name: SENSOR_NAME in name.lower())
        if not nodes:
            log.notice("no %s sub-device in %s: keeping the sensor's current mode and controls"
                       % (SENSOR_NAME, self.sysfs))
            return None
        return nodes[0][0]

    def _configured_bayer(self):
        return None if self.cfg.bayer == "auto" else self.cfg.bayer.upper()

    # --- sensor sub-device ---------------------------------------------------

    def _set_ctrl(self, fd, cid, value):
        arg = bytearray(CONTROL.pack(cid, value))
        self._ioctl(fd, VIDIOC_S_CTRL, arg)

    def _get_ctrl(self, fd, cid):
        arg = bytearray(CONTROL.pack(cid, 0))
        self._ioctl(fd, VIDIOC_G_CTRL, arg)
        return CONTROL.unpack(arg)[1]

    def _try_ctrl(self, fd, cid, value, name):
        try:
            self._set_ctrl(fd, cid, value)
            return True
        except OSError as exc:
            log.warning("sensor control %s=%d not applied: %s" % (name, value, exc.strerror or exc))
            return False

    def _setup_sensor(self):
        """Flips, manual exposure, mode. Returns (width, height, bayer, bits)."""
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        try:
            self.subdev_fd = self._open_node(self.subdev_path, flags)
        except OSError as exc:
            log.warning("%s: %s: keeping the sensor's current mode" % (self.subdev_path, exc.strerror or exc))
            self.subdev_fd = None
            return None
        fd = self.subdev_fd
        # Flips first: they change the Bayer order the sensor reports.
        self._try_ctrl(fd, V4L2_CID_HFLIP, int(self.cfg.hflip), "hflip")
        self._try_ctrl(fd, V4L2_CID_VFLIP, int(self.cfg.vflip), "vflip")
        # The detection compares every frame with the calibrated reference:
        # the exposure must never move on its own.
        try:
            self._set_ctrl(fd, V4L2_CID_EXPOSURE_AUTO, V4L2_EXPOSURE_MANUAL)
        except OSError:
            pass
        try:
            self._set_ctrl(fd, V4L2_CID_AUTOGAIN, 0)
        except OSError:
            pass
        self.set_controls(self.cfg.exposure or None, self.cfg.gain or None, fd=fd)
        arg = bytearray(SUBDEV_FORMAT.pack(V4L2_SUBDEV_FORMAT_ACTIVE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self._ioctl(fd, VIDIOC_SUBDEV_G_FMT, arg)
        values = list(SUBDEV_FORMAT.unpack(arg))
        values[2], values[3] = self.cfg.width, self.cfg.height     # width, height; keep code
        arg = bytearray(SUBDEV_FORMAT.pack(*values))
        self._ioctl(fd, VIDIOC_SUBDEV_S_FMT, arg)
        which, pad, width, height, code = SUBDEV_FORMAT.unpack(arg)[:5]
        if (width, height) != (self.cfg.width, self.cfg.height):
            log.notice("sensor mode %dx%d chosen for the requested %dx%d (nearest mode of the driver)"
                       % (width, height, self.cfg.width, self.cfg.height))
        bayer, bits = MBUS_CODES.get(code, (None, 10))
        if bayer is None:
            log.warning("unknown sensor media bus code 0x%04x: Bayer order from the video format" % code)
        return width, height, bayer, bits

    def set_controls(self, exposure=None, gain=None, fd=None):
        """Applies exposure (sensor lines) / analogue gain and returns the
        values read back (the driver clamps to its range)."""
        fd = self.subdev_fd if fd is None else fd
        if fd is None:
            raise CameraError("no sensor sub-device: exposure and gain cannot be changed")
        if exposure is not None:
            self._try_ctrl(fd, V4L2_CID_EXPOSURE, exposure, "exposure")
        if gain is not None:
            self._try_ctrl(fd, V4L2_CID_ANALOGUE_GAIN, gain, "gain")
        result = {}
        for name, cid in (("exposure", V4L2_CID_EXPOSURE), ("gain", V4L2_CID_ANALOGUE_GAIN)):
            try:
                result[name] = self._get_ctrl(fd, cid)
            except OSError:
                pass
        self.controls = result
        return result

    # --- video node ----------------------------------------------------------

    def _check_caps(self):
        arg = bytearray(CAPABILITY.size)
        self._ioctl(self.fd, VIDIOC_QUERYCAP, arg)
        driver, card, bus, version, caps, device_caps = CAPABILITY.unpack(arg)
        if caps & V4L2_CAP_DEVICE_CAPS:
            caps = device_caps
        missing = []
        if not caps & V4L2_CAP_VIDEO_CAPTURE_MPLANE:
            missing.append("multi-planar capture")
        if not caps & V4L2_CAP_STREAMING:
            missing.append("streaming")
        if missing:
            raise CameraError("%s (%s) lacks %s" % (self.video_path, driver.rstrip(b"\0").decode("ascii", "replace"),
                                                    " and ".join(missing)))

    def _set_format(self, width, height, pixelformat):
        arg = bytearray(FORMAT_SIZE)
        struct.pack_into("<I", arg, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
        struct.pack_into("<5I", arg, _FMT_PIX, width, height, pixelformat, V4L2_FIELD_ANY, 0)
        self._ioctl(self.fd, VIDIOC_S_FMT, arg)

    def _set_memory_mode(self):
        name = self.cfg.memory_mode
        if name == "keep":
            return None
        try:
            self._ioctl(self.fd, RKCIF_CMD_SET_CSI_MEMORY_MODE, bytearray(struct.pack("<i", MEMORY_MODES[name])))
        except OSError as exc:
            # ENOTTY on any driver but rkcif: not an error, the layout is
            # then deduced from bytesperline.
            level = log.debug if exc.errno == errno.ENOTTY else log.warning
            level("CIF memory mode %s not set: %s" % (name, exc.strerror or exc))
            return None
        return name

    def _read_format(self, sensor_bayer, mode):
        arg = bytearray(FORMAT_SIZE)
        struct.pack_into("<I", arg, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
        self._ioctl(self.fd, VIDIOC_G_FMT, arg)
        width, height, pixelformat = struct.unpack_from("<3I", arg, _FMT_PIX)
        self.num_planes = max(1, arg[_FMT_NUM_PLANES])
        sizeimage, stride = struct.unpack_from("<II", arg, _FMT_PLANES)
        info = PIXEL_FORMATS.get(pixelformat)
        if info is None:
            raise CameraError("unsupported pixel format %r from %s (raw Bayer 8/10 bits expected)"
                              % (fourcc_text(pixelformat), self.video_path))
        bayer, bits, packed = info
        if packed:
            layout = "mipi10"
        elif bits == 8:
            layout = "raw8"
        elif stride >= 2 * width:
            layout = "raw16"
        else:
            # rkcif "compact" mode: assumed to be the MIPI CSI-2 packing
            # (not verified on the hardware, see README).
            layout = "mipi10"
            log.warning("%s: RAW10 stored compact (bytesperline %d for %d pixels); reading it as MIPI "
                        "RAW10 packing, which is unverified on the rkcif - prefer [camera] memory_mode = word_low"
                        % (self.video_path, stride, width))
        if layout == "raw16":
            # word_high puts the 10 bits at the top of the 16-bit word.
            max_value = 0xFFC0 if mode == "word_high" else (1 << bits) - 1
        else:
            max_value = 255
        bayer = self._configured_bayer() or sensor_bayer or bayer
        self.pixelformat = pixelformat
        try:
            fmt = FrameFormat(width, height, stride, layout, bayer, max_value)
        except FormatError as exc:
            raise CameraError("%s: %s" % (self.video_path, exc)) from None
        if sizeimage and sizeimage < fmt.frame_bytes:
            raise CameraError("%s: sizeimage %d smaller than %d x %d" % (self.video_path, sizeimage, stride, height))
        log.info("camera %s: %dx%d %s, bytesperline %d, %s, Bayer %s%s"
                 % (self.video_path, width, height, fourcc_text(pixelformat), stride, layout, bayer,
                    ", CIF memory mode %s" % mode if mode else ""))
        return fmt

    def _buffer_arg(self, index=0):
        # struct v4l2_buffer.m.planes points to an array of struct v4l2_plane
        # in our own memory: the kernel reads and fills it during the ioctl.
        # array.buffer_info() gives its address without ctypes; the array is
        # kept in self._planes so it outlives the call and can be read back.
        self._planes = array("B", bytes(PLANE.size * self.num_planes))
        address = self._planes.buffer_info()[0]
        return bytearray(BUFFER.pack(index, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 0, 0, 0, 0, 0, b"",
                                     0, V4L2_MEMORY_MMAP, address, self.num_planes, 0, 0))

    def _plane(self, number=0):
        return PLANE.unpack_from(self._planes, PLANE.size * number)

    def _start_streaming(self):
        arg = bytearray(REQUESTBUFFERS.pack(self.cfg.buffers, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
                                            V4L2_MEMORY_MMAP, 0, 0))
        self._ioctl(self.fd, VIDIOC_REQBUFS, arg)
        count = REQUESTBUFFERS.unpack(arg)[0]
        if count < 2:
            raise CameraError("%s: only %d capture buffer(s) granted" % (self.video_path, count))
        for index in range(count):
            arg = self._buffer_arg(index)
            self._ioctl(self.fd, VIDIOC_QUERYBUF, arg)
            bytesused, length, m, data_offset = self._plane(0)
            # Only plane 0 carries the raw image (raw Bayer has one plane).
            self.buffers.append(self._map(self.fd, length, m & 0xFFFFFFFF))
        for index in range(count):
            self._qbuf(index)
        self._ioctl(self.fd, VIDIOC_STREAMON, bytearray(struct.pack("<I", V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)))

    def _qbuf(self, index):
        self._ioctl(self.fd, VIDIOC_QBUF, self._buffer_arg(index))
        self._queued.add(index)

    def _dqbuf(self):
        arg = self._buffer_arg()
        self._ioctl(self.fd, VIDIOC_DQBUF, arg)
        values = BUFFER.unpack(arg)
        index, flags, sec, usec, sequence = values[0], values[3], values[5], values[6], values[8]
        bytesused, length, m, data_offset = self._plane(0)
        self._queued.discard(index)
        return index, flags, bytesused, data_offset, sequence, sec + usec / 1e6

    # --- capture -------------------------------------------------------------

    def read(self, now=0.0):
        """Newest complete frame, or None. Older pending buffers are given
        back to the driver at once: only the latest frame matters, and
        falling behind would add latency to every event."""
        latest = None
        while True:
            try:
                index, flags, bytesused, data_offset, sequence, stamp = self._dqbuf()
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                if exc.errno == errno.EINTR:
                    continue
                raise CameraError("%s: capture failed: %s" % (self.video_path, exc.strerror or exc)) from exc
            if latest is not None:
                self._qbuf(latest[0])
                latest = None
            # Corrupted or short frame: skip it. bytesused = 0 means the driver
            # did not fill it in; the buffer is then taken as complete.
            short = bytesused and bytesused - data_offset < self.fmt.frame_bytes
            if flags & V4L2_BUF_FLAG_ERROR or short:
                self._qbuf(index)
                continue
            latest = (index, data_offset, sequence, stamp)
        if latest is None:
            return None
        index, data_offset, sequence, stamp = latest
        try:
            raw, view = self.fmt.views(self.buffers[index], data_offset)
        except FormatError as exc:
            self._qbuf(index)
            raise CameraError(str(exc)) from None
        return Frame(self.fmt, raw, view, sequence, stamp, token=index)

    def release(self, frame):
        index = frame.token
        frame.release()
        if self.fd is not None and index is not None and index not in self._queued:
            try:
                self._qbuf(index)
            except OSError as exc:
                raise CameraError("%s: cannot requeue a buffer: %s" % (self.video_path, exc.strerror or exc)) from exc
