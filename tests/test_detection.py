import unittest

from udg_throw_line.calibration import compute
from udg_throw_line.detection import Detector, Evaluation, GeometryError, LineGeometry, LineTracker, Probe
from udg_throw_line.frame import FrameFormat, frame_from_bytes

from .support import make_cfg, quiet_logs, sim_frames


def evaluate(detector, fmt, data):
    frame = frame_from_bytes(fmt, data)
    try:
        return detector.evaluate(frame.view)
    finally:
        frame.release()


class GeometryTest(unittest.TestCase):
    def setUp(self):
        self.fmt = FrameFormat(640, 480, 1280, "raw16", "BGGR", 1023)

    def test_samples(self):
        geometry = LineGeometry(self.fmt, (0.1, 0.5, 0.9, 0.5), step=2, search=6)
        # Red plane 320 wide: from u = 31.5 to 287.5, 256 pixels, one sample every 2.
        self.assertAlmostEqual(geometry.length, 256.0)
        self.assertEqual(geometry.n, 129)
        self.assertAlmostEqual(geometry.t[-1], 1.0)
        self.assertTrue(all(geometry.inside))
        self.assertEqual(len(geometry.windows[0]), 13)
        # Horizontal line: the window runs down the image, one red row per step.
        rows = [index // 640 for index in geometry.windows[10]]
        self.assertEqual([b - a for a, b in zip(rows, rows[1:])], [2] * 12)
        self.assertEqual(geometry.point(0), (31.5, 119.5))
        self.assertEqual(geometry.point(0, 3), (31.5, 122.5))

    def test_border_samples_are_outside(self):
        geometry = LineGeometry(self.fmt, (0.1, 0.01, 0.9, 0.01), step=2, search=6)
        self.assertFalse(any(geometry.inside))
        with self.assertRaises(GeometryError):
            Probe(geometry, [i for i in range(geometry.n) if geometry.inside[i]])

    def test_too_short(self):
        with self.assertRaises(GeometryError):
            LineGeometry(self.fmt, (0.5, 0.5, 0.51, 0.5), step=2, search=6)


class DetectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet_logs()
        cls.cfg = make_cfg()
        off, cls.fmt = sim_frames(cls.cfg, lit=False, count=4)
        on, _ = sim_frames(cls.cfg, lit=True, count=4)
        cls.cal = compute(cls.fmt, off, on, cls.cfg, "sim")

    def detector(self, **values):
        cfg = make_cfg(**values)
        geometry = LineGeometry(self.fmt, self.cal.line, cfg.step, cfg.search)
        return Detector(geometry, self.cal.offsets, self.cal.contrasts, cfg)

    def frame(self, lit=True, feet=()):
        frames, _ = sim_frames(self.cfg, lit=lit, count=1, feet=feet, start=5.0)
        return frames[0]

    def test_measure_on_the_line(self):
        detector = self.detector()
        frame = frame_from_bytes(self.fmt, self.frame())
        try:
            offsets, contrasts = detector.probe.measure(frame.view)
        finally:
            frame.release()
        self.assertTrue(all(abs(o) <= 1 for o in offsets))
        self.assertGreater(min(contrasts), 300)    # intensity 0.5 x 1023 on a ~80 floor

    def test_clear(self):
        result = evaluate(self.detector(), self.fmt, self.frame())
        self.assertEqual(result.cls, "clear")
        self.assertEqual(result.runs, [])
        self.assertEqual(result.coverage, 0.0)

    def test_foot_is_a_crossing(self):
        result = evaluate(self.detector(), self.fmt, self.frame(feet=[(0.5, 0.1, 8)]))
        self.assertEqual(result.cls, "crossed")
        start, end, length = result.largest
        middle = (start + end) / 2
        self.assertAlmostEqual(middle, 0.5, delta=0.03)
        self.assertAlmostEqual(end - start, 0.1 * 0.84 / 0.81, delta=0.03)

    def test_light_sent_back_in_place_is_invisible(self):
        """Known limit, kept as a test: when the beam on the shoe shows up
        exactly where the floor line was (lift 0, same brightness), the line
        looks intact. Only parallax makes a foot visible: the camera must
        not look along the laser plane (doc/DETECTION.md, mise en place)."""
        result = evaluate(self.detector(), self.fmt, self.frame(feet=[(0.3, 0.1, 0)]))
        self.assertEqual(result.cls, "clear")

    def test_shoe_light_inside_the_window_shifts_the_peak(self):
        # The beam caught 4 pixels above the floor: still inside the +/-6
        # window, the peak moves by 4 > tolerance 2.
        result = evaluate(self.detector(), self.fmt, self.frame(feet=[(0.3, 0.1, 4)]))
        self.assertEqual(result.cls, "crossed")

    def test_narrow_object_is_ignored(self):
        result = evaluate(self.detector(), self.fmt, self.frame(feet=[(0.5, 0.004, 8)]))
        self.assertEqual(result.cls, "clear")

    def test_two_feet(self):
        result = evaluate(self.detector(), self.fmt, self.frame(feet=[(0.3, 0.08, 8), (0.7, 0.08, 8)]))
        self.assertEqual(result.cls, "crossed")
        self.assertEqual(len(result.runs), 2)

    def test_close_feet_merge_with_gap(self):
        feet = [(0.40, 0.06, 8), (0.465, 0.06, 8)]     # ~1 sample apart
        merged = evaluate(self.detector(gap=3), self.fmt, self.frame(feet=feet))
        split = evaluate(self.detector(gap=0), self.fmt, self.frame(feet=feet))
        self.assertEqual(len(merged.runs), 1)
        self.assertEqual(len(split.runs), 2)

    def test_blocked(self):
        result = evaluate(self.detector(), self.fmt, self.frame(feet=[(0.5, 0.8, 8)]))
        self.assertEqual(result.cls, "blocked")
        self.assertGreater(result.coverage, 0.6)
        result = evaluate(self.detector(), self.fmt, self.frame(lit=False))
        self.assertEqual(result.cls, "blocked")
        self.assertEqual(result.coverage, 1.0)

    def test_dead_samples_are_not_watched(self):
        contrasts = list(self.cal.contrasts)
        n = len(contrasts)
        for i in range(n // 2 - 10, n // 2 + 10):
            contrasts[i] = None
        cfg = make_cfg()
        geometry = LineGeometry(self.fmt, self.cal.line, cfg.step, cfg.search)
        detector = Detector(geometry, self.cal.offsets, contrasts, cfg)
        self.assertEqual(len(detector.ids), n - 20)
        result = evaluate(detector, self.fmt, self.frame(feet=[(0.5, 0.05, 8)]))
        self.assertEqual(result.cls, "clear")

    def test_no_usable_sample(self):
        cfg = make_cfg()
        geometry = LineGeometry(self.fmt, self.cal.line, cfg.step, cfg.search)
        with self.assertRaises(GeometryError):
            Detector(geometry, self.cal.offsets, [None] * geometry.n, cfg)

    def test_adaptation_is_bounded(self):
        cfg = make_cfg(adapt=0.5)
        geometry = LineGeometry(self.fmt, self.cal.line, cfg.step, cfg.search)
        # Reference at a quarter of the real contrast: tracking pulls it up,
        # but never beyond twice the calibrated value.
        quarter = [None if c is None else c / 4 for c in self.cal.contrasts]
        detector = Detector(geometry, self.cal.offsets, quarter, cfg)
        data = self.frame()
        for _ in range(30):
            evaluate(detector, self.fmt, data)
        for ref, initial in zip(detector.ref_contrasts, detector._initial):
            self.assertAlmostEqual(ref, 2 * initial, places=6)

    def test_no_adaptation_while_crossed(self):
        detector = self.detector(adapt=0.5)
        before = list(detector.ref_contrasts)
        evaluate(detector, self.fmt, self.frame(feet=[(0.5, 0.1, 8)]))
        self.assertEqual(detector.ref_contrasts, before)


def ev(cls, runs=(), coverage=0.0):
    runs = list(runs)
    return Evaluation(cls, runs, coverage, [], [])


class TrackerTest(unittest.TestCase):
    def test_sequence(self):
        tracker = LineTracker(confirm=2, release=3)
        self.assertEqual(tracker.feed(ev("clear"), 0.0), [("state", "clear"), ("seq", 1)])
        self.assertIsNone(tracker.feed(ev("crossed", [(0.4, 0.5, 13)]), 0.1))
        event = tracker.feed(ev("crossed", [(0.4, 0.5, 13), (0.8, 0.85, 7)]), 0.2)
        self.assertEqual(event, [("state", "crossed"), ("seq", 2), ("pos", "0.450"), ("from", "0.400"),
                                 ("to", "0.500"), ("runs", 2)])
        self.assertEqual(tracker.crossings, 1)
        self.assertIsNone(tracker.feed(ev("crossed", [(0.4, 0.5, 13)]), 0.3))
        self.assertIsNone(tracker.feed(ev("clear"), 0.4))
        self.assertIsNone(tracker.feed(ev("clear"), 0.5))
        event = tracker.feed(ev("clear"), 0.6)
        self.assertEqual(event, [("state", "clear"), ("seq", 3), ("duration", "0.40")])

    def test_single_bad_frame_is_ignored(self):
        tracker = LineTracker(confirm=2, release=3)
        tracker.feed(ev("clear"), 0.0)
        self.assertIsNone(tracker.feed(ev("crossed", [(0.4, 0.5, 13)]), 0.1))
        self.assertIsNone(tracker.feed(ev("clear"), 0.2))
        self.assertIsNone(tracker.feed(ev("crossed", [(0.4, 0.5, 13)]), 0.3))
        self.assertEqual(tracker.state, "clear")

    def test_blocked_and_back_to_crossed(self):
        tracker = LineTracker(confirm=1, release=1)
        tracker.feed(ev("clear"), 0.0)
        event = tracker.feed(ev("blocked", coverage=0.9), 0.1)
        self.assertEqual(event, [("state", "blocked"), ("seq", 2), ("coverage", "0.90")])
        event = tracker.feed(ev("crossed", [(0.1, 0.2, 13)]), 0.2)
        self.assertEqual(event[0], ("state", "crossed"))
        self.assertEqual(tracker.crossings, 1)

    def test_starting_on_the_line(self):
        tracker = LineTracker(confirm=2, release=3)
        self.assertIsNone(tracker.feed(ev("crossed", [(0.4, 0.5, 13)]), 0.0))
        self.assertEqual(tracker.feed(ev("crossed", [(0.4, 0.5, 13)]), 0.1)[0], ("state", "crossed"))

    def test_reverse_position(self):
        tracker = LineTracker(confirm=1, release=1, reverse=True)
        event = tracker.feed(ev("crossed", [(0.1, 0.3, 20)]), 0.0)
        self.assertEqual(event[2:5], [("pos", "0.800"), ("from", "0.700"), ("to", "0.900")])

    def test_reset_and_current(self):
        tracker = LineTracker(confirm=1, release=1)
        self.assertEqual(tracker.current("laser-off"), [("state", "unknown"), ("seq", 0), ("reason", "laser-off")])
        self.assertIsNone(tracker.reset(0.0, "camera"))
        tracker.feed(ev("crossed", [(0.1, 0.3, 20)]), 0.0)
        self.assertEqual(tracker.current()[0], ("state", "crossed"))
        self.assertIn(("from", "0.100"), tracker.current())
        event = tracker.reset(1.0, "laser-off")
        self.assertEqual(event, [("state", "unknown"), ("seq", 2), ("reason", "laser-off")])
        self.assertEqual(tracker.current("x"), [("state", "unknown"), ("seq", 2), ("reason", "laser-off")])
        self.assertEqual(tracker.feed(ev("clear"), 2.0), [("state", "clear"), ("seq", 3)])


if __name__ == "__main__":
    unittest.main()
