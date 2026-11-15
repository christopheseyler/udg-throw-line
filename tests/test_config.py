import os
import unittest

from udg_throw_line import config
from udg_throw_line.config import ConfigError, parse_line

from .support import TempDirTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigLoadTest(TempDirTest):
    def test_defaults_without_files(self):
        cfg, messages = config.load(self.path("absent.conf"), self.path("data"))
        self.assertEqual(messages, [])
        self.assertEqual((cfg.width, cfg.height), (640, 480))
        self.assertEqual(cfg.port, 47810)
        self.assertEqual(cfg.laser_backend, "led")
        self.assertFalse(cfg.laser_startup)
        self.assertEqual(cfg.calibration_path, os.path.join(self.path("data"), "calibration.conf"))

    def test_shipped_file_matches_defaults(self):
        # conf/udg-throw-line.conf documents every key with its default value.
        cfg, messages = config.load(os.path.join(ROOT, "conf", "udg-throw-line.conf"), self.path("data"))
        self.assertEqual(messages, [])
        defaults = config.Config()
        for (section, key), (attr, _) in config.SCHEMA.items():
            if (section, key) == ("service", "data_dir"):
                continue
            self.assertEqual(getattr(cfg, attr), getattr(defaults, attr), "%s.%s" % (section, key))

    def test_data_layer_overrides_etc_key_by_key(self):
        etc = self.write("etc.conf", "[laser]\nbackend = gpio\nline = 4\n[detect]\nstep = 3\n")
        self.write("data/udg-throw-line.conf", "[detect]\nstep = 4\n[service]\ndata_dir = /elsewhere\n")
        cfg, messages = config.load(etc, self.path("data"))
        self.assertEqual(cfg.laser_backend, "gpio")
        self.assertEqual(cfg.laser_line, 4)
        self.assertEqual(cfg.step, 4)
        # data_dir cannot be moved from the file that lives in data_dir.
        self.assertEqual(cfg.data_dir, self.path("data"))
        self.assertEqual(messages, [])

    def test_bad_value_keeps_previous_layer(self):
        etc = self.write("etc.conf", "[camera]\nwidth = 1296\n")
        self.write("data/udg-throw-line.conf", "[camera]\nwidth = 641\nheight = banana\n")
        cfg, messages = config.load(etc, self.path("data"))
        self.assertEqual(cfg.width, 1296)
        self.assertEqual(cfg.height, 480)
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(level == "error" for level, _ in messages))
        self.assertIn("even", messages[0][1])

    def test_unknown_key_is_a_warning(self):
        etc = self.write("etc.conf", "[camera]\ncolour = red\n")
        cfg, messages = config.load(etc, self.path("data"))
        self.assertEqual(messages[0][0], "warning")

    def test_unparsable_file_is_ignored(self):
        etc = self.write("etc.conf", "no section header\n")
        cfg, messages = config.load(etc, self.path("data"))
        self.assertEqual(messages[0][0], "error")
        self.assertEqual(cfg.port, 47810)

    def test_command_line_wins(self):
        etc = self.write("etc.conf", "[network]\nport = 5000\n")
        cfg, _ = config.load(etc, self.path("data"), {("network", "port"): "6000"})
        self.assertEqual(cfg.port, 6000)
        self.assertIn("command line", cfg.sources)

    def test_loopback_only(self):
        etc = self.write("etc.conf", "[network]\naddress = 0.0.0.0\n")
        cfg, messages = config.load(etc, self.path("data"))
        self.assertEqual(cfg.address, "127.0.0.1")
        self.assertIn("loopback", messages[0][1])

    def test_gpio_backend_without_line_is_reported(self):
        etc = self.write("etc.conf", "[laser]\nbackend = gpio\n")
        cfg, messages = config.load(etc, self.path("data"))
        self.assertTrue(any("line" in text for _, text in messages))

    def test_laser_startup_words(self):
        for word, expected in (("on", True), ("yes", True), ("off", False), ("0", False)):
            etc = self.write("etc.conf", "[laser]\nstartup = %s\n" % word)
            cfg, messages = config.load(etc, self.path("data"))
            self.assertEqual(cfg.laser_startup, expected, word)
            self.assertEqual(messages, [])

    def test_max_width_must_exceed_min_width(self):
        etc = self.write("etc.conf", "[detect]\nmin_width = 0.3\nmax_width = 0.2\n")
        cfg, messages = config.load(etc, self.path("data"))
        self.assertGreater(cfg.max_width, cfg.min_width)
        self.assertTrue(messages)


class ParsersTest(unittest.TestCase):
    def test_parse_line(self):
        self.assertEqual(parse_line("0.1,0.5, 0.9,0.5"), (0.1, 0.5, 0.9, 0.5))
        for bad in ("0.1,0.5,0.9", "a,b,c,d", "0.1,0.5,1.2,0.5", "0.5,0.5,0.51,0.5"):
            with self.assertRaises(ConfigError, msg=bad):
                parse_line(bad)

    def test_parse_override(self):
        self.assertEqual(config.parse_override("detect.step=3"), (("detect", "step"), "3"))
        with self.assertRaises(ConfigError):
            config.parse_override("detect.nope=3")
        with self.assertRaises(ConfigError):
            config.parse_override("step=3")


if __name__ == "__main__":
    unittest.main()
