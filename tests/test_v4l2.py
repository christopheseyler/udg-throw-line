"""V4L2 capture against a fake rkcif + OV5647.

The expected numbers come from a C program compiled against the Linux uapi
headers (linux/videodev2.h, v4l2-subdev.h, media-bus-format.h; sizes are the
same on aarch64 and x86_64):
    sizeof v4l2_capability 104, v4l2_format 208, v4l2_requestbuffers 20,
    v4l2_buffer 88, v4l2_plane 64, v4l2_control 8, v4l2_subdev_format 88;
    offsetof v4l2_buffer.m 64, .length 72; v4l2_plane.m 8;
    v4l2_pix_format_mplane.plane_fmt 20, .num_planes 180.
"""

import ctypes
import errno
import os
import struct
import unittest

from udg_throw_line import v4l2
from udg_throw_line.v4l2 import (BUFFER, CAPABILITY, CONTROL, PLANE, REQUESTBUFFERS, SUBDEV_FORMAT, CameraError,
                                 V4L2Camera, fourcc, fourcc_text)

from .support import TempDirTest, make_cfg


class AbiTest(unittest.TestCase):
    def test_struct_sizes(self):
        self.assertEqual(CAPABILITY.size, 104)
        self.assertEqual(v4l2.FORMAT_SIZE, 208)
        self.assertEqual(REQUESTBUFFERS.size, 20)
        self.assertEqual(BUFFER.size, 88)
        self.assertEqual(PLANE.size, 64)
        self.assertEqual(CONTROL.size, 8)
        self.assertEqual(SUBDEV_FORMAT.size, 88)
        self.assertEqual(v4l2._FMT_PLANES, 8 + 20)
        self.assertEqual(v4l2._FMT_NUM_PLANES, 8 + 180)

    def test_ioctl_numbers(self):
        expected = {
            "VIDIOC_QUERYCAP": 0x80685600, "VIDIOC_G_FMT": 0xC0D05604, "VIDIOC_S_FMT": 0xC0D05605,
            "VIDIOC_REQBUFS": 0xC0145608, "VIDIOC_QUERYBUF": 0xC0585609, "VIDIOC_QBUF": 0xC058560F,
            "VIDIOC_DQBUF": 0xC0585611, "VIDIOC_STREAMON": 0x40045612, "VIDIOC_STREAMOFF": 0x40045613,
            "VIDIOC_G_CTRL": 0xC008561B, "VIDIOC_S_CTRL": 0xC008561C,
            "VIDIOC_SUBDEV_G_FMT": 0xC0585604, "VIDIOC_SUBDEV_S_FMT": 0xC0585605,
            # rkcif-config.h, _IOR/_IOW('V', BASE_VIDIOC_PRIVATE + 0/1, int)
            "RKCIF_CMD_GET_CSI_MEMORY_MODE": 0x800456C0, "RKCIF_CMD_SET_CSI_MEMORY_MODE": 0x400456C1,
        }
        for name, value in expected.items():
            self.assertEqual(getattr(v4l2, name), value, name)

    def test_codes(self):
        self.assertEqual(fourcc("BG10"), 0x30314742)
        self.assertEqual(fourcc("GB10"), 0x30314247)
        self.assertEqual(fourcc("BA10"), 0x30314142)
        self.assertEqual(fourcc("RG10"), 0x30314752)
        self.assertEqual(fourcc("BA81"), 0x31384142)
        self.assertEqual(fourcc("pBAA"), 0x41414270)
        self.assertEqual(fourcc("pgAA"), 0x41416770)
        self.assertEqual(fourcc_text(0x30314247), "GB10")
        self.assertEqual(v4l2.MBUS_CODES[0x300E], ("GBRG", 10))
        self.assertEqual(v4l2.MBUS_CODES[0x3007], ("BGGR", 10))
        self.assertEqual((v4l2.V4L2_CID_EXPOSURE, v4l2.V4L2_CID_ANALOGUE_GAIN, v4l2.V4L2_CID_HFLIP),
                         (0x00980911, 0x009E0903, 0x00980914))


MODES = [(2592, 1944), (1920, 1080), (1296, 972), (640, 480)]
# ov5647_get_mbus_code(): index = hflip | vflip << 1
FLIP_CODES = [0x300E, 0x3007, 0x300F, 0x300A]


