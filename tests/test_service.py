import os
import unittest

from udg_throw_line.calibration import Calibration
from udg_throw_line.laser import NullLaser, SimLaser
from udg_throw_line.service import Service
from udg_throw_line.simulator import SimCamera
from udg_throw_line.v4l2 import CameraError

from .support import Collector, TempDirTest, make_cfg

GAME = ("127.0.0.1", 40000)
OTHER = ("127.0.0.1", 40001)


class Harness:
    def __init__(self, test, simulate=True, laser=None, camera=None, **values):
        values.setdefault("data_dir", test.path("data"))
        values.setdefault("run_dir", test.path("run"))
        self.cfg = make_cfg(**values)
        self.laser = laser or SimLaser()
        self.camera = camera or SimCamera(self.cfg, self.laser)
        self.out = Collector()
        self.service = Service(self.cfg, self.camera, self.laser, self.out, simulate=simulate)
        self.now = 100.0
        self.service.start(self.now)

    def cmd(self, text, peer=GAME):
        return self.service.handle(text.encode("ascii"), peer, self.now)

    def run(self, seconds):
        end = self.now + seconds
        stuck = 0
        while self.now < end:
            deadline = min(end, self.service.next_deadline(self.now))
            if deadline <= self.now:
                stuck += 1
                if stuck > 100:
                    raise AssertionError("the service keeps asking to be woken up at %r" % self.now)
            else:
                stuck = 0
            self.now = max(self.now, deadline)
            self.service.on_camera_ready(self.now)
            self.service.tick(self.now)

    def events(self, peer=GAME, kind="event line"):
        return self.out.texts(peer, kind)

    def calibrate(self):
        self.assertNone(self.cmd("calibrate"))
        self.run(2.0)
        replies = self.out.texts(GAME, "ok calibrate") + self.out.texts(GAME, "err calibrate")
        return replies[-1]

    @staticmethod
    def assertNone(value):
        if value is not None:
            raise AssertionError("expected a deferred reply, got %r" % value)


