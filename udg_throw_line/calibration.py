"""Calibration: find the laser line in the image and record its reference.

Done on request (`calibrate`), with nobody near the line:

1. a few frames with the laser off, then a few with the laser on (the
   service switches it itself); averaging the red planes and subtracting
   leaves only the laser: red carpets, posters or lamps cancel out;
2. the line is located on that difference: one brightness peak per column
   (or per row for a line closer to vertical), then a robust straight-line
   fit (a few candidate lines through point pairs, the one with the most
   inliers, refined by least squares) - a reflection or a stray spot does not
   pull the line;
3. along the fitted line, every sample is measured on each laser-on frame
   (same code as the live detection) and each laser-off frame: the averages
   become the reference offset and contrast; samples too dim, too noisy or
   not much brighter than the floor texture are dropped ("dead").

A line can also be imposed (`calibrate line=x0,y0,x1,y1`) when the automatic
search fails; step 3 still measures it.

The result is kept in <data_dir>/calibration.conf (INI) and reloaded at
startup, as long as the camera format matches.
"""

import configparser
import io
import math
import operator
from dataclasses import dataclass, field

from .detection import GeometryError, LineGeometry, Probe
from .frame import frame_from_bytes, write_atomic

FORMAT_VERSION = 1
MIN_POINTS = 12
FIT_TOLERANCE = 1.5     # red-plane pixels


class CalibrationError(Exception):
    pass


@dataclass
class Calibration:
    signature: str
    line: tuple
    step: int
    offsets: list
    contrasts: list
    created: str = ""
    stats: dict = field(default_factory=dict)

    @property
    def samples(self):
        return len(self.offsets)

    @property
    def usable(self):
        return sum(1 for c in self.contrasts if c is not None)

    def line_text(self):
        return ",".join("%.4f" % v for v in self.line)

    def save(self, path):
        parser = configparser.RawConfigParser(interpolation=None)
        parser["calibration"] = {
            "version": str(FORMAT_VERSION),
            "signature": self.signature,
            "line": self.line_text(),
            "step": str(self.step),
            "created": self.created,
            "samples": str(self.samples),
            "offsets": " ".join("-" if v is None else "%.2f" % v for v in self.offsets),
            "contrasts": " ".join("-" if v is None else "%.1f" % v for v in self.contrasts),
        }
        parser["stats"] = {key: str(value) for key, value in self.stats.items()}
        buffer = io.StringIO()
        buffer.write("# udg-throw-line calibration, written by the service (`calibrate`).\n"
                     "# Delete it (or send `forget`) to go back to an uncalibrated state.\n\n")
        parser.write(buffer)
        write_atomic(path, buffer.getvalue().encode("utf-8"))

    @classmethod
    def load(cls, path):
        parser = configparser.RawConfigParser(interpolation=None)
        try:
            with open(path, encoding="utf-8") as handle:
                parser.read_file(handle, source=path)
            section = parser["calibration"]
            version = int(section.get("version", "0"))
            if version != FORMAT_VERSION:
                raise CalibrationError("%s: format version %d, expected %d" % (path, version, FORMAT_VERSION))
            line = tuple(float(v) for v in section["line"].split(","))
            if len(line) != 4:
                raise ValueError("line needs 4 values")

            def numbers(text):
                return [None if word == "-" else float(word) for word in text.split()]

            offsets = numbers(section["offsets"])
            contrasts = numbers(section["contrasts"])
            if len(offsets) != len(contrasts) or len(offsets) != int(section["samples"]):
                raise ValueError("offsets/contrasts/samples lengths differ")
            stats = dict(parser["stats"]) if parser.has_section("stats") else {}
            return cls(section["signature"], line, int(section["step"]), offsets, contrasts,
                       section.get("created", ""), stats)
        except CalibrationError:
            raise
        except (OSError, KeyError, ValueError, configparser.Error) as exc:
            raise CalibrationError("%s: %s" % (path, exc)) from None


# --- line search ----------------------------------------------------------------

