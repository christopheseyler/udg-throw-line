"""Line watching: sample geometry, per-frame measure, crossing state machine.

Principle (details in doc/DETECTION.md). The calibrated laser line is cut into
samples every `step` red-plane pixels. Around each sample, a short window of
2 x `search` + 1 red photosites runs across the line (along its normal). With
nobody on the line, each window shows a bright peak at a known position
(the reference offset) with a known contrast (peak minus the darkest pixel
of the window, which cancels the ambient light).

A foot that crosses the plane of the laser either hides the line at that
place (shadow) or catches the beam higher up, where the camera sees it
shifted sideways. Either way the sample is "broken": its contrast falls
below `visibility` x reference, or its peak moves by more than `tolerance`.
A run of broken samples at least `min_width` of the line long is a crossing;
more than `max_width` of the line broken at once is "blocked" (camera
covered, laser fault, someone standing across the whole line).

Per frame this costs one operator.itemgetter call over all window pixels and
three C-level calls (max, index, min) per sample: well under a millisecond
for the ~150 samples of a 640x480 frame, so every frame can be checked.
"""

import math
import operator


class GeometryError(ValueError):
    pass


class LineGeometry:
    """Samples of a line given in normalized frame coordinates."""

    def __init__(self, fmt, line, step, search):
        self.fmt = fmt
        self.line = tuple(line)
        self.step = step
        self.search = search
        wr, hr = fmt.red_width, fmt.red_height
        x0, y0, x1, y1 = line
        # Normalized -> red-plane pixel centers.
        u0, v0 = x0 * wr - 0.5, y0 * hr - 0.5
        u1, v1 = x1 * wr - 0.5, y1 * hr - 0.5
        du, dv = u1 - u0, v1 - v0
        length = math.hypot(du, dv)
        if length < 4 * step:
            raise GeometryError("line too short: %.1f red-plane pixels for a step of %d" % (length, step))
        self.length = length
        self.tu, self.tv = du / length, dv / length       # along the line
        self.nu, self.nv = -self.tv, self.tu              # across (positive = image down for a left-right line)
        self.n = int(length // step) + 1
        self.t = [i * step / length for i in range(self.n)]
        self.centers = []
        self.windows = []
        self.inside = []
        for i in range(self.n):
            cu = u0 + self.tu * i * step
            cv = v0 + self.tv * i * step
            self.centers.append((cu, cv))
            window = []
            for k in range(-search, search + 1):
                # floor(x + 0.5), not round(): round() goes to the even
                # integer on .5, which reads one pixel twice and skips the
                # next when the line sits between two pixel rows.
                u = math.floor(cu + self.nu * k + 0.5)
                v = math.floor(cv + self.nv * k + 0.5)
                if not (0 <= u < wr and 0 <= v < hr):
                    window = None
                    break
                window.append(fmt.index(u, v))
            self.windows.append(window)
            self.inside.append(window is not None)

    def point(self, i, offset=0.0):
        """Red-plane (u, v) of sample i, moved `offset` pixels across the line."""
        cu, cv = self.centers[i]
        return cu + self.nu * offset, cv + self.nv * offset


class Probe:
    """Compiled measure of a subset of samples."""

    def __init__(self, geometry, ids):
        self.ids = list(ids)
        if not self.ids:
            raise GeometryError("no sample to measure")
        self.search = geometry.search
        self.width = 2 * geometry.search + 1
        flat = []
        for i in self.ids:
            flat.extend(geometry.windows[i])
        self._getter = operator.itemgetter(*flat)
        self._single = len(flat) == 1

    def measure(self, view):
        """Returns (offsets, contrasts), one per sample id: peak position
        relative to the line (red-plane pixels) and peak - minimum."""
        values = self._getter(view)
        if self._single:
            values = (values,)
        w = self.width
        s = self.search
        offsets = []
        contrasts = []
        append_o = offsets.append
        append_c = contrasts.append
        for j in range(0, len(values), w):
            window = values[j:j + w]
            peak = max(window)
            append_o(window.index(peak) - s)
            append_c(peak - min(window))
        return offsets, contrasts


class Evaluation:
    __slots__ = ("cls", "runs", "coverage", "broken", "ids", "largest")

    def __init__(self, cls, runs, coverage, broken, ids):
        self.cls = cls            # "clear", "crossed", "blocked"
        self.runs = runs          # [(t_from, t_to, n_samples)], qualifying runs, longest first
        self.coverage = coverage  # fraction of usable samples broken
        self.broken = broken      # per usable sample
        self.ids = ids
        self.largest = runs[0] if runs else None


class Detector:
    def __init__(self, geometry, offsets, contrasts, cfg):
        """`offsets` / `contrasts`: calibrated reference per sample, None for
        samples that are not usable."""
        ids = [i for i in range(geometry.n)
               if geometry.inside[i] and contrasts[i] is not None and offsets[i] is not None]
        if not ids:
            raise GeometryError("no usable sample on the calibrated line")
        self.geometry = geometry
        self.ids = ids
        self.probe = Probe(geometry, ids)
        self.ref_offsets = [float(offsets[i]) for i in ids]
        self.ref_contrasts = [float(contrasts[i]) for i in ids]
        self._initial = list(self.ref_contrasts)
        self.visibility = cfg.visibility
        self.tolerance = cfg.tolerance
        self.gap = cfg.gap
        self.max_width = cfg.max_width
        self.adapt_rate = cfg.adapt
        # Run length counted in samples of the whole line (dead samples
        # included), so min_width keeps its meaning of "fraction of the line".
        self.min_run = max(1, int(math.ceil(cfg.min_width * geometry.n - 1e-9)))
        self.last = None

    def evaluate(self, view):
        offsets, contrasts = self.probe.measure(view)
        vis = self.visibility
        tol = self.tolerance
        ref_o = self.ref_offsets
        ref_c = self.ref_contrasts
        broken = [c < vis * rc or abs(o - ro) > tol
                  for o, c, ro, rc in zip(offsets, contrasts, ref_o, ref_c)]
        count = broken.count(True)
        coverage = count / len(broken)
        runs = []
        if count:
            ids = self.ids
            t = self.geometry.t
            start = last = None
            spans = []
            for j, b in enumerate(broken):
                if not b:
                    continue
                if start is None:
                    start = j
                elif j - last - 1 > self.gap:
                    spans.append((start, last))
                    start = j
                last = j
            spans.append((start, last))
            for a, b in spans:
                length = ids[b] - ids[a] + 1
                if length >= self.min_run:
                    runs.append((t[ids[a]], t[ids[b]], length))
            runs.sort(key=lambda run: run[2], reverse=True)
        if coverage >= self.max_width:
            cls = "blocked"
        elif runs:
            cls = "crossed"
        else:
            cls = "clear"
            if self.adapt_rate:
                self._adapt(offsets, contrasts, broken)
        self.last = Evaluation(cls, runs, coverage, broken, self.ids)
        return self.last

    def _adapt(self, offsets, contrasts, broken):
        """Slow tracking of the reference (laser warming up, light level,
        a camera that settles by a pixel), only on frames where the line is
        clear and only for intact samples, so a foot is never learned.
        Contrast stays within 0.5x..2x of the calibrated value."""
        a = self.adapt_rate
        ref_o = self.ref_offsets
        ref_c = self.ref_contrasts
        initial = self._initial
        for j, b in enumerate(broken):
            if b:
                continue
            ref_o[j] += a * (offsets[j] - ref_o[j])
            c = ref_c[j] + a * (contrasts[j] - ref_c[j])
            ref_c[j] = min(2.0 * initial[j], max(0.5 * initial[j], c))


class LineTracker:
    """Debounced line state and the events it emits.

    States: unknown (not watching), clear, crossed, blocked.
    - unknown -> clear on the first clear frame;
    - clear/unknown -> crossed/blocked after `confirm` consecutive non-clear
      frames (the latest frame decides which);
    - crossed/blocked -> clear after `release` consecutive clear frames;
    - crossed <-> blocked after `confirm` consecutive frames of the other.
    """

    def __init__(self, confirm, release, reverse=False):
        self.confirm = confirm
        self.release = release
        self.reverse = reverse
        self.state = "unknown"
        self.seq = 0
        self.crossings = 0
        self.since = None
        self.fields = []
        self._clear_run = 0
        self._bad_run = 0
        self._other_run = 0

    def _event(self, state, now, fields):
        previous_since = self.since
        self.state = state
        self.seq += 1
        self.since = now
        # Kept to describe the current state to a new subscriber.
        self.fields = list(fields)
        event = [("state", state), ("seq", self.seq)]
        event.extend(fields)
        return event, previous_since

    def current(self, reason=None):
        """Fields describing the current state (for a new subscriber)."""
        fields = [("state", self.state), ("seq", self.seq)]
        if self.state == "unknown" and not self.fields:
            fields.append(("reason", reason or "starting"))
        else:
            fields.extend(self.fields)
        return fields

    def reset(self, now, reason):
        """Stop watching. Returns the event fields, or None if already unknown."""
        self._clear_run = self._bad_run = self._other_run = 0
        if self.state == "unknown":
            return None
        event, _ = self._event("unknown", now, [("reason", reason)])
        return event

    def _position(self, run):
        start, end = run[0], run[1]
        if self.reverse:
            start, end = 1.0 - end, 1.0 - start
        return [("pos", "%.3f" % ((start + end) / 2)), ("from", "%.3f" % start), ("to", "%.3f" % end)]

    def _bad_fields(self, evaluation):
        if evaluation.cls == "blocked":
            return [("coverage", "%.2f" % evaluation.coverage)]
        return self._position(evaluation.largest) + [("runs", len(evaluation.runs))]

    def feed(self, evaluation, now):
        """Returns the event fields (list of pairs) or None."""
        cls = evaluation.cls
        state = self.state
        if cls == "clear":
            self._bad_run = 0
            self._other_run = 0
            self._clear_run += 1
            if state == "unknown":
                return self._event("clear", now, [])[0]
            if state in ("crossed", "blocked") and self._clear_run >= self.release:
                event, since = self._event("clear", now, [])
                if since is not None:
                    event.append(("duration", "%.2f" % (now - since)))
                return event
            return None
        self._clear_run = 0
        if state in ("unknown", "clear"):
            self._bad_run += 1
            if self._bad_run >= self.confirm:
                self._bad_run = 0
                if cls == "crossed":
                    self.crossings += 1
                return self._event(cls, now, self._bad_fields(evaluation))[0]
            return None
        if cls == state:
            self._other_run = 0
            return None
        self._other_run += 1
        if self._other_run >= self.confirm:
            self._other_run = 0
            if cls == "crossed":
                self.crossings += 1
            return self._event(cls, now, self._bad_fields(evaluation))[0]
        return None
