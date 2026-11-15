"""Configuration: /etc/udg-throw-line.conf, then <data_dir>/udg-throw-line.conf, then CLI.

The /etc file is baked into the read-only image; the /data file lets one kiosk
override a few values (laser GPIO, exposure, thresholds...) without
reflashing. Each layer only needs the keys it changes.

A bad value never stops the service: it is reported and the previous layer's
value (or the default) is kept. With Restart=always, refusing to start would
only turn a typo into a restart loop and a blind throw line.
"""

import configparser
import ipaddress
import os
import re
from dataclasses import dataclass, field, fields

from . import DEFAULT_ADDRESS, DEFAULT_PORT

DEFAULT_ETC_PATH = "/etc/udg-throw-line.conf"
DEFAULT_DATA_DIR = "/data/udg-throw-line"
DEFAULT_RUN_DIR = "/run/udg-throw-line"
OVERRIDE_NAME = "udg-throw-line.conf"
CALIBRATION_NAME = "calibration.conf"
SNAPSHOT_NAME = "snapshot.bmp"

BAYER_CHOICES = ("auto", "bggr", "gbrg", "grbg", "rggb")
MEMORY_MODES = ("word_low", "word_high", "compact", "keep")
LASER_BACKENDS = ("led", "gpio", "none")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    # [camera]
    video: str = "auto"
    subdev: str = "auto"
    width: int = 640
    height: int = 480
    bayer: str = "auto"
    memory_mode: str = "word_low"
    buffers: int = 4
    hflip: bool = False
    vflip: bool = False
    exposure: int = 0        # 0 = leave the driver's value
    gain: int = 0            # 0 = leave the driver's value
    camera_retry: float = 5.0
    frame_timeout: float = 2.0
    # [laser]
    laser_backend: str = "led"
    laser_led: str = "udg-laser"
    laser_chip: str = "/dev/gpiochip1"
    laser_line: int = -1
    laser_active_low: bool = False
    laser_startup: bool = False
    # [detect]
    step: int = 2
    search: int = 6
    tolerance: float = 2.0
    visibility: float = 0.5
    min_width: float = 0.02
    gap: int = 1
    max_width: float = 0.6
    confirm_frames: int = 2
    release_frames: int = 5
    settle_frames: int = 4
    adapt: float = 0.002
    reverse_position: bool = False
    # [calibration]
    cal_frames: int = 8
    cal_settle_frames: int = 5
    min_contrast: float = 0.04
    snr: float = 3.0
    min_usable: float = 0.6
    margin: int = 2
    # [network]
    address: str = DEFAULT_ADDRESS
    port: int = DEFAULT_PORT
    subscription_ttl: float = 10.0
    max_subscribers: int = 8
    # [service]
    data_dir: str = DEFAULT_DATA_DIR
    run_dir: str = DEFAULT_RUN_DIR
    # [simulate]
    sim_fps: float = 30.0
    sim_line: tuple = (0.08, 0.62, 0.92, 0.55)
    sim_intensity: float = 0.5
    sim_noise: float = 0.02
    # Not configurable: where the values came from, for the startup log.
    sources: list = field(default_factory=list)

    @property
    def calibration_path(self):
        return os.path.join(self.data_dir, CALIBRATION_NAME)

    @property
    def snapshot_path(self):
        return os.path.join(self.run_dir, SNAPSHOT_NAME)


# --- value parsers -------------------------------------------------------

def _int_range(lo, hi):
    def parse(text):
        try:
            value = int(text.strip(), 10)
        except ValueError:
            raise ConfigError("expected an integer") from None
        if not lo <= value <= hi:
            raise ConfigError("expected %d..%d" % (lo, hi))
        return value
    return parse


def _float_range(lo, hi):
    def parse(text):
        try:
            value = float(text.strip())
        except ValueError:
            raise ConfigError("expected a number") from None
        if not lo <= value <= hi:  # also rejects nan
            raise ConfigError("expected %g..%g" % (lo, hi))
        return value
    return parse


_TRUE = ("1", "yes", "true", "on")
_FALSE = ("0", "no", "false", "off")


def parse_bool(text):
    value = text.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError("expected yes or no")


def _choice(choices):
    def parse(text):
        value = text.strip().lower()
        if value not in choices:
            raise ConfigError("expected one of " + ", ".join(choices))
        return value
    return parse


def _even_size(lo, hi):
    inner = _int_range(lo, hi)

    def parse(text):
        value = inner(text)
        if value % 2:
            # Bayer frames are processed in 2x2 cells.
            raise ConfigError("expected an even number")
        return value
    return parse


def _laser_state(text):
    value = text.strip().lower()
    if value in ("on",) + _TRUE:
        return True
    if value in ("off",) + _FALSE:
        return False
    raise ConfigError("expected on or off")