def sum_red_planes(fmt, frames):
    """Per-pixel sum of the red planes of several frames (bytes copies)."""
    total = None
    for data in frames:
        frame = frame_from_bytes(fmt, data)
        try:
            plane = fmt.red_plane(frame.view)
        finally:
            frame.release()
        if total is None:
            total = plane
        else:
            total = [list(map(operator.add, a, b)) for a, b in zip(total, plane)]
    return total


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _peaks(lines, min_peak):
    """One (index, sub-pixel position) per line whose maximum reaches min_peak."""
    points = []
    for a, values in enumerate(lines):
        peak = max(values)
        if peak < min_peak:
            continue
        p = values.index(peak)
        lo = max(values[p - 1], 0.0) if p > 0 else 0.0
        hi = max(values[p + 1], 0.0) if p + 1 < len(values) else 0.0
        total = lo + peak + hi
        points.append((a, p + (hi - lo) / total if total > 0 else float(p)))
    return points


def _fit(points, tolerance):
    """Robust y = m x + c through (x, y) points. Returns (m, c, inliers) or None."""
    n = len(points)
    if n < 2:
        return None
    third = max(1, n // 3)
    stride = max(1, third // 8)
    best_count, best = 0, None
    for i in range(0, third, stride):
        x1, y1 = points[i]
        for j in range(n - 1, max(n - 1 - third, i), -stride):
            x2, y2 = points[j]
            if x2 == x1:
                continue
            m = (y2 - y1) / (x2 - x1)
            c = y1 - m * x1
            count = sum(1 for x, y in points if abs(y - (m * x + c)) <= tolerance)
            if count > best_count:
                best_count, best = count, (m, c)
    if best is None:
        return None
    for _ in range(3):
        m, c = best
        inliers = [(x, y) for x, y in points if abs(y - (m * x + c)) <= tolerance]
        k = len(inliers)
        if k < 2:
            break
        sx = sum(x for x, _ in inliers)
        sy = sum(y for _, y in inliers)
        sxx = sum(x * x for x, _ in inliers)
        sxy = sum(x * y for x, y in inliers)
        den = k * sxx - sx * sx
        if den == 0:
            break
        slope = (k * sxy - sx * sy) / den
        best = (slope, (sy - slope * sx) / k)
    m, c = best
    inliers = [(x, y) for x, y in points if abs(y - (m * x + c)) <= tolerance]
    return m, c, inliers


def find_line(on, off, min_peak):
    """Locate the laser line in red-plane sums `on` / `off` (already divided
    by their frame counts; `off` may be None when the laser cannot be
    switched off). Returns ((u0, v0, u1, v1), info)."""
    height = len(on)
    width = len(on[0])
    candidates = []
    for orientation in ("horizontal", "vertical"):
        if orientation == "horizontal":
            lines = [list(col) for col in zip(*on)]
            backs = [list(col) for col in zip(*off)] if off else None
            size = width
        else:
            lines = [list(row) for row in on]
            backs = [list(row) for row in off] if off else None
            size = height
        if backs is not None:
            diff = [[max(0.0, a - b) for a, b in zip(line, back)] for line, back in zip(lines, backs)]
        else:
            diff = []
            for line in lines:
                base = _median(line)
                diff.append([max(0.0, a - base) for a in line])
        points = _peaks(diff, min_peak)
        if len(points) < MIN_POINTS:
            candidates.append((len(points), orientation, None, "only %d bright points" % len(points)))
            continue
        fit = _fit(points, FIT_TOLERANCE)
        if fit is None:
            candidates.append((0, orientation, None, "no line through the bright points"))
            continue
        m, c, inliers = fit
        xs = [x for x, _ in inliers]
        a0, a1 = min(xs), max(xs)
        span = a1 - a0 + 1
        density = len(inliers) / span
        problem = None
        if len(inliers) < MIN_POINTS:
            problem = "only %d points on a line" % len(inliers)
        elif span < 0.15 * size:
            problem = "line spans only %d%% of the image" % (100 * span // size)
        elif density < 0.5:
            problem = "line too broken (%d%% of its length visible)" % (100 * density)
        if orientation == "horizontal":
            ends = (a0, m * a0 + c, a1, m * a1 + c)
        else:
            ends = (m * a0 + c, a0, m * a1 + c, a1)
        candidates.append((len(inliers), orientation, ends, problem,
                           {"orientation": orientation, "points": len(inliers), "density": "%.2f" % density}))
    valid = [item for item in candidates if item[2] is not None and item[3] is None]
    if not valid:
        reasons = "; ".join("%s: %s" % (item[1], item[3]) for item in candidates)
        raise CalibrationError("no laser line found (%s). Is the laser on and the line in view, "
                               "with nobody on it?" % reasons)
    best = max(valid, key=lambda item: item[0])
    return best[2], best[4]


def _trim(ends, amount):
    u0, v0, u1, v1 = ends
    length = math.hypot(u1 - u0, v1 - v0)
    if amount <= 0 or length <= 4 * amount:
        return ends
    du, dv = (u1 - u0) / length, (v1 - v0) / length
    return u0 + du * amount, v0 + dv * amount, u1 - du * amount, v1 - dv * amount


def _normalize(fmt, ends):
    u0, v0, u1, v1 = ends
    wr, hr = fmt.red_width, fmt.red_height

    def clamp(value):
        return min(1.0, max(0.0, value))
    return (clamp((u0 + 0.5) / wr), clamp((v0 + 0.5) / hr), clamp((u1 + 0.5) / wr), clamp((v1 + 0.5) / hr))


# --- reference -------------------------------------------------------------------

def _measure_frames(fmt, probe, frames):
    """Per sample id: list of (offset, contrast) over frames."""
    columns = [[] for _ in probe.ids]
    for data in frames:
        frame = frame_from_bytes(fmt, data)
        try:
            offsets, contrasts = probe.measure(frame.view)
        finally:
            frame.release()
        for j, pair in enumerate(zip(offsets, contrasts)):
            columns[j].append(pair)
    return columns


def compute(fmt, off_frames, on_frames, cfg, signature, line=None, created=""):
    """Build a Calibration from bytes copies of laser-off / laser-on frames.
    Raises CalibrationError with a message meant for the person installing
    the kiosk."""
    if not on_frames:
        raise CalibrationError("no frame captured")
    min_peak = cfg.min_contrast * fmt.max_value
    info = {}
    if line is None:
        n_on = len(on_frames)
        on = [[value / n_on for value in row] for row in sum_red_planes(fmt, on_frames)]
        off = None
        if off_frames:
            n_off = len(off_frames)
            off = [[value / n_off for value in row] for row in sum_red_planes(fmt, off_frames)]
        ends, info = find_line(on, off, min_peak)
        line = _normalize(fmt, _trim(ends, cfg.margin * cfg.step))
        info["mode"] = "auto"
    else:
        info["mode"] = "manual"
    try:
        geometry = LineGeometry(fmt, line, cfg.step, cfg.search)
    except GeometryError as exc:
        raise CalibrationError(str(exc)) from None
    inside = [i for i in range(geometry.n) if geometry.inside[i]]
    if not inside:
        raise CalibrationError("the line is too close to the image border to be measured")
    probe = Probe(geometry, inside)
    lit = _measure_frames(fmt, probe, on_frames)
    dark = _measure_frames(fmt, probe, off_frames) if off_frames else None
    offsets = [None] * geometry.n
    contrasts = [None] * geometry.n
    used = []
    for j, i in enumerate(inside):
        samples = lit[j]
        k = len(samples)
        mean_o = sum(o for o, _ in samples) / k
        var_o = sum((o - mean_o) ** 2 for o, _ in samples) / k
        mean_c = sum(c for _, c in samples) / k
        floor = sum(c for _, c in dark[j]) / len(dark[j]) if dark else 0.0
        if mean_c < min_peak:
            continue                       # too dim
        if dark and mean_c < cfg.snr * floor:
            continue                       # not much brighter than the floor texture
        if abs(mean_o) > cfg.search - 1:
            continue                       # peak at the window edge: not the line
        if math.sqrt(var_o) > cfg.tolerance:
            continue                       # peak jumps from frame to frame
        offsets[i] = mean_o
        contrasts[i] = mean_c
        used.append(mean_c)
    usable = len(used)
    fraction = usable / geometry.n
    if fraction < cfg.min_usable:
        raise CalibrationError(
            "only %d%% of the line is usable (%d of %d samples, %d%% required): line too dim or "
            "hidden, or exposure too short/long - check with `snapshot`"
            % (100 * fraction, usable, geometry.n, 100 * cfg.min_usable))
    info["usable"] = usable
    info["contrast"] = "%.3f" % (sum(used) / usable / fmt.max_value)
    info["frames"] = "%d+%d" % (len(off_frames or ()), len(on_frames))
    return Calibration(signature, tuple(line), cfg.step, offsets, contrasts, created, info)
