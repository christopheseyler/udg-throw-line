"""Raw Bayer frames: pixel addressing, red plane, snapshot rendering.

The laser line is red, so the detection only reads the red photosites of the
Bayer mosaic: one per 2x2 cell. Everything works in "red-plane" coordinates
(u, v), the cell column and row (half the frame resolution); (u, v) is the red
photosite at frame pixel (2u + rx, 2v + ry), where (rx, ry) depends on the
Bayer order. Reading raw red values (no demosaicing, no ISP) keeps the laser
contrast intact and costs one memory access per sample.

Supported memory layouts (chosen by the camera from the negotiated format):

- raw16:  one little-endian 16-bit word per pixel (rkcif "word" memory modes,
          V4L2 SBGGR10 & co. with bytesperline >= 2 x width);
- raw8:   one byte per pixel (SBGGR8 & co.);
- mipi10: MIPI CSI-2 RAW10 packing, 4 pixels in 5 bytes (the 8 high bits of
          each pixel, then one byte with the four pairs of low bits). Only the
          high bytes are read: 8 bits are plenty for a contrast ratio.

Values are only ever compared with each other (ratios against the calibrated
reference), so their scale does not matter; `max_value` is the full scale of
the layout, used for the few absolute thresholds and the snapshot.
"""

import math
import operator
import os
import struct

LAYOUTS = ("raw16", "raw8", "mipi10")
BAYERS = ("BGGR", "GBRG", "GRBG", "RGGB")


def site(bayer, color, which=0):
    """(x, y) of a color in the 2x2 cell of `bayer` ("BGGR"...); `which`
    picks the first or second green."""
    positions = [i for i, c in enumerate(bayer) if c == color]
    index = positions[which]
    return index % 2, index // 2


class FormatError(ValueError):
    pass