class FakeRkcif:
    """Just enough of rkcif + ov5647 for V4L2Camera."""

    VIDEO_FD = 901
    SUBDEV_FD = 902

    def __init__(self, memory_mode=True, caps=None, buffers=4):
        self.memory_mode_supported = memory_mode
        self.caps = caps if caps is not None else (v4l2.V4L2_CAP_VIDEO_CAPTURE_MPLANE | v4l2.V4L2_CAP_STREAMING)
        self.max_buffers = buffers
        self.controls = {v4l2.V4L2_CID_HFLIP: 0, v4l2.V4L2_CID_VFLIP: 0, v4l2.V4L2_CID_EXPOSURE: 1000,
                         v4l2.V4L2_CID_ANALOGUE_GAIN: 32, v4l2.V4L2_CID_EXPOSURE_AUTO: 1,
                         v4l2.V4L2_CID_AUTOGAIN: 0}
        self.sensor = (2592, 1944)
        self.mode = v4l2.CSI_LVDS_MEM_COMPACT
        self.fmt = None
        self.buffers = []
        self.queued = []
        self.done = []
        self.streaming = False
        self.sequence = 0
        self.calls = []

    # --- wiring ---
    def opener(self, path, flags):
        self.calls.append(("open", path))
        return self.SUBDEV_FD if "subdev" in path else self.VIDEO_FD

    def mapper(self, fd, length, offset):
        index = offset // 4096
        self.calls.append(("mmap", index, length))
        return self.buffers[index]

    def code(self):
        return FLIP_CODES[self.controls[v4l2.V4L2_CID_HFLIP] | self.controls[v4l2.V4L2_CID_VFLIP] << 1]

    def stride(self, width):
        if self.mode == v4l2.CSI_LVDS_MEM_COMPACT:
            return (width * 10 // 8 + 255) // 256 * 256
        return width * 2

    def produce(self, flags=0):
        index = self.queued.pop(0)
        self.sequence += 1
        struct.pack_into("<H", self.buffers[index], 0, self.sequence)
        self.done.append((index, flags, self.sequence))

    # --- ioctl ---
    def ioctl(self, fd, request, arg):
        names = {value: name for name, value in vars(v4l2).items()
                 if name.startswith(("VIDIOC_", "RKCIF_")) and isinstance(value, int)}
        self.calls.append(names.get(request, hex(request)))
        handler = getattr(self, "_" + names.get(request, "unknown").lower())
        return handler(fd, arg)

    def _unknown(self, fd, arg):
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    def _vidioc_querycap(self, fd, arg):
        arg[:] = CAPABILITY.pack(b"rkcif", b"rkcif-mipi-lvds2", b"platform:rkcif", 0,
                                 self.caps | v4l2.V4L2_CAP_DEVICE_CAPS, self.caps)

    def _write_fmt(self, arg):
        width, height, pixfmt = self.fmt
        stride = self.stride(width)
        struct.pack_into("<3I", arg, 8, width, height, pixfmt)
        struct.pack_into("<II", arg, v4l2._FMT_PLANES, stride * height, stride)
        arg[v4l2._FMT_NUM_PLANES] = 1

    def _vidioc_s_fmt(self, fd, arg):
        width, height, pixfmt = struct.unpack_from("<3I", arg, 8)
        # The CIF output is the sensor size (it only crops).
        self.fmt = (min(width, self.sensor[0]), min(height, self.sensor[1]), pixfmt)
        self._write_fmt(arg)

    def _vidioc_g_fmt(self, fd, arg):
        self._write_fmt(arg)

    def _rkcif_cmd_set_csi_memory_mode(self, fd, arg):
        if not self.memory_mode_supported:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        self.mode = struct.unpack("<i", arg)[0]

    def _vidioc_reqbufs(self, fd, arg):
        count, kind, memory, caps, flags = REQUESTBUFFERS.unpack(arg)
        count = min(count, self.max_buffers)
        if count == 0:
            self.buffers = []
            return
        width, height, _ = self.fmt
        self.buffers = [bytearray(self.stride(width) * height) for _ in range(count)]
        arg[:] = REQUESTBUFFERS.pack(count, kind, memory, 0, 0)

    def _planes(self, arg):
        values = BUFFER.unpack(arg)
        return values, values[10]      # m.planes

    def _vidioc_querybuf(self, fd, arg):
        values, address = self._planes(arg)
        index = values[0]
        plane = PLANE.pack(0, len(self.buffers[index]), index * 4096, 0)
        ctypes.memmove(address, plane, len(plane))

    def _vidioc_qbuf(self, fd, arg):
        values, _ = self._planes(arg)
        self.queued.append(values[0])

    def _vidioc_dqbuf(self, fd, arg):
        if not self.done:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        index, flags, sequence = self.done.pop(0)
        values, address = self._planes(arg)
        values = list(values)
        values[0], values[3], values[5], values[6], values[8] = index, flags, 12, 500000, sequence
        arg[:] = BUFFER.pack(*values)
        plane = PLANE.pack(len(self.buffers[index]), len(self.buffers[index]), index * 4096, 0)
        ctypes.memmove(address, plane, len(plane))

    def _vidioc_streamon(self, fd, arg):
        self.streaming = True

    def _vidioc_streamoff(self, fd, arg):
        self.streaming = False

    def _vidioc_s_ctrl(self, fd, arg):
        cid, value = CONTROL.unpack(arg)
        if cid not in self.controls:
            raise OSError(errno.EINVAL, "Invalid argument")
        self.controls[cid] = value

    def _vidioc_g_ctrl(self, fd, arg):
        cid, _ = CONTROL.unpack(arg)
        arg[:] = CONTROL.pack(cid, self.controls[cid])

    def _vidioc_subdev_g_fmt(self, fd, arg):
        values = list(SUBDEV_FORMAT.unpack(arg))
        values[2], values[3], values[4] = self.sensor[0], self.sensor[1], self.code()
        arg[:] = SUBDEV_FORMAT.pack(*values)

    def _vidioc_subdev_s_fmt(self, fd, arg):
        values = list(SUBDEV_FORMAT.unpack(arg))
        width, height = values[2], values[3]
        self.sensor = min(MODES, key=lambda m: abs(m[0] - width) + abs(m[1] - height))
        values[2], values[3], values[4] = self.sensor[0], self.sensor[1], self.code()
        arg[:] = SUBDEV_FORMAT.pack(*values)


class CameraTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.sysfs = self.path("sysfs")
        for entry, name in (("video0", "rkisp_mainpath"), ("video11", "stream_cif_mipi_id0"),
                            ("v4l-subdev2", "m00_b_ov5647 3-0036"), ("v4l-subdev0", "rkisp-isp-subdev")):
            self.write(os.path.join("sysfs", entry, "name"), name + "\n")

    def camera(self, fake, **cfg):
        return V4L2Camera(make_cfg(**cfg), ioctl=fake.ioctl, opener=fake.opener, mapper=fake.mapper,
                          sysfs=self.sysfs)

    def test_open_negotiates_640x480_raw16(self):
        fake = FakeRkcif()
        camera = self.camera(fake, exposure=300, gain=16)
        camera.open()
        self.assertEqual(camera.state, "streaming")
        self.assertEqual(camera.video_path, "/dev/video11")
        self.assertEqual(camera.subdev_path, "/dev/v4l-subdev2")
        fmt = camera.fmt
        self.assertEqual((fmt.width, fmt.height, fmt.stride, fmt.layout), (640, 480, 1280, "raw16"))
        # Default flips: the OV5647 reports SGBRG10, not its table's SBGGR10.
        self.assertEqual(fmt.bayer, "GBRG")
        self.assertEqual(fourcc_text(camera.pixelformat), "GB10")
        self.assertEqual(fmt.max_value, 1023)
        self.assertEqual(fake.mode, v4l2.CSI_LVDS_MEM_WORD_LOW_ALIGN)
        self.assertEqual(fake.sensor, (640, 480))
        self.assertEqual(fake.controls[v4l2.V4L2_CID_EXPOSURE], 300)
        self.assertEqual(fake.controls[v4l2.V4L2_CID_ANALOGUE_GAIN], 16)
        self.assertEqual(camera.controls, {"exposure": 300, "gain": 16})
        self.assertTrue(fake.streaming)
        self.assertEqual(len(fake.queued), 4)
        self.assertEqual(camera.signature, "640x480 GB10 hflip=0 vflip=0")
        self.assertEqual(camera.generation, 1)
        calls = [c for c in fake.calls if isinstance(c, str)]
        # Flips before the sensor format; memory mode between two S_FMT.
        self.assertLess(calls.index("VIDIOC_S_CTRL"), calls.index("VIDIOC_SUBDEV_S_FMT"))
        first = calls.index("VIDIOC_S_FMT")
        self.assertEqual(calls[first + 1], "RKCIF_CMD_SET_CSI_MEMORY_MODE")
        self.assertEqual(calls[first + 2], "VIDIOC_S_FMT")

    def test_flips_change_the_bayer_order(self):
        fake = FakeRkcif()
        camera = self.camera(fake, hflip=True)
        camera.open()
        self.assertEqual(camera.fmt.bayer, "BGGR")
        self.assertEqual(fourcc_text(camera.pixelformat), "BG10")

    def test_bayer_override(self):
        camera = self.camera(FakeRkcif(), bayer="rggb")
        camera.open()
        self.assertEqual(camera.fmt.bayer, "RGGB")

    def test_read_returns_the_newest_frame(self):
        fake = FakeRkcif()
        camera = self.camera(fake)
        camera.open()
        self.assertIsNone(camera.read())
        for _ in range(3):
            fake.produce()
        frame = camera.read()
        self.assertEqual(frame.sequence, 3)
        self.assertEqual(frame.view[0], 3)            # data of the 3rd buffer
        self.assertAlmostEqual(frame.timestamp, 12.5)
        self.assertEqual(len(fake.queued), 3)          # two older frames given back
        camera.release(frame)
        self.assertEqual(len(fake.queued), 4)
        self.assertIsNone(frame.view)

    def test_frames_with_the_error_flag_are_skipped(self):
        fake = FakeRkcif()
        camera = self.camera(fake)
        camera.open()
        fake.produce(flags=v4l2.V4L2_BUF_FLAG_ERROR)
        self.assertIsNone(camera.read())
        self.assertEqual(len(fake.queued), 4)

    def test_compact_layout_without_the_private_ioctl(self):
        fake = FakeRkcif(memory_mode=False)
        camera = self.camera(fake)
        camera.open()
        # 640 x 10 / 8 = 800 bytes, aligned to 256 by the CIF.
        self.assertEqual((camera.fmt.layout, camera.fmt.stride, camera.fmt.max_value), ("mipi10", 1024, 255))

    def test_missing_node(self):
        os.remove(self.path("sysfs", "video11", "name"))
        camera = self.camera(FakeRkcif())
        with self.assertRaises(CameraError) as ctx:
            camera.open()
        self.assertEqual(ctx.exception.state, "absent")
        self.assertEqual(camera.state, "absent")
        self.assertIsNone(camera.fileno())

    def test_node_without_multiplanar_capture(self):
        fake = FakeRkcif(caps=v4l2.V4L2_CAP_STREAMING | 1)
        camera = self.camera(fake)
        with self.assertRaises(CameraError) as ctx:
            camera.open()
        self.assertIn("multi-planar", str(ctx.exception))
        self.assertEqual(camera.state, "error")
        self.assertIsNone(camera.fd)

    def test_explicit_nodes_and_no_subdev(self):
        fake = FakeRkcif()
        fake.sensor = (640, 480)
        camera = self.camera(fake, video="/dev/video5", subdev="none")
        camera.open()
        self.assertEqual(camera.video_path, "/dev/video5")
        self.assertIsNone(camera.subdev_path)
        with self.assertRaises(CameraError):
            camera.set_controls(exposure=100)

    def test_close_stops_and_reopen_bumps_generation(self):
        fake = FakeRkcif()
        camera = self.camera(fake)
        camera.open()
        camera.close()
        self.assertFalse(fake.streaming)
        self.assertEqual(camera.state, "stopped")
        self.assertIsNone(camera.fd)
        camera.open()
        self.assertEqual(camera.generation, 2)

    def test_lost_device(self):
        fake = FakeRkcif()
        camera = self.camera(fake)
        camera.open()

        def gone(fd, arg):
            raise OSError(errno.ENODEV, "No such device")
        fake._vidioc_dqbuf = gone
        with self.assertRaises(CameraError):
            camera.read()


if __name__ == "__main__":
    unittest.main()
