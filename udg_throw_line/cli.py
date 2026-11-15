"""udg-throw-line: send one command to udg-throw-line-service and print the reply.

Exit status 0 when the reply is `ok ...` or `pong ...`, 1 otherwise (error
reply, no reply). `watch` subscribes and prints the events until Ctrl-C.
Used by hand on the kiosk (bring-up, calibration) and from scripts.
"""

import argparse
import socket
import sys
import time

from . import config

EPILOG = """examples:
  udg-throw-line ping
  udg-throw-line laser on
  udg-throw-line calibrate
  udg-throw-line snapshot          # then: scp root@<kiosk>:/run/udg-throw-line/snapshot.bmp .
  udg-throw-line camera exposure=300 gain=16
  udg-throw-line watch             # print events until Ctrl-C
  udg-throw-line sim foot pos=0.4  # --simulate only
"""

# Replies that take longer than a plain command: calibration switches the
# laser and collects two series of frames; a snapshot waits for the next frame.
SLOW = {"calibrate": 15.0, "snapshot": 5.0}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="udg-throw-line", description="Send a command to udg-throw-line-service.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="service address (default: [network] address of the config)")
    parser.add_argument("--port", type=int, help="service port (default: [network] port of the config)")
    parser.add_argument("--timeout", type=float, default=1.0, help="seconds to wait for the reply (default: 1)")
    parser.add_argument("--config", default=config.DEFAULT_ETC_PATH, help="configuration file to read the "
                        "address and port from (default: %(default)s)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command and its arguments")
    return parser


def _open(timeout):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    return sock


def request(payload, host, port, timeout, keyword):
    """Send one command, return its reply (events are skipped) or None."""
    with _open(timeout) as sock:
        sock.sendto(payload, (host, port))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(65536)
            except (socket.timeout, ConnectionResetError, ConnectionRefusedError):
                # Reset/Refused: the ICMP "port unreachable" of a service
                # that is not running (Windows, Linux).
                return None
            reply = data.decode("utf-8", "replace").strip()
            words = reply.split(" ", 2)
            if words[0] == "event":
                continue
            if words[0] == "pong" or (len(words) > 1 and words[1] == keyword) or words[0] == "err":
                return reply


def watch(host, port):
    """Subscribe and print every event; renew the subscription before it
    expires; unsubscribe on Ctrl-C."""
    ttl = 10.0
    with _open(1.0) as sock:
        def subscribe():
            sock.sendto(b"subscribe", (host, port))
        subscribe()
        renew_at = time.monotonic() + ttl / 3
        answered = False
        try:
            while True:
                now = time.monotonic()
                if now >= renew_at:
                    subscribe()
                    renew_at = now + ttl / 3
                sock.settimeout(max(0.05, renew_at - now))
                try:
                    data, _ = sock.recvfrom(65536)
                except socket.timeout:
                    if not answered:
                        print("udg-throw-line: no reply from %s:%d (is udg-throw-line-service running?)"
                              % (host, port), file=sys.stderr)
                        answered = True    # say it once, keep trying
                    continue
                except (ConnectionResetError, ConnectionRefusedError):
                    continue
                text = data.decode("utf-8", "replace").strip()
                if text.startswith("ok subscribe"):
                    answered = True
                    for word in text.split():
                        if word.startswith("ttl="):
                            try:
                                ttl = float(word[4:])
                            except ValueError:
                                pass
                    continue
                print("%s %s" % (time.strftime("%H:%M:%S"), text), flush=True)
        except KeyboardInterrupt:
            try:
                sock.sendto(b"unsubscribe", (host, port))
            except OSError:
                pass
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage(sys.stderr)
        return 1
    host, port = args.host, args.port
    if host is None or port is None:
        cfg, _ = config.load(args.config)
        host = host or cfg.address
        port = port or cfg.port
    keyword = args.command[0].lower()
    if keyword == "watch":
        return watch(host, port)
    payload = " ".join(args.command).encode("utf-8")
    timeout = max(args.timeout, SLOW.get(keyword, 0.0))
    try:
        reply = request(payload, host, port, timeout, keyword)
    except OSError as exc:
        print("udg-throw-line: cannot send to %s:%d: %s" % (host, port, exc), file=sys.stderr)
        return 1
    if reply is None:
        print("udg-throw-line: no reply from %s:%d within %gs (is udg-throw-line-service running?)"
              % (host, port, timeout), file=sys.stderr)
        return 1
    print(reply)
    return 0 if reply.split(" ", 1)[0] in ("ok", "pong") else 1


if __name__ == "__main__":
    sys.exit(main())