def _address(text):
    value = text.strip()
    try:
        # The unit file restricts the service to AF_INET: only IPv4 literals.
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise ConfigError("expected an IPv4 loopback address such as 127.0.0.1") from None
    if not address.is_loopback:
        # The protocol has no authentication: anyone who can reach the port
        # can switch the laser, so it never leaves the machine.
        raise ConfigError("only loopback addresses (127.x.x.x) are allowed: the protocol has no authentication")
    return value


def _path(text):
    value = text.strip()
    if not value:
        raise ConfigError("expected a path")
    return value


def _node(text):
    value = text.strip()
    if not value:
        raise ConfigError("expected auto or a /dev path")
    return "auto" if value.lower() == "auto" else value


def parse_line(text):
    """`x0,y0,x1,y1`, coordinates normalized to the frame (0..1)."""
    parts = text.replace(" ", "").split(",")
    if len(parts) != 4:
        raise ConfigError("expected x0,y0,x1,y1 (four numbers between 0 and 1)")
    try:
        values = tuple(float(part) for part in parts)
    except ValueError:
        raise ConfigError("expected x0,y0,x1,y1 (four numbers between 0 and 1)") from None
    if not all(0.0 <= v <= 1.0 for v in values):
        raise ConfigError("line coordinates are normalized: each must be between 0 and 1")
    x0, y0, x1, y1 = values
    if ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 < 0.05:
        raise ConfigError("the two ends of the line are too close")
    return values


# (section, key) -> (Config attribute, parser)
SCHEMA = {
    ("camera", "video"): ("video", _node),
    ("camera", "subdev"): ("subdev", _node),
    ("camera", "width"): ("width", _even_size(64, 4096)),
    ("camera", "height"): ("height", _even_size(64, 4096)),
    ("camera", "bayer"): ("bayer", _choice(BAYER_CHOICES)),
    ("camera", "memory_mode"): ("memory_mode", _choice(MEMORY_MODES)),
    ("camera", "buffers"): ("buffers", _int_range(2, 16)),
    ("camera", "hflip"): ("hflip", parse_bool),
    ("camera", "vflip"): ("vflip", parse_bool),
    ("camera", "exposure"): ("exposure", _int_range(0, 1_000_000)),
    ("camera", "gain"): ("gain", _int_range(0, 100_000)),
    ("camera", "retry"): ("camera_retry", _float_range(0.5, 3600)),
    ("camera", "frame_timeout"): ("frame_timeout", _float_range(0.2, 60)),
    ("laser", "backend"): ("laser_backend", _choice(LASER_BACKENDS)),
    ("laser", "led"): ("laser_led", _path),
    ("laser", "chip"): ("laser_chip", _path),
    ("laser", "line"): ("laser_line", _int_range(-1, 1023)),
    ("laser", "active_low"): ("laser_active_low", parse_bool),
    ("laser", "startup"): ("laser_startup", _laser_state),
    ("detect", "step"): ("step", _int_range(1, 16)),
    ("detect", "search"): ("search", _int_range(2, 40)),
    ("detect", "tolerance"): ("tolerance", _float_range(0.5, 20)),
    ("detect", "visibility"): ("visibility", _float_range(0.05, 0.95)),
    ("detect", "min_width"): ("min_width", _float_range(0.0, 0.5)),
    ("detect", "gap"): ("gap", _int_range(0, 20)),
    ("detect", "max_width"): ("max_width", _float_range(0.05, 1.0)),
    ("detect", "confirm_frames"): ("confirm_frames", _int_range(1, 100)),
    ("detect", "release_frames"): ("release_frames", _int_range(1, 1000)),
    ("detect", "settle_frames"): ("settle_frames", _int_range(0, 100)),
    ("detect", "adapt"): ("adapt", _float_range(0.0, 0.5)),
    ("detect", "reverse_position"): ("reverse_position", parse_bool),
    ("calibration", "frames"): ("cal_frames", _int_range(1, 64)),
    ("calibration", "settle_frames"): ("cal_settle_frames", _int_range(0, 100)),
    ("calibration", "min_contrast"): ("min_contrast", _float_range(0.001, 1.0)),
    ("calibration", "snr"): ("snr", _float_range(1.0, 100.0)),
    ("calibration", "min_usable"): ("min_usable", _float_range(0.05, 1.0)),
    ("calibration", "margin"): ("margin", _int_range(0, 50)),
    ("network", "address"): ("address", _address),
    ("network", "port"): ("port", _int_range(1, 65535)),
    ("network", "subscription_ttl"): ("subscription_ttl", _float_range(1, 3600)),
    ("network", "max_subscribers"): ("max_subscribers", _int_range(1, 64)),
    ("service", "data_dir"): ("data_dir", _path),
    ("service", "run_dir"): ("run_dir", _path),
    ("simulate", "fps"): ("sim_fps", _float_range(1, 240)),
    ("simulate", "line"): ("sim_line", parse_line),
    ("simulate", "intensity"): ("sim_intensity", _float_range(0.0, 1.0)),
    ("simulate", "noise"): ("sim_noise", _float_range(0.0, 0.5)),
}

