import unittest

from udg_throw_line import protocol
from udg_throw_line.protocol import ProtocolError, parse


class ParseTest(unittest.TestCase):
    def error(self, data):
        with self.assertRaises(ProtocolError) as ctx:
            parse(data)
        return ctx.exception

    def test_simple_commands(self):
        for word in ("ping", "status", "subscribe", "unsubscribe", "forget", "snapshot"):
            self.assertEqual(parse(word.encode()).keyword, word)
            self.assertEqual(parse(word.upper().encode() + b"\n").keyword, word)
            exc = self.error(word.encode() + b" extra")
            self.assertEqual(exc.keyword, word)

    def test_laser(self):
        self.assertIs(parse(b"laser on").value, True)
        self.assertIs(parse(b"LASER Off").value, False)
        self.assertIs(parse(b"laser 1").value, True)
        self.assertEqual(self.error(b"laser").keyword, "laser")
        self.assertIn("on or off", str(self.error(b"laser blink")))
        self.error(b"laser on off")

    def test_calibrate(self):
        self.assertIsNone(parse(b"calibrate").value)
        self.assertEqual(parse(b"calibrate line=0.1,0.6,0.9,0.55").value, (0.1, 0.6, 0.9, 0.55))
        exc = self.error(b"calibrate line=0.1,0.6")
        self.assertEqual(exc.keyword, "calibrate")
        self.error(b"calibrate now")
        self.assertIn("unknown parameter", str(self.error(b"calibrate speed=2")))

    def test_camera(self):
        self.assertEqual(parse(b"camera").params, {})
        self.assertEqual(parse(b"camera exposure=300 gain=16").params, {"exposure": 300, "gain": 16})
        self.assertIn("integer", str(self.error(b"camera exposure=1.5")))
        self.error(b"camera exposure=0")

    def test_sim(self):
        command = parse(b"sim foot pos=0.4")
        self.assertEqual(command.value, "foot")
        self.assertEqual(command.params, {"pos": 0.4, "width": 0.1, "duration": 0.5, "lift": 8.0})
        self.assertEqual(parse(b"sim clear").value, "clear")
        self.assertIn("pos", str(self.error(b"sim foot width=0.2")))
        self.error(b"sim foot pos=2")
        self.error(b"sim jump")

    def test_parameters(self):
        self.assertIn("twice", str(self.error(b"camera gain=2 gain=3")))
        self.assertIn("key=value", str(self.error(b"camera gain=")))

    def test_bad_datagrams(self):
        self.assertEqual(self.error(b"").keyword, "?")
        self.assertEqual(self.error(b"   ").keyword, "?")
        self.assertIn("ASCII", str(self.error("laser é".encode("utf-8"))))
        self.assertIn("too long", str(self.error(b"x" * 2000)))
        exc = self.error(b"jump high")
        self.assertEqual(exc.keyword, "jump")
        self.assertIn("unknown command", str(exc))
        # Junk keywords are echoed in a printable, bounded form.
        exc = self.error(b"\x01\x02" + b"y" * 100)
        self.assertLessEqual(len(exc.keyword), 32)
        self.assertNotIn("\x01", exc.keyword)

    def test_format_fields(self):
        self.assertEqual(protocol.format_fields([("a", 1), ("b", "x")]), "a=1 b=x")


if __name__ == "__main__":
    unittest.main()