class ServiceTest(TempDirTest):
    def ready(self, **values):
        h = Harness(self, **values)
        self.assertEqual(h.cmd("laser on"), "ok laser on")
        self.assertTrue(h.calibrate().startswith("ok calibrate"))
        return h

    def test_start_uncalibrated(self):
        h = Harness(self)
        self.assertEqual(h.camera.state, "streaming")
        self.assertEqual(h.cmd("ping"), "pong 0.1.0 api=1 laser=off camera=streaming calibrated=no watching=no")
        status = h.cmd("status")
        self.assertIn("calibrated=no", status)
        self.assertIn("state=unknown", status)
        self.assertEqual(h.cmd("laser on"), "ok laser on")
        self.assertIn("watching=no", h.cmd("status"))

    def test_calibrate_then_watch(self):
        h = Harness(self)
        h.cmd("laser on")
        reply = h.calibrate()
        self.assertRegex(reply, r"^ok calibrate line=[0-9.,]+ samples=\d+ usable=\d+ contrast=[0-9.]+ "
                                r"orientation=horizontal mode=auto$")
        self.assertTrue(os.path.exists(h.cfg.calibration_path))
        self.assertTrue(h.laser.lit)          # back to what the game asked
        status = h.cmd("status")
        self.assertIn("calibrated=yes", status)
        self.assertIn("watching=yes", status)
        h.run(0.5)
        self.assertIn("state=clear", h.cmd("status"))

    def test_calibration_restores_laser_off(self):
        h = Harness(self)
        self.assertTrue(h.calibrate().startswith("ok calibrate"))
        self.assertFalse(h.laser.lit)
        self.assertIn("watching=no", h.cmd("status"))

    def test_commands_during_calibration(self):
        h = Harness(self)
        self.assertIsNone(h.cmd("calibrate"))
        self.assertEqual(h.cmd("calibrate"), "err calibrate already running")
        self.assertEqual(h.cmd("laser on"), "ok laser on deferred=calibrating")
        self.assertIn("watching=no", h.cmd("status"))
        h.run(2.0)
        self.assertTrue(h.laser.lit)
        self.assertIn("watching=yes", h.cmd("status"))

    def test_events(self):
        h = self.ready()
        h.out.clear()
        h.cmd("subscribe")
        self.assertEqual(h.out.sent[0], ("ok subscribe ttl=10", GAME))
        self.assertEqual(h.out.sent[1][0], "event service laser=on camera=streaming calibrated=yes watching=yes")
        self.assertTrue(h.out.sent[2][0].startswith("event line state="))
        h.run(0.5)
        self.assertIn("state=clear", h.events()[-1])
        self.assertEqual(h.cmd("sim foot pos=0.5 width=0.1 duration=0.5"),
                         "ok sim foot from=0.450 to=0.550 duration=0.5")
        h.run(0.3)
        crossed = h.events()[-1]
        self.assertRegex(crossed, r"^event line state=crossed seq=\d+ pos=0\.[45]\d\d from=0\.4\d\d to=0\.5\d\d runs=1$")
        h.run(1.0)
        self.assertRegex(h.events()[-1], r"^event line state=clear seq=\d+ duration=0\.[5-7]\d$")
        self.assertIn("crossings=1", h.cmd("status"))

    def test_new_subscriber_learns_a_crossing_in_progress(self):
        h = self.ready()
        h.run(0.5)
        h.cmd("sim foot pos=0.3 duration=0")
        h.run(0.5)
        h.cmd("subscribe", OTHER)
        line = h.events(OTHER)[0]
        self.assertTrue(line.startswith("event line state=crossed"), line)
        self.assertIn("pos=", line)
        h.cmd("sim clear")
        h.run(0.5)
        self.assertTrue(h.events(OTHER)[-1].startswith("event line state=clear"))

    def test_laser_off_stops_watching(self):
        h = self.ready()
        h.cmd("subscribe")
        h.run(0.5)
        h.out.clear()
        self.assertEqual(h.cmd("laser off"), "ok laser off")
        self.assertRegex(h.events()[-1], r"^event line state=unknown seq=\d+ reason=laser-off$")
        self.assertEqual(h.events(kind="event service")[-1],
                         "event service laser=off camera=streaming calibrated=yes watching=no")
        h.cmd("laser on")
        h.run(0.5)
        self.assertTrue(h.events()[-1].startswith("event line state=clear"))

    def test_forget(self):
        h = self.ready()
        h.cmd("subscribe")
        h.run(0.5)
        self.assertEqual(h.cmd("forget"), "ok forget")
        self.assertFalse(os.path.exists(h.cfg.calibration_path))
        self.assertIn("reason=not-calibrated", h.events()[-1])
        self.assertIn("calibrated=no", h.cmd("status"))
        self.assertEqual(h.cmd("forget"), "ok forget")

    def test_calibration_survives_a_restart(self):
        self.ready()
        h = Harness(self)
        self.assertIn("calibrated=yes", h.cmd("status"))
        h.cmd("laser on")
        h.run(0.5)
        self.assertIn("state=clear", h.cmd("status"))

    def test_calibration_of_another_camera_mode_is_not_used(self):
        h = self.ready()
        cal = Calibration.load(h.cfg.calibration_path)
        cal.signature = "1296x972 GB10 hflip=0 vflip=0"
        cal.save(h.cfg.calibration_path)
        h2 = Harness(self)
        self.assertIn("calibrated=no", h2.cmd("status"))

    def test_failed_calibration(self):
        h = Harness(self, sim_intensity=0.01, sim_noise=0.0)
        reply = h.calibrate()
        self.assertTrue(reply.startswith("err calibrate no laser line found"), reply)
        self.assertIn("calibrated=no", h.cmd("status"))

    def test_calibration_without_switchable_laser(self):
        laser = NullLaser()
        laser.lit = True       # wired permanently on
        h = Harness(self, laser=laser)
        h.camera.laser = laser
        self.assertTrue(h.calibrate().startswith("ok calibrate"))

    def test_snapshot(self):
        h = self.ready()
        self.assertIsNone(h.cmd("snapshot"))
        h.run(0.1)
        reply = h.out.texts(GAME, "ok snapshot")[-1]
        self.assertEqual(reply, "ok snapshot path=%s size=320x240" % h.cfg.snapshot_path)
        with open(h.cfg.snapshot_path, "rb") as handle:
            self.assertEqual(handle.read(2), b"BM")

    def test_camera_controls(self):
        h = Harness(self)
        self.assertEqual(h.cmd("camera"), "ok camera exposure=1000 gain=32")
        self.assertEqual(h.cmd("camera exposure=200"), "ok camera exposure=200 gain=32")

    def test_sim_needs_simulate(self):
        h = Harness(self, simulate=False)
        self.assertEqual(h.cmd("sim clear"), "err sim only available with --simulate")

    def test_errors_echo_the_keyword(self):
        h = Harness(self)
        self.assertEqual(h.cmd("laser maybe"), "err laser expected on or off, got maybe")
        self.assertTrue(h.cmd("dance").startswith("err dance unknown command"))

    def test_subscription_expires(self):
        h = self.ready(subscription_ttl=1.0)
        h.cmd("subscribe")
        h.run(2.0)
        self.assertEqual(h.service.subscribers, {})
        h.out.clear()
        h.cmd("laser off")
        self.assertEqual(h.events(), [])

    def test_renewal_keeps_one_entry(self):
        h = Harness(self)
        h.cmd("subscribe")
        h.out.clear()
        h.cmd("subscribe")
        self.assertEqual(h.out.sent, [("ok subscribe ttl=10", GAME)])    # no state replay
        self.assertEqual(len(h.service.subscribers), 1)
        self.assertEqual(h.cmd("unsubscribe"), "ok unsubscribe")
        self.assertEqual(h.service.subscribers, {})

    def test_too_many_subscribers(self):
        h = Harness(self, max_subscribers=2)
        for port in (1, 2, 3):
            h.now += 1
            h.cmd("subscribe", ("127.0.0.1", port))
        self.assertEqual(sorted(h.service.subscribers), [("127.0.0.1", 2), ("127.0.0.1", 3)])

    def test_stalled_camera_is_restarted(self):
        h = self.ready()
        h.camera._next = float("inf")        # no more frames
        h.cmd("subscribe")
        h.out.clear()
        h.run(2.5)                                      # frame_timeout = 2 s
        self.assertIn("reason=camera", h.events()[0])
        self.assertEqual(h.camera.state, "error")
        h.run(1.0)                                      # retried 1 s later
        self.assertEqual(h.camera.state, "streaming")
        h.camera._next = h.now
        h.run(0.5)
        self.assertTrue(h.events()[-1].startswith("event line state=clear"))

    def test_missing_camera_is_retried(self):
        class Flaky(SimCamera):
            failures = 2

            def open(self, now=0.0):
                if self.failures:
                    self.failures -= 1
                    self.state = "absent"
                    self.reason = "no CIF capture node"
                    raise CameraError(self.reason, "absent")
                super().open(now)

        laser = SimLaser()
        cfg_values = dict(camera_retry=1.0)
        h = Harness(self, laser=laser, camera=Flaky(make_cfg(), laser), **cfg_values)
        self.assertEqual(h.cmd("calibrate"), "err calibrate camera not available (no CIF capture node)")
        self.assertTrue(h.cmd("snapshot").startswith("err snapshot camera not available"))
        h.run(2.5)
        self.assertEqual(h.camera.state, "streaming")

    def test_shutdown_switches_the_laser_off(self):
        h = self.ready()
        self.assertTrue(h.laser.lit)
        h.service.shutdown(h.now)
        self.assertFalse(h.laser.lit)
        self.assertEqual(h.camera.state, "stopped")


if __name__ == "__main__":
    unittest.main()
