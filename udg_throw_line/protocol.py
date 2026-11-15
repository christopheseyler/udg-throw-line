"""Parsing of the UDP commands (pure functions, see doc/PROTOCOLE.md).

One datagram = one ASCII command line: a case-insensitive keyword, then
positional words and `key=value` parameters separated by spaces.

Every reply repeats the command keyword (`ok laser on`, `err calibrate ...`):
`calibrate` answers about a second later, so a client that sent other
commands meanwhile can still tell which reply is which. Events pushed to
subscribers start with `event` and never collide with a reply.
"""

from dataclasses import dataclass, field

from .config import ConfigError, parse_line

MAX_COMMAND_BYTES = 1024

KEYWORDS = ("ping", "status", "laser", "subscribe", "unsubscribe", "calibrate", "forget",
            "snapshot", "camera", "sim")

_ON = ("on", "1", "true", "yes")
_OFF = ("off", "0", "false", "no")


class ProtocolError(ValueError):
    """Invalid command; `keyword` is echoed in the `err <keyword> ...` reply."""

    def __init__(self, keyword, message):
        super().__init__(message)
        self.keyword = keyword


@dataclass
class Command:
    keyword: str
    value: object = None
    params: dict = field(default_factory=dict)


def _printable(word, limit=32):
    text = "".join(ch if 33 <= ord(ch) < 127 else "?" for ch in word[:limit])
    return text or "?"


def _split(keyword, words):
    args, params = [], {}
    for word in words:
        if "=" in word:
            key, _, value = word.partition("=")
            key = key.lower()
            if not key or not value:
                raise ProtocolError(keyword, "bad parameter %r: expected key=value" % _printable(word))
            if key in params:
                raise ProtocolError(keyword, "parameter %s given twice" % key)
            params[key] = value
        else:
            args.append(word)
    return args, params


def _no_arguments(keyword, args, params):
    if args or params:
        raise ProtocolError(keyword, "takes no argument")
    return Command(keyword)


def _check_params(keyword, params, allowed):
    for key in params:
        if key not in allowed:
            raise ProtocolError(keyword, "unknown parameter %s (parameters: %s)"
                                % (_printable(key), ", ".join(allowed) or "none"))


def _number(keyword, key, text, lo, hi, kind=float):
    try:
        value = kind(text)
    except ValueError:
        raise ProtocolError(keyword, "%s=%s: expected a%s number"
                            % (key, _printable(text), "n integer" if kind is int else "")) from None
    if not lo <= value <= hi:  # also rejects nan
        raise ProtocolError(keyword, "%s=%s: expected %s..%s" % (key, _printable(text), lo, hi))
    return value


def _laser(keyword, args, params):
    _check_params(keyword, params, ())
    if len(args) != 1:
        raise ProtocolError(keyword, "usage: laser on|off")
    word = args[0].lower()
    if word in _ON:
        return Command(keyword, True)
    if word in _OFF:
        return Command(keyword, False)
    raise ProtocolError(keyword, "expected on or off, got %s" % _printable(args[0]))


def _calibrate(keyword, args, params):
    if args:
        raise ProtocolError(keyword, "usage: calibrate [line=x0,y0,x1,y1]")
    _check_params(keyword, params, ("line",))
    line = None
    if "line" in params:
        try:
            line = parse_line(params["line"])
        except ConfigError as exc:
            raise ProtocolError(keyword, "line=%s: %s" % (_printable(params["line"], 64), exc)) from None
    return Command(keyword, line)


def _camera(keyword, args, params):
    if args:
        raise ProtocolError(keyword, "usage: camera [exposure=<lines>] [gain=<n>]")
    _check_params(keyword, params, ("exposure", "gain"))
    values = {}
    if "exposure" in params:
        values["exposure"] = _number(keyword, "exposure", params["exposure"], 1, 1_000_000, int)
    if "gain" in params:
        values["gain"] = _number(keyword, "gain", params["gain"], 1, 100_000, int)
    return Command(keyword, None, values)


def _sim(keyword, args, params):
    if len(args) != 1 or args[0].lower() not in ("foot", "clear"):
        raise ProtocolError(keyword, "usage: sim foot pos=<0-1> [width=<0-1>] [duration=<s>] [lift=<px>] | sim clear")
    action = args[0].lower()
    if action == "clear":
        _check_params(keyword, params, ())
        return Command(keyword, "clear")
    _check_params(keyword, params, ("pos", "width", "duration", "lift"))
    if "pos" not in params:
        raise ProtocolError(keyword, "sim foot needs pos=<0-1>")
    values = {
        "pos": _number(keyword, "pos", params["pos"], 0.0, 1.0),
        "width": _number(keyword, "width", params.get("width", "0.1"), 0.001, 1.0),
        "duration": _number(keyword, "duration", params.get("duration", "0.5"), 0.0, 3600.0),
        "lift": _number(keyword, "lift", params.get("lift", "8"), 0.0, 200.0),
    }
    return Command(keyword, "foot", values)


_PARSERS = {
    "ping": _no_arguments,
    "status": _no_arguments,
    "subscribe": _no_arguments,
    "unsubscribe": _no_arguments,
    "forget": _no_arguments,
    "snapshot": _no_arguments,
    "laser": _laser,
    "calibrate": _calibrate,
    "camera": _camera,
    "sim": _sim,
}


def parse(data):
    """Datagram (bytes) -> Command. Raises ProtocolError."""
    if len(data) > MAX_COMMAND_BYTES:
        raise ProtocolError("?", "command too long (%d bytes, max %d)" % (len(data), MAX_COMMAND_BYTES))
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ProtocolError("?", "commands are ASCII text") from None
    words = text.split()
    if not words:
        raise ProtocolError("?", "empty command")
    keyword = words[0].lower()
    if keyword not in _PARSERS:
        raise ProtocolError(_printable(keyword), "unknown command (commands: %s)" % ", ".join(KEYWORDS))
    args, params = _split(keyword, words[1:])
    return _PARSERS[keyword](keyword, args, params)


def format_fields(pairs):
    """[(key, value), ...] -> 'key=value key=value' (values must not contain spaces)."""
    return " ".join("%s=%s" % (key, value) for key, value in pairs)
