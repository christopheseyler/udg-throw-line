import os
import struct
import unittest
from array import array

from udg_throw_line.frame import (BAYERS, FormatError, FrameFormat, bmp_bytes, draw_points, frame_from_bytes,
                                  render_rgb, site, write_atomic)

from .support import TempDirTest


def pixel_value(x, y):
    """Distinct 10-bit test value per pixel."""
    return (x * 7 + y * 131) % 1024


def make_raw16(width, height, stride):
    data = bytearray(stride * height)
    for y in range(height):
        row = array("H", [pixel_value(x, y) for x in range(width)]).tobytes()
        data[y * stride:y * stride + len(row)] = row
    return bytes(data)


def make_raw8(width, height, stride):
    data = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            data[y * stride + x] = pixel_value(x, y) >> 2
    return bytes(data)


def make_mipi10(width, height, stride):
    """MIPI CSI-2 RAW10: per 4 pixels, their 8 high bits then one byte of the
    four 2-bit remainders (pixel 0 in bits 1:0)."""
    data = bytearray(stride * height)
    for y in range(height):
        out = y * stride
        for x in range(0, width, 4):
            values = [pixel_value(x + i, y) for i in range(4)]
            for i, value in enumerate(values):
                data[out + i] = value >> 2
            data[out + 4] = sum((value & 3) << (2 * i) for i, value in enumerate(values))
            out += 5
    return bytes(data)


class SiteTest(unittest.TestCase):
    def test_red_positions(self):
        # 2x2 cell read row by row: BGGR = B G / G R -> red at (1, 1)...
        self.assertEqual(site("BGGR", "R"), (1, 1))
        self.assertEqual(site("GBRG", "R"), (0, 1))
        self.assertEqual(site("GRBG", "R"), (1, 0))
        self.assertEqual(site("RGGB", "R"), (0, 0))
        self.assertEqual(site("BGGR", "G", 0), (1, 0))
        self.assertEqual(site("BGGR", "G", 1), (0, 1))
        self.assertEqual(site("GBRG", "B"), (1, 0))


class FormatTest(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(FormatError):
            FrameFormat(640, 480, 1000, "raw16", "BGGR", 1023)    # stride < 2 x width
        with self.assertRaises(FormatError):
            FrameFormat(641, 480, 1282, "raw16", "BGGR", 1023)    # odd width
        with self.assertRaises(FormatError):
            FrameFormat(640, 480, 1280, "raw12", "BGGR", 1023)
        with self.assertRaises(FormatError):
            FrameFormat(640, 480, 1280, "raw16", "RGBG", 1023)
        with self.assertRaises(FormatError):
            FrameFormat(642, 480, 1024, "mipi10", "BGGR", 255)    # width not a multiple of 4

    def check_layout(self, layout, builder, stride, scale):
        width, height = 16, 6
        for bayer in BAYERS:
            fmt = FrameFormat(width, height, stride, layout, bayer, 1023 if layout == "raw16" else 255)
            frame = frame_from_bytes(fmt, builder(width, height, stride))
            try:
                for v in range(fmt.red_height):
                    expected = [pixel_value(2 * u + fmt.rx, 2 * v + fmt.ry) >> scale for u in range(fmt.red_width)]
                    self.assertEqual(fmt.red_row(frame.view, v), expected, (layout, bayer, v))
                    for u in range(fmt.red_width):
                        self.assertEqual(frame.view[fmt.index(u, v)], expected[u])
                    for cx in (0, 1):
                        for cy in (0, 1):
                            row = fmt.plane_row(frame.view, v, cx, cy)
                            self.assertEqual(row, [pixel_value(2 * u + cx, 2 * v + cy) >> scale
                                                   for u in range(fmt.red_width)])
            finally:
                frame.release()

    def test_raw16(self):
        # Padded rows (bytesperline > 2 x width), as the CIF aligns them.
        self.check_layout("raw16", make_raw16, 48, 0)

    def test_raw8(self):
        self.check_layout("raw8", make_raw8, 24, 2)

    def test_mipi10_reads_the_high_bytes(self):
        self.check_layout("mipi10", make_mipi10, 32, 2)

    def test_short_buffer(self):
        fmt = FrameFormat(16, 6, 32, "raw16", "BGGR", 1023)
        with self.assertRaises(FormatError):
            fmt.views(bytes(100))

    def test_views_release_the_buffer(self):
        # A frame must not keep the buffer exported once released (an mmap
        # cannot be closed while a memoryview of it exists).
        fmt = FrameFormat(16, 6, 32, "raw16", "BGGR", 1023)
        buffer = bytearray(32 * 6)
        frame = frame_from_bytes(fmt, buffer)
        frame.release()
        buffer.extend(b"x")    # would raise BufferError if still exported


class SnapshotTest(TempDirTest):
    def test_bmp_layout(self):
        rows = [bytearray([255, 0, 0, 0, 255, 0, 0, 0, 255]),      # red, green, blue
                bytearray([10, 20, 30, 40, 50, 60, 70, 80, 90])]
        data = bmp_bytes(rows)
        self.assertEqual(data[:2], b"BM")
        size, _, _, offset = struct.unpack_from("<IHHI", data, 2)
        self.assertEqual(size, len(data))
        self.assertEqual(offset, 54)
        width, height, planes, bits = struct.unpack_from("<iiHH", data, 18)
        self.assertEqual((width, height, planes, bits), (3, 2, 1, 24))
        row_size = 12     # 9 bytes padded to 4
        bottom = data[54:54 + row_size]
        top = data[54 + row_size:54 + 2 * row_size]
        self.assertEqual(top[:9], bytes([0, 0, 255, 0, 255, 0, 255, 0, 0]))    # BGR order
        self.assertEqual(bottom[:9], bytes([30, 20, 10, 60, 50, 40, 90, 80, 70]))
        self.assertEqual(len(data), 54 + 2 * row_size)

    def test_render_and_draw(self):
        fmt = FrameFormat(16, 6, 32, "raw16", "BGGR", 1023)
        frame = frame_from_bytes(fmt, make_raw16(16, 6, 32))
        try:
            rows = render_rgb(fmt, frame.view)
        finally:
            frame.release()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(rows[0]), 3 * 8)
        draw_points(rows, [(1, 2, (1, 2, 3)), (100, 100, (9, 9, 9))])
        self.assertEqual(bytes(rows[2][3:6]), bytes([1, 2, 3]))

    def test_write_atomic(self):
        path = self.path("out.bin")
        write_atomic(path, b"one")
        write_atomic(path, b"two")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"two")
        self.assertEqual(os.listdir(self.tmp), ["out.bin"])


if __name__ == "__main__":
    unittest.main()
