import os
import unittest

from udg_throw_line.calibration import Calibration, CalibrationError, compute

from .support import TempDirTest, make_cfg, quiet_logs, sim_frames


def sim_y(line, x):
    x0, y0, x1, y1 = line
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


def sim_x(line, y):
    x0, y0, x1, y1 = line
    return x0 + (y - y0) * (x1 - x0) / (y1 - y0)


def add_red_blob(fmt, data, u0, v0, size, value):
    """Bright red patch (a red poster...) present in every frame."""
    out = bytearray(data)
    view = memoryview(out).cast("H")
    for v in range(v0, v0 + size):
        for u in range(u0, u0 + size):
            view[fmt.index(u, v)] = value
    view.release()
    return bytes(out)


class ComputeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet_logs()

    def frames(self, cfg, count=4, **kw):
        off, fmt = sim_frames(cfg, lit=False, count=count)
        on, _ = sim_frames(cfg, lit=True, count=count, **kw)
        return fmt, off, on

    def test_finds_the_simulated_line(self):
        cfg = make_cfg()
        fmt, off, on = self.frames(cfg)
        cal = compute(fmt, off, on, cfg, "sig", created="now")
        x0, y0, x1, y1 = cal.line
        # Within a red-plane pixel (1/240 of the height) of the drawn line...
        self.assertAlmostEqual(y0, sim_y(cfg.sim_line, x0), delta=1 / 240)
        self.assertAlmostEqual(y1, sim_y(cfg.sim_line, x1), delta=1 / 240)
        # ...trimmed by margin x step = 4 red pixels at each end.
        self.assertAlmostEqual(x0, cfg.sim_line[0] + 4.5 / 320, delta=2 / 320)
        self.assertAlmostEqual(x1, cfg.sim_line[2] - 4.5 / 320, delta=2 / 320)
        self.assertEqual(cal.usable, cal.samples)
        self.assertEqual(cal.stats["orientation"], "horizontal")
        self.assertEqual(cal.stats["mode"], "auto")
        self.assertEqual(cal.signature, "sig")
        self.assertEqual(cal.step, cfg.step)

    def test_vertical_line(self):
        cfg = make_cfg(sim_line=(0.5, 0.1, 0.42, 0.9))
        fmt, off, on = self.frames(cfg)
        cal = compute(fmt, off, on, cfg, "sig")
        self.assertEqual(cal.stats["orientation"], "vertical")
        x0, y0, x1, y1 = cal.line
        self.assertAlmostEqual(x0, sim_x(cfg.sim_line, y0), delta=1 / 320)
        self.assertAlmostEqual(x1, sim_x(cfg.sim_line, y1), delta=1 / 320)

    def test_red_object_cancels_out(self):
        cfg = make_cfg()
        fmt, off, on = self.frames(cfg)
        off = [add_red_blob(fmt, d, 200, 40, 20, 1000) for d in off]
        on = [add_red_blob(fmt, d, 200, 40, 20, 1000) for d in on]
        cal = compute(fmt, off, on, cfg, "sig")
        self.assertAlmostEqual(cal.line[1], sim_y(cfg.sim_line, cal.line[0]), delta=1 / 240)
        self.assertEqual(cal.usable, cal.samples)

    def test_without_laser_off_frames(self):
        cfg = make_cfg()
        fmt, off, on = self.frames(cfg)
        cal = compute(fmt, [], on, cfg, "sig")
        self.assertAlmostEqual(cal.line[1], sim_y(cfg.sim_line, cal.line[0]), delta=1 / 240)
        self.assertEqual(cal.stats["frames"], "0+4")

    def test_no_laser(self):
        cfg = make_cfg()
        off, fmt = sim_frames(cfg, lit=False, count=4)
        with self.assertRaises(CalibrationError) as ctx:
            compute(fmt, off, off, cfg, "sig")
        self.assertIn("no laser line found", str(ctx.exception))

    def test_dim_laser(self):
        cfg = make_cfg(sim_intensity=0.02, sim_noise=0.0)
        fmt, off, on = self.frames(cfg)
        with self.assertRaises(CalibrationError):
            compute(fmt, off, on, cfg, "sig")

    def test_partly_hidden_line(self):
        cfg = make_cfg()
        fmt, off, on = self.frames(cfg, feet=[(0.5, 0.1, 30)])
        cal = compute(fmt, off, on, cfg, "sig")
        dead = cal.samples - cal.usable
        self.assertGreater(dead, 0.08 * cal.samples)
        self.assertLess(dead, 0.15 * cal.samples)

    def test_manual_line(self):
        cfg = make_cfg()
        fmt, off, on = self.frames(cfg)
        cal = compute(fmt, off, on, cfg, "sig", line=cfg.sim_line)
        self.assertEqual(cal.stats["mode"], "manual")
        self.assertEqual(cal.line, cfg.sim_line)
        self.assertGreater(cal.usable, 0.95 * cal.samples)

    def test_manual_line_away_from_the_laser(self):
        cfg = make_cfg()
        fmt, off, on = self.frames(cfg)
        with self.assertRaises(CalibrationError) as ctx:
            compute(fmt, off, on, cfg, "sig", line=(0.1, 0.2, 0.9, 0.2))
        self.assertIn("usable", str(ctx.exception))

    def test_no_frames(self):
        cfg = make_cfg()
        with self.assertRaises(CalibrationError):
            compute(None, [], [], cfg, "sig")


class PersistenceTest(TempDirTest):
    def sample(self):
        return Calibration("640x480 GB10 hflip=0 vflip=0", (0.1, 0.6, 0.9, 0.55), 2,
                           [0.0, -0.5, None, 1.25], [300.0, 280.5, None, 310.0], "2026-11-15 12:00:00",
                           {"orientation": "horizontal", "contrast": "0.290"})

    def test_roundtrip(self):
        path = self.path("calibration.conf")
        self.sample().save(path)
        loaded = Calibration.load(path)
        self.assertEqual(loaded.signature, "640x480 GB10 hflip=0 vflip=0")
        self.assertEqual(loaded.line, (0.1, 0.6, 0.9, 0.55))
        self.assertEqual(loaded.offsets, [0.0, -0.5, None, 1.25])
        self.assertEqual(loaded.contrasts, [300.0, 280.5, None, 310.0])
        self.assertEqual(loaded.usable, 3)
        self.assertEqual(loaded.stats["orientation"], "horizontal")
        self.assertEqual(os.listdir(self.tmp), ["calibration.conf"])

    def test_bad_files(self):
        with self.assertRaises(CalibrationError):
            Calibration.load(self.path("absent.conf"))
        path = self.path("calibration.conf")
        self.sample().save(path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for broken in (text.replace("version = 1", "version = 9"),
                       text.replace("samples = 4", "samples = 5"),
                       text.replace("line = ", "line = 0.1,"),
                       "[other]\n"):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(broken)
            with self.assertRaises(CalibrationError, msg=broken[:40]):
                Calibration.load(path)


if __name__ == "__main__":
    unittest.main()
