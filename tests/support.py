"""Shared test helpers: configuration, simulated frames, collecting sender."""

import os
import shutil
import tempfile
import unittest

from udg_throw_line import log
from udg_throw_line.config import Config
from udg_throw_line.laser import SimLaser
from udg_throw_line.simulator import SimCamera


class _Sink:
    def write(self, text):
        pass

    def flush(self):
        pass


def quiet_logs():
    log.configure(verbose=False, stream=_Sink(), journal=False)


def make_cfg(**values):
    cfg = Config()
    for key, value in values.items():
        if not hasattr(cfg, key):
            raise AttributeError(key)
        setattr(cfg, key, value)
    return cfg


def sim_frames(cfg, lit=True, count=1, feet=(), start=0.0):
    """Bytes copies of `count` simulated frames. feet: (pos, width, lift)."""
    laser = SimLaser()
    laser.lit = lit
    camera = SimCamera(cfg, laser)
    camera.open(start)
    for pos, width, lift in feet:
        camera.add_foot(pos, width, 0, lift, start)
    frames = []
    now = start
    while len(frames) < count:
        frame = camera.read(now)
        if frame is not None:
            frames.append(frame.copy_bytes())
            camera.release(frame)
        now += 1.0 / cfg.sim_fps
    return frames, camera.fmt


class TempDirTest(unittest.TestCase):
    def setUp(self):
        quiet_logs()
        self.tmp = tempfile.mkdtemp(prefix="udg-throw-line-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write(self, name, text):
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class Collector:
    """Stand-in for the UDP sender: records (text, peer)."""

    def __init__(self):
        self.sent = []

    def __call__(self, text, peer):
        self.sent.append((text, peer))

    def texts(self, peer=None, prefix=""):
        return [t for t, p in self.sent if (peer is None or p == peer) and t.startswith(prefix)]

    def clear(self):
        self.sent = []
