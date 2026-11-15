"""Simulated camera (`--simulate`): a synthetic Bayer scene with a laser line.

Lets the game be developed on a PC with the real protocol, and the whole
pipeline (calibration, detection, events) be tested without hardware. The
scene is a textured floor; the laser line is drawn on the red photosites
only while the laser output is lit (so calibration's laser-off/laser-on
difference works for real); `sim foot` puts a foot across the line for a
while: the line is hidden there and its light shows up `lift` pixels higher,
on the shoe, as it would for a real camera looking from the side.

Pure Python but cheap per frame: a copy of the prepared background, then a
few thousand writes along the line.
"""

import math
from array import array

from .frame import Frame, FrameFormat, site


class _XorShift:
    """Tiny deterministic noise source (no `random`: keeps python3-math out
    of the service's dependencies on the target)."""

    def __init__(self, seed=0x2545F491):
        self.state = seed & 0xFFFFFFFF or 1

    def next(self):
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.state = x
        return x


class SimCamera:
    kind = "sim"

    def __init__(self, cfg, laser, seed=1):
        self.cfg = cfg
        self.laser = laser
        self.fps = cfg.sim_fps
        self.fmt = FrameFormat(cfg.width, cfg.height, 2 * cfg.width, "raw16", "BGGR", 1023)
        self.state = "stopped"
        self.reason = ""
        self.signature = "%dx%d SIM hflip=%d vflip=%d" % (cfg.width, cfg.height, cfg.hflip, cfg.vflip)
        self.controls = {"exposure": 1000, "gain": 32}
        self.feet = []          # [t_from, t_to, until, shoe pixel list]
        self.sequence = 0
        self._rng = _XorShift(seed)
        self._next = 0.0
        self._base = None
        self._line = []         # (t, u, v, element index, added value)

    # --- camera interface ----------------------------------------------------

    def _prepare(self):
        if self._base is None:
            self._base = self._background()
            self._line = self._laser_pixels()

    def open(self, now=0.0):
        self._prepare()
        self.state = "streaming"
        self._next = now

    def close(self):
        if self.state == "streaming":
            self.state = "stopped"

    def fileno(self):
        return None

    def next_deadline(self, now):
        return self._next if self.state == "streaming" else float("inf")

    def describe(self):
        return "sim:%s,%gfps" % (self.fmt.describe(), self.fps)

    def set_controls(self, exposure=None, gain=None):
        if exposure is not None:
            self.controls["exposure"] = exposure
        if gain is not None:
            self.controls["gain"] = gain
        return dict(self.controls)

    def read(self, now):
        if self.state != "streaming" or now < self._next:
            return None
        period = 1.0 / self.fps
        self._next += period
        if self._next < now:          # fell behind: do not try to catch up
            self._next = now + period
        self.sequence += 1
        data = self._render(now)
        raw, view = self.fmt.views(data)
        return Frame(self.fmt, raw, view, self.sequence, now)

    def release(self, frame):
        frame.release()

    # --- scene ---------------------------------------------------------------

    def add_foot(self, pos, width, duration, lift, now):
        self._prepare()
        lo = max(0.0, pos - width / 2)
        hi = min(1.0, pos + width / 2)
        until = now + duration if duration > 0 else float("inf")
        shoe = []
        for t, u, v, index, value in self._line:
            if lo <= t <= hi:
                # The shoe catches the beam above the floor: seen `lift`
                # red-plane pixels up (towards the top of the image).
                su = u + self._nu * -lift
                sv = v + self._nv * -lift
                iu, iv = math.floor(su + 0.5), math.floor(sv + 0.5)
                if 0 <= iu < self.fmt.red_width and 0 <= iv < self.fmt.red_height:
                    shoe.append((self.fmt.index(iu, iv), value))
        self.feet.append([lo, hi, until, shoe])
        return lo, hi

    def clear_feet(self):
        self.feet = []

    def _background(self):
        """Textured floor, dim: red ~50-80, green ~70-100, blue ~40-60 (10 bits)."""
        fmt = self.fmt
        width, height = fmt.width, fmt.height
        level = {}
        for color, base in (("R", 50), ("G", 70), ("B", 40)):
            for which in ((0, 1) if color == "G" else (0,)):
                x, y = site(fmt.bayer, color, which)
                level[(x, y)] = base
        data = array("H")
        for y in range(height):
            cy = y & 1
            row = [level[(x & 1, cy)] + ((x * 7 + y * 13) % 31) for x in range(width)]
            data.extend(row)
        return data.tobytes()

    def _laser_pixels(self):
        fmt = self.fmt
        wr, hr = fmt.red_width, fmt.red_height
        x0, y0, x1, y1 = self.cfg.sim_line
        u0, v0 = x0 * wr - 0.5, y0 * hr - 0.5
        u1, v1 = x1 * wr - 0.5, y1 * hr - 0.5
        length = math.hypot(u1 - u0, v1 - v0)
        tu, tv = (u1 - u0) / length, (v1 - v0) / length
        self._nu, self._nv = -tv, tu
        peak = self.cfg.sim_intensity * fmt.max_value
        pixels = []
        horizontal = abs(u1 - u0) >= abs(v1 - v0)
        if horizontal:
            steps = range(int(math.ceil(min(u0, u1))), int(math.floor(max(u0, u1))) + 1)
        else:
            steps = range(int(math.ceil(min(v0, v1))), int(math.floor(max(v0, v1))) + 1)
        for a in steps:
            if horizontal:
                t = (a - u0) / (u1 - u0)
                center = v0 + t * (v1 - v0)
            else:
                t = (a - v0) / (v1 - v0)
                center = u0 + t * (u1 - u0)
            for d in (-1, 0, 1):
                b = math.floor(center + 0.5) + d
                weight = math.exp(-((b - center) / 0.8) ** 2)
                u, v = (a, b) if horizontal else (b, a)
                if 0 <= u < wr and 0 <= v < hr:
                    pixels.append((t, u, v, fmt.index(u, v), int(peak * weight)))
        return pixels

    def _render(self, now):
        data = bytearray(self._base)
        if not self.laser.lit:
            return data
        view = memoryview(data).cast("H")
        top = self.fmt.max_value
        amplitude = int(self.cfg.sim_noise * top)
        span = 2 * amplitude + 1
        rng = self._rng
        self.feet = [foot for foot in self.feet if foot[2] > now]
        hidden = [(foot[0], foot[1]) for foot in self.feet]
        for t, u, v, index, value in self._line:
            if hidden and any(lo <= t <= hi for lo, hi in hidden):
                continue
            noisy = view[index] + value + (rng.next() % span - amplitude if span > 1 else 0)
            view[index] = min(top, max(0, noisy))
        for foot in self.feet:
            for index, value in foot[3]:
                view[index] = min(top, view[index] + value)
        view.release()
        return data
