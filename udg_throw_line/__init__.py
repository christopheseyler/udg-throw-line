"""udg-throw-line: throw-line watcher of the Ultimate Darts Game kiosk.

Owns the CSI camera (OV5647 on the RK3588 CIF, raw Bayer) and the GPIO that
switches the laser line drawn on the floor. The camera films that red line;
a foot that crosses it hides or displaces part of it, and the service tells
the game (Godot) over local UDP. See doc/PROTOCOLE.md.
"""

VERSION = "0.1.0"

# Major version of the UDP protocol. `ping` announces it so the game can
# check that it talks to a service it understands (the service lives in the
# read-only image, the game in an A/B slot updated on its own).
API_VERSION = 1

DEFAULT_ADDRESS = "127.0.0.1"
# udg-leds-service uses 47800; leave it some room.
DEFAULT_PORT = 47810