class FrameFormat:
    def __init__(self, width, height, stride, layout, bayer, max_value):
        if layout not in LAYOUTS:
            raise FormatError("unknown layout %r" % layout)
        if bayer not in BAYERS:
            raise FormatError("unknown Bayer order %r" % bayer)
        if width < 4 or height < 4 or width % 2 or height % 2:
            raise FormatError("frame size %dx%d: width and height must be even, at least 4" % (width, height))
        need = {"raw16": 2 * width, "raw8": width, "mipi10": (width * 10 + 7) // 8}[layout]
        if stride < need:
            raise FormatError("bytesperline %d too small for %d %s pixels (need %d)" % (stride, width, layout, need))
        if layout == "raw16" and stride % 2:
            raise FormatError("bytesperline %d is odd for 16-bit pixels" % stride)
        if layout == "mipi10" and width % 4:
            raise FormatError("MIPI RAW10 width %d is not a multiple of 4" % width)
        self.width = width
        self.height = height
        self.stride = stride
        self.layout = layout
        self.bayer = bayer
        self.max_value = max_value
        self.red_width = width // 2
        self.red_height = height // 2
        self.rx, self.ry = site(bayer, "R")
        # Index unit of the view returned by view(): 16-bit words or bytes.
        self.row_elements = stride // 2 if layout == "raw16" else stride
        self.frame_bytes = stride * height

    def describe(self):
        return "%dx%d,%s,%s" % (self.width, self.height, self.bayer, self.layout)

    def __eq__(self, other):
        return isinstance(other, FrameFormat) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    def key(self):
        return (self.width, self.height, self.stride, self.layout, self.bayer, self.max_value)

    # --- views -----------------------------------------------------------------

    def views(self, buffer, offset=0):
        """Returns (raw, view): `raw` is the byte memoryview of the frame,
        `view` the one to index with index() (16-bit words for raw16). Both
        must be released (release_views) before the buffer can be unmapped."""
        base = memoryview(buffer)
        raw = base[offset:offset + self.frame_bytes]
        base.release()
        if len(raw) < self.frame_bytes:
            size = len(raw)
            raw.release()
            raise FormatError("frame has %d bytes, %s needs %d" % (size, self.describe(), self.frame_bytes))
        if self.layout == "raw16":
            return raw, raw.cast("H")
        return raw, raw

    # --- addressing ------------------------------------------------------------

    def index(self, u, v, cx=None, cy=None):
        """Element index (in the view) of the photosite (cx, cy) of cell (u, v);
        red by default."""
        if cx is None:
            cx, cy = self.rx, self.ry
        x = 2 * u + cx
        y = 2 * v + cy
        if self.layout == "mipi10":
            return y * self.stride + (x >> 2) * 5 + (x & 3)
        return y * self.row_elements + x

    def plane_row(self, view, v, cx, cy):
        """Values of photosite (cx, cy) along cell row v, as a list (C-speed slices)."""
        width = self.red_width
        y = 2 * v + cy
        if self.layout == "mipi10":
            start = y * self.stride
            end = start + (self.width // 4) * 5
            # Pixels 4k+cx and 4k+2+cx are the bytes 5k+cx and 5k+2+cx.
            even = view[start + cx:end:5].tolist()
            odd = view[start + cx + 2:end:5].tolist()
            row = [0] * width
            row[0::2] = even[:(width + 1) // 2]
            row[1::2] = odd[:width // 2]
            return row
        start = y * self.row_elements + cx
        return view[start:start + 2 * width:2].tolist()

    def red_row(self, view, v):
        return self.plane_row(view, v, self.rx, self.ry)

    def red_plane(self, view):
        return [self.red_row(view, v) for v in range(self.red_height)]


class Frame:
    """One captured frame. `raw` / `view` are only valid until release()."""

    def __init__(self, fmt, raw, view, sequence=0, timestamp=0.0, token=None):
        self.fmt = fmt
        self.raw = raw
        self.view = view
        self.sequence = sequence
        self.timestamp = timestamp
        self.token = token   # camera-private (buffer index)

    def copy_bytes(self):
        return bytes(self.raw)

    def release(self):
        for mv in (self.view, self.raw):
            if mv is not None:
                try:
                    mv.release()
                except ValueError:
                    pass
        self.view = self.raw = None


def frame_from_bytes(fmt, data, sequence=0):
    raw, view = fmt.views(data)
    return Frame(fmt, raw, view, sequence)


# --- snapshot ----------------------------------------------------------------

GREEN = (0, 255, 0)
YELLOW = (255, 255, 0)
MAGENTA = (255, 0, 255)
CYAN = (0, 200, 255)


def render_rgb(fmt, view):
    """Half-resolution color image: one RGB pixel per 2x2 Bayer cell (red,
    mean of the greens, blue), stretched up to 16x when the frame is dark
    (short exposures meant for the laser). Returns a list of bytearrays."""
    w, h = fmt.red_width, fmt.red_height
    r_site = site(fmt.bayer, "R")
    b_site = site(fmt.bayer, "B")
    g1_site = site(fmt.bayer, "G", 0)
    g2_site = site(fmt.bayer, "G", 1)
    planes = []
    peak = 1
    for v in range(h):
        r = fmt.plane_row(view, v, *r_site)
        g1 = fmt.plane_row(view, v, *g1_site)
        g2 = fmt.plane_row(view, v, *g2_site)
        b = fmt.plane_row(view, v, *b_site)
        planes.append((r, g1, g2, b))
        peak = max(peak, max(r), max(g1), max(g2), max(b))
    full = fmt.max_value
    scale = 255.0 / max(peak, full / 16.0)
    lut = bytes(min(255, int(i * scale + 0.5)) for i in range(full + 1))
    lut_pair = bytes(min(255, int(i * scale / 2 + 0.5)) for i in range(2 * full + 1))
    rows = []
    for r, g1, g2, b in planes:
        row = bytearray(3 * w)
        row[0::3] = bytes(map(lut.__getitem__, r))
        row[1::3] = bytes(map(lut_pair.__getitem__, map(operator.add, g1, g2)))
        row[2::3] = bytes(map(lut.__getitem__, b))
        rows.append(row)
    return rows


def draw_points(rows, points):
    """points: iterable of (u, v, (r, g, b)) in red-plane coordinates."""
    height = len(rows)
    width = len(rows[0]) // 3 if rows else 0
    for u, v, color in points:
        u = math.floor(u + 0.5)
        v = math.floor(v + 0.5)
        if 0 <= u < width and 0 <= v < height:
            rows[v][3 * u:3 * u + 3] = bytes(color)


def bmp_bytes(rows):
    """24-bit uncompressed BMP from top-down RGB rows. BMP because Godot reads
    it natively (Image.load_bmp_from_buffer) and every PC opens it."""
    height = len(rows)
    width = len(rows[0]) // 3
    row_size = (3 * width + 3) & ~3
    pad = bytes(row_size - 3 * width)
    pixels = bytearray()
    for row in reversed(rows):           # bottom-up
        bgr = bytearray(row)
        bgr[0::3] = row[2::3]
        bgr[2::3] = row[0::3]
        pixels += bgr
        pixels += pad
    header = struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
    # 2835 px/m = 72 dpi.
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return header + dib + bytes(pixels)


def write_atomic(path, data):
    """Write to a temporary file then rename it over `path`, so a reader (the
    game loading the snapshot, the service reloading its calibration) never
    sees a half-written file."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
