"""The real service process (--simulate) driven over UDP by the CLI."""

import contextlib
import io
import os
import socket
import subprocess
import sys
import time
import unittest

from udg_throw_line import cli

from .support import TempDirTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class IntegrationTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "udg_throw_line", "--simulate", "--config", self.path("none.conf"),
             "--data-dir", self.path("data"), "--run-dir", self.path("run"), "--port", str(self.port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code, out = self.cli("ping")
            if code == 0:
                break
            time.sleep(0.1)
        else:
            self.fail("service did not answer: %s" % self.stop())

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            _, err = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err = self.proc.communicate()
        return err.decode("utf-8", "replace")

    def tearDown(self):
        self.stop()
        super().tearDown()

    def cli(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--host", "127.0.0.1", "--port", str(self.port), "--config", self.path("none.conf")]
                            + list(words))
        return code, out.getvalue().strip()

    def test_session(self):
        code, out = self.cli("ping")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("pong 0.1.0 api=1"))
        self.assertEqual(self.cli("laser", "on"), (0, "ok laser on"))
        code, out = self.cli("calibrate")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("ok calibrate line="))

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.2)
            sock.sendto(b"subscribe", ("127.0.0.1", self.port))
            seen = []

            def wait_for(prefix, seconds=4.0):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    try:
                        data, _ = sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    text = data.decode().strip()
                    seen.append(text)
                    if text.startswith(prefix):
                        return text
                self.fail("no %r in %r" % (prefix, seen))

            wait_for("ok subscribe ttl=")
            wait_for("event line state=clear")
            self.assertEqual(self.cli("sim", "foot", "pos=0.6", "duration=0.6")[0], 0)
            crossed = wait_for("event line state=crossed")
            self.assertIn("runs=1", crossed)
            cleared = wait_for("event line state=clear")
            self.assertIn("duration=", cleared)

        code, out = self.cli("snapshot")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.path("run", "snapshot.bmp")))
        self.assertEqual(self.cli("laser", "dim")[0], 1)
        err = self.stop()
        if os.name == "posix":
            # SIGTERM: clean stop, laser switched off. (Windows' terminate()
            # kills the process outright: nothing to check there.)
            self.assertEqual(self.proc.returncode, 0)
            self.assertIn("stopping: laser off", err)
        self.assertTrue(os.path.exists(self.path("data", "calibration.conf")))

    def test_no_service(self):
        self.stop()
        code, out = self.cli("ping")
        self.assertEqual((code, out), (1, ""))


class LaserOffTest(TempDirTest):
    def test_laser_off_mode(self):
        conf = self.write("none.conf", "[laser]\nbackend = none\n")
        result = subprocess.run([sys.executable, "-m", "udg_throw_line", "--config", conf, "--laser-off",
                                 "--data-dir", self.path("data")], cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