_OVERRIDE_RE = re.compile(r"^([a-z_]+)\.([a-z0-9_]+)=(.*)$", re.S)


def parse_override(text):
    """Parse a `--set section.key=value` argument."""
    match = _OVERRIDE_RE.match(text.strip())
    if not match:
        raise ConfigError("expected section.key=value, got %r" % text)
    section, key, value = match.groups()
    if (section, key) not in SCHEMA:
        raise ConfigError("unknown setting %s.%s" % (section, key))
    return (section, key), value


def _new_parser():
    # "#" and ";" start a comment line; only " ;" starts an inline comment.
    # No interpolation: "%" is not special.
    return configparser.RawConfigParser(
        comment_prefixes=("#", ";"), inline_comment_prefixes=(";",), interpolation=None
    )


def _read_layer(path, messages):
    """Return the parser for one file, or None if it is absent or unreadable."""
    if not path or not os.path.isfile(path):
        return None
    parser = _new_parser()
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle, source=path)
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        messages.append(("error", "%s: ignored, cannot be parsed: %s" % (path, exc)))
        return None
    return parser


def _apply_layer(cfg, parser, origin, messages, skip=()):
    for section in parser.sections():
        for key, raw in parser.items(section):
            entry = SCHEMA.get((section, key))
            if entry is None:
                messages.append(("warning", "%s: unknown setting [%s] %s ignored" % (origin, section, key)))
                continue
            if (section, key) in skip:
                continue
            attr, parse = entry
            try:
                setattr(cfg, attr, parse(raw))
            except ConfigError as exc:
                messages.append(("error", "%s: [%s] %s = %r: %s; keeping %r"
                                 % (origin, section, key, raw, exc, getattr(cfg, attr))))


def finalize(cfg, messages):
    """Cross-field constraints."""
    if cfg.laser_backend == "gpio" and cfg.laser_line < 0:
        messages.append(("error", "[laser] backend = gpio needs [laser] line (offset on %s); "
                         "the laser cannot be switched" % cfg.laser_chip))
    if cfg.max_width <= cfg.min_width:
        messages.append(("error", "[detect] max_width (%g) must be above min_width (%g); using %g"
                         % (cfg.max_width, cfg.min_width, min(1.0, cfg.min_width * 4 + 0.05))))
        cfg.max_width = min(1.0, cfg.min_width * 4 + 0.05)
    return cfg


def load(etc_path=DEFAULT_ETC_PATH, data_dir=None, overrides=None, use_data_file=True):
    """Build the effective configuration.

    Returns (config, messages); messages are (level, text) tuples to log once
    logging is set up. `overrides` maps (section, key) to raw strings (CLI).
    """
    overrides = dict(overrides or {})
    messages = []
    cfg = Config()
    etc = _read_layer(etc_path, messages)
    if etc is not None:
        _apply_layer(cfg, etc, etc_path, messages)
        cfg.sources.append(etc_path)
    # The data directory decides where the second layer lives, so it can only
    # come from /etc or the command line, never from the /data file itself.
    if data_dir:
        cfg.data_dir = data_dir
    elif ("service", "data_dir") in overrides:
        try:
            cfg.data_dir = _path(overrides[("service", "data_dir")])
        except ConfigError:
            pass
    if use_data_file:
        data_path = os.path.join(cfg.data_dir, OVERRIDE_NAME)
        data = _read_layer(data_path, messages)
        if data is not None:
            _apply_layer(cfg, data, data_path, messages, skip={("service", "data_dir")})
            cfg.sources.append(data_path)
    for (section, key), raw in overrides.items():
        attr, parse = SCHEMA[(section, key)]
        try:
            setattr(cfg, attr, parse(raw))
        except ConfigError as exc:
            messages.append(("error", "command line: %s.%s = %r: %s" % (section, key, raw, exc)))
    if data_dir:
        cfg.data_dir = data_dir
    if overrides:
        cfg.sources.append("command line")
    return finalize(cfg, messages), messages


def describe(cfg):
    """One-line summary for the debug log."""
    skip = {"sources"}
    parts = ["%s=%s" % (f.name, getattr(cfg, f.name)) for f in fields(cfg) if f.name not in skip]
    return " ".join(parts)
