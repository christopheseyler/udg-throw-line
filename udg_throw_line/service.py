"""udg-throw-line-service: one process, one selectors loop.

The loop waits on the UDP control socket, the camera's file descriptor (a
V4L2 node is readable when a frame is ready) and a few timers (camera retry,
frame timeout, subscription expiry, calibration and snapshot deadlines, the
simulated camera's frame clock). No threads: every frame is handled as soon
as it is dequeued, in well under a millisecond, and a single loop keeps the
ordering of laser switching, frames and events easy to reason about.
"""

import argparse
import os
import selectors
import signal
import socket
import sys
import time

from . import API_VERSION, VERSION, config, log, protocol
from .calibration import Calibration, CalibrationError, compute
from .detection import Detector, GeometryError, LineGeometry, LineTracker
from .frame import CYAN, GREEN, MAGENTA, YELLOW, bmp_bytes, draw_points, render_rgb, write_atomic
from .laser import NullLaser, SimLaser, make_laser
from .protocol import format_fields
from .simulator import SimCamera
from .v4l2 import CameraError, V4L2Camera

RECV_SIZE = 4096
# Datagrams handled per loop iteration before frames get a chance.
DRAIN_LIMIT = 64
SNAPSHOT_TIMEOUT = 2.0
CALIBRATION_TIMEOUT = 10.0
# Longest sleep of the loop, so housekeeping never waits for a timer that
# was computed before a state change.
MAX_SLEEP = 1.0


class CalibrationJob:
    """Frame collection of one `calibrate` request: laser off (settle, then
    `frames` frames), laser on (settle, then `frames` frames)."""

    def __init__(self, peer, line, cfg, now, with_off_phase):
        self.peer = peer
        self.line = line
        self.count = cfg.cal_frames
        self.settle = cfg.cal_settle_frames
        self.phase = "off" if with_off_phase else "on"
        self.skip = self.settle
        self.off = []
        self.on = []
        self.deadline = now + CALIBRATION_TIMEOUT

    def feed(self, frame):
        """Returns "switch-on" when the laser must be lit, "done" when enough
        frames were collected, None otherwise."""
        if self.skip > 0:
            # Frames exposed before (or while) the laser switched, still in
            # the driver's queue: skip them.
            self.skip -= 1
            return None
        target = self.off if self.phase == "off" else self.on
        target.append(frame.copy_bytes())
        if len(target) < self.count:
            return None
        if self.phase == "off":
            self.phase = "on"
            self.skip = self.settle
            return "switch-on"
        return "done"


class Service:
    """Everything except the socket and the loop: testable without the network."""

    def __init__(self, cfg, camera, laser, send=None, simulate=False):
        self.cfg = cfg
        self.camera = camera
        self.laser = laser
        self.send = send or (lambda text, peer: None)
        self.simulate = simulate
        self.subscribers = {}           # (host, port) -> expiry time
        self.tracker = LineTracker(cfg.confirm_frames, cfg.release_frames, cfg.reverse_position)
        self.calibration = None         # loaded or computed, may not match the camera
        self.geometry = None
        self.detector = None
        self.job = None
        self.snapshot_peers = []
        self.snapshot_deadline = 0.0
        self.laser_wanted = cfg.laser_startup
        self.laser_retry_at = float("inf")
        self.camera_retry_at = 0.0
        self.settle_left = 0
        self.frames = 0
        self.last_eval = None
        self._watching = False
        self._last_frame = None
        self._frame_dt = 0.0
        self._camera_once = log.Once()
        self._calibration_once = log.Once()
        self._service_key = None

    # --- lifecycle -----------------------------------------------------------

    def start(self, now):
        self._load_calibration()
        self._apply_laser(now)
        self._open_camera(now)
        self._update_watch(now)

    def shutdown(self, now):
        if self.job is not None:
            self._reply(self.job.peer, "err calibrate service stopping")
            self.job = None
        for peer in self.snapshot_peers:
            self._reply(peer, "err snapshot service stopping")
        self.snapshot_peers = []
        self.laser.set(False)
        self.laser.close()
        self.camera.close()

    # --- state ---------------------------------------------------------------

    def _reason(self):
        """Why the line is not watched, or None when it is."""
        if self.job is not None:
            return "calibrating"
        if self.camera.state != "streaming":
            return "camera"
        if self.detector is None:
            return "not-calibrated"
        if not self.laser.lit:
            return "laser-off"
        return None

    def _update_watch(self, now):
        reason = self._reason()
        watching = reason is None
        if watching and not self._watching:
            # The first frames after the laser switches on (or the stream
            # starts) were exposed before: do not judge them.
            self.settle_left = self.cfg.settle_frames
        if not watching:
            event = self.tracker.reset(now, reason)
            if event:
                self._broadcast_line(event)
        self._watching = watching
        self._broadcast_service_if_changed()

    def service_fields(self):
        return [
            ("laser", "on" if self.laser.lit else "off"),
            ("camera", self.camera.state),
            ("calibrated", "yes" if self.detector is not None else "no"),
            ("watching", "yes" if self._watching else "no"),
        ]

    def _broadcast_service_if_changed(self):
        fields = self.service_fields()
        key = tuple(fields)
        if key != self._service_key:
            self._service_key = key
            self._broadcast("event service " + format_fields(fields))

    # --- events and replies -----------------------------------------------------

    def _reply(self, peer, text):
        if peer is not None:
            self.send(text, peer)

    def _broadcast(self, text):
        for peer in list(self.subscribers):
            self.send(text, peer)

    def _broadcast_line(self, fields):
        text = "event line " + format_fields(fields)
        log.info(text[6:])
        self._broadcast(text)

    # --- laser ---------------------------------------------------------------

    def _apply_laser(self, now):
        if self.job is not None:
            return True             # calibration owns the laser until it ends
        ok = self.laser.set(self.laser_wanted)
        # A missing output (no device tree entry yet, GPIO busy) is retried.
        self.laser_retry_at = float("inf") if ok else now + self.cfg.camera_retry
        return ok

    # --- camera --------------------------------------------------------------

    def _open_camera(self, now):
        try:
            self.camera.open(now)
        except CameraError as exc:
            self._camera_once(log.warning if exc.state == "absent" else log.error, "camera: %s" % exc)
            self.camera_retry_at = now + self.cfg.camera_retry
            return False
        self._camera_once.reset()
        log.info("camera streaming: %s" % self.camera.describe())
        self._last_frame = now
        self._frame_dt = 0.0
        self._bind_calibration()
        self._update_watch(now)
        return True

    def _close_camera(self, now, message):
        log.error(message)
        if self.job is not None:
            self._fail_job(now, "camera lost during calibration")
        for peer in self.snapshot_peers:
            self._reply(peer, "err snapshot camera lost")
        self.snapshot_peers = []
        self.camera.close()
        self.camera.state = "error"
        self.camera.reason = message
        self.detector = None
        self.geometry = None
        self.camera_retry_at = now + min(1.0, self.cfg.camera_retry)
        self._update_watch(now)

    def on_camera_ready(self, now):
        """The camera fd is readable, or the simulated frame clock ticked."""
        if self.camera.state != "streaming":
            return
        try:
            frame = self.camera.read(now)
        except CameraError as exc:
            self._close_camera(now, "camera: %s" % exc)
            return
        if frame is None:
            return
        try:
            self._on_frame(frame, now)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not stop the service
            log.error("frame %d: %s: %s" % (frame.sequence, type(exc).__name__, exc))
        finally:
            try:
                self.camera.release(frame)
            except CameraError as exc:
                self._close_camera(now, "camera: %s" % exc)

    def _on_frame(self, frame, now):
        self.frames += 1
        if self._last_frame is not None:
            dt = now - self._last_frame
            if dt > 0:
                self._frame_dt = dt if not self._frame_dt else 0.9 * self._frame_dt + 0.1 * dt
        self._last_frame = now
        if self.snapshot_peers:
            self._write_snapshot(frame)
        if self.job is not None:
            self._feed_job(frame, now)
            return
        if not self._watching:
            return
        if self.settle_left > 0:
            self.settle_left -= 1
            return
        self.last_eval = self.detector.evaluate(frame.view)
        event = self.tracker.feed(self.last_eval, now)
        if event:
            self._broadcast_line(event)

    @property
    def fps(self):
        return 1.0 / self._frame_dt if self._frame_dt else 0.0

    # --- calibration ---------------------------------------------------------

    def _load_calibration(self):
        path = self.cfg.calibration_path
        if not os.path.exists(path):
            log.info("not calibrated yet (%s absent): send `calibrate`" % path)
            return
        try:
            self.calibration = Calibration.load(path)
        except CalibrationError as exc:
            log.error("calibration ignored: %s" % exc)
            return
        log.info("calibration loaded: line %s, %d/%d usable samples, made %s for %s"
                 % (self.calibration.line_text(), self.calibration.usable, self.calibration.samples,
                    self.calibration.created or "?", self.calibration.signature))

    def _bind_calibration(self):
        """Build the detector for the current camera format, if the stored
        calibration was made with the same one."""
        self.detector = None
        self.geometry = None
        cal = self.calibration
        if cal is None or self.camera.state != "streaming":
            return
        problem = None
        if cal.signature != self.camera.signature:
            problem = "made for '%s', camera is '%s'" % (cal.signature, self.camera.signature)
        elif cal.step != self.cfg.step:
            problem = "made with [detect] step = %d, now %d" % (cal.step, self.cfg.step)
        else:
            try:
                geometry = LineGeometry(self.camera.fmt, cal.line, cal.step, self.cfg.search)
                if geometry.n != cal.samples:
                    problem = "%d samples stored, the line now has %d" % (cal.samples, geometry.n)
                else:
                    self.detector = Detector(geometry, cal.offsets, cal.contrasts, self.cfg)
                    self.geometry = geometry
            except GeometryError as exc:
                problem = str(exc)
        if problem:
            self._calibration_once(log.warning, "calibration not used (%s): send `calibrate` again" % problem)
        else:
            self._calibration_once.reset()

    def _start_job(self, peer, line, now):
        with_off = False
        if not isinstance(self.laser, NullLaser):
            # Laser-off frames make the line search immune to red objects;
            # without a switchable laser, the search works on one image.
            with_off = self.laser.set(False)
        self.job = CalibrationJob(peer, line, self.cfg, now, with_off)
        if not with_off:
            self.laser.set(True)
        log.info("calibration started (%s)" % ("laser off then on" if with_off else "laser not switchable"))
        self._update_watch(now)

    def _feed_job(self, frame, now):
        action = self.job.feed(frame)
        if action == "switch-on":
            if not self.laser.set(True):
                self._fail_job(now, "laser output unavailable: %s" % self.laser.reason)
        elif action == "done":
            self._finish_job(now)

    def _fail_job(self, now, message):
        job, self.job = self.job, None
        log.warning("calibration failed: %s" % message)
        self._reply(job.peer, "err calibrate %s" % message)
        self._apply_laser(now)
        self._update_watch(now)

    def _finish_job(self, now):
        job, self.job = self.job, None
        reply = None
        try:
            cal = compute(self.camera.fmt, job.off, job.on, self.cfg, self.camera.signature, job.line,
                          created=time.strftime("%Y-%m-%d %H:%M:%S"))
        except CalibrationError as exc:
            log.warning("calibration failed: %s" % exc)
            reply = "err calibrate %s" % exc
        except Exception as exc:  # noqa: BLE001 - never kill the service
            log.error("calibration crashed: %s: %s" % (type(exc).__name__, exc))
            reply = "err calibrate internal error: %s" % type(exc).__name__
        else:
            self.calibration = cal
            warning = ""
            try:
                os.makedirs(self.cfg.data_dir, exist_ok=True)
                cal.save(self.cfg.calibration_path)
            except OSError as exc:
                log.error("calibration not saved to %s: %s" % (self.cfg.calibration_path, exc.strerror or exc))
                warning = " warning=not-saved"
            self._bind_calibration()
            fields = [("line", cal.line_text()), ("samples", cal.samples), ("usable", cal.usable)]
            fields += [(key, cal.stats[key]) for key in ("contrast", "orientation", "mode") if key in cal.stats]
            reply = "ok calibrate " + format_fields(fields) + warning
            log.info("calibrated: " + format_fields(fields))
        self._reply(job.peer, reply)
        self._apply_laser(now)
        self._update_watch(now)

    # --- snapshot ------------------------------------------------------------

    def _write_snapshot(self, frame):
        peers, self.snapshot_peers = self.snapshot_peers, []
        try:
            rows = render_rgb(frame.fmt, frame.view)
            if self.geometry is not None:
                draw_points(rows, self._overlay())
            data = bmp_bytes(rows)
            os.makedirs(self.cfg.run_dir, exist_ok=True)
            write_atomic(self.cfg.snapshot_path, data)
        except OSError as exc:
            reply = "err snapshot cannot write %s: %s" % (self.cfg.snapshot_path, exc.strerror or exc)
        else:
            reply = "ok snapshot path=%s size=%dx%d" % (self.cfg.snapshot_path, len(rows[0]) // 3, len(rows))
        for peer in peers:
            self._reply(peer, reply)

    def _overlay(self):
        """Dots on both sides of the watched line, `search` + 1 pixels away
        (the laser itself stays visible): green = intact, yellow = broken in
        the last checked frame, magenta = dead sample (not watched); cyan
        while the line is not watched."""
        geometry = self.geometry
        state = {}
        if self.detector is not None:
            broken = self.last_eval.broken if self.last_eval is not None and self._watching else None
            for j, i in enumerate(self.detector.ids):
                if not self._watching:
                    state[i] = CYAN
                else:
                    state[i] = YELLOW if broken is not None and broken[j] else GREEN
        points = []
        side = geometry.search + 1
        for i in range(geometry.n):
            color = state.get(i, MAGENTA)
            for offset in (-side, side):
                u, v = geometry.point(i, offset)
                points.append((u, v, color))
        return points

    # --- housekeeping ----------------------------------------------------------

    def next_deadline(self, now):
        deadlines = [now + MAX_SLEEP, self.camera.next_deadline(now)]
        if self.camera.state != "streaming":
            deadlines.append(self.camera_retry_at)
        elif self._last_frame is not None:
            deadlines.append(self._last_frame + self.cfg.frame_timeout)
        if self.subscribers:
            deadlines.append(min(self.subscribers.values()))
        if self.job is not None:
            deadlines.append(self.job.deadline)
        if self.snapshot_peers:
            deadlines.append(self.snapshot_deadline)
        deadlines.append(self.laser_retry_at)
        return min(deadlines)

    def tick(self, now):
        for peer in [p for p, expiry in self.subscribers.items() if expiry <= now]:
            del self.subscribers[peer]
            log.debug("subscription of %s:%d expired" % peer)
        if self.camera.state != "streaming":
            if now >= self.camera_retry_at:
                self._open_camera(now)
        # ">=" everywhere: next_deadline() wakes the loop exactly at these
        # times, and a strict comparison would leave it spinning until the
        # clock moves on.
        elif self._last_frame is not None and now - self._last_frame >= self.cfg.frame_timeout:
            self._close_camera(now, "camera: no frame for %.1f s, restarting the stream" % (now - self._last_frame))
        if self.job is not None and now >= self.job.deadline:
            self._fail_job(now, "timeout: not enough frames from the camera")
        if self.snapshot_peers and now >= self.snapshot_deadline:
            for peer in self.snapshot_peers:
                self._reply(peer, "err snapshot no frame from the camera")
            self.snapshot_peers = []
        if now >= self.laser_retry_at:
            was = self.laser.lit
            self._apply_laser(now)
            if self.laser.lit != was:
                self._update_watch(now)

    # --- commands -----------------------------------------------------------

    def handle(self, data, peer, now):
        """One datagram -> one reply line, or None when the reply comes later
        (calibrate, snapshot) or was already sent (subscribe)."""
        try:
            command = protocol.parse(data)
        except protocol.ProtocolError as exc:
            return "err %s %s" % (exc.keyword, exc)
        handler = getattr(self, "_cmd_" + command.keyword)
        try:
            return handler(command, peer, now)
        except Exception as exc:  # noqa: BLE001 - a command must never kill the service
            log.error("internal error on %r: %s: %s" % (command, type(exc).__name__, exc))
            return "err %s internal error: %s" % (command.keyword, type(exc).__name__)

    def _cmd_ping(self, command, peer, now):
        return "pong %s api=%d %s" % (VERSION, API_VERSION, format_fields(self.service_fields()))

    def status_fields(self):
        cal = self.calibration if self.detector is not None else None
        evaluation = self.last_eval if self._watching else None
        fields = [
            ("laser", "on" if self.laser.lit else "off"),
            ("laser_wanted", "on" if self.laser_wanted else "off"),
            ("output", self.laser.describe()),
            ("camera", self.camera.state),
            ("source", self.camera.describe()),
            ("fps", "%.1f" % self.fps),
            ("frames", self.frames),
        ]
        for key in ("exposure", "gain"):
            if key in self.camera.controls:
                fields.append((key, self.camera.controls[key]))
        fields += [
            ("calibrated", "yes" if cal else "no"),
            ("line", cal.line_text() if cal else "-"),
            ("samples", cal.samples if cal else 0),
            ("usable", cal.usable if cal else 0),
            ("watching", "yes" if self._watching else "no"),
            ("state", self.tracker.state),
            ("coverage", "%.2f" % evaluation.coverage if evaluation else "-"),
            ("crossings", self.tracker.crossings),
            ("subscribers", len(self.subscribers)),
        ]
        return fields

    def _cmd_status(self, command, peer, now):
        return "ok status " + format_fields(self.status_fields())

    def _cmd_laser(self, command, peer, now):
        word = "on" if command.value else "off"
        self.laser_wanted = command.value
        if self.job is not None:
            return "ok laser %s deferred=calibrating" % word
        ok = self._apply_laser(now)
        self._update_watch(now)
        if not ok:
            return "err laser output unavailable: %s" % self.laser.reason
        return "ok laser " + word

    def _cmd_subscribe(self, command, peer, now):
        if peer not in self.subscribers and len(self.subscribers) >= self.cfg.max_subscribers:
            oldest = min(self.subscribers, key=self.subscribers.get)
            del self.subscribers[oldest]
            log.warning("too many subscribers: dropped %s:%d" % oldest)
        new = peer not in self.subscribers
        self.subscribers[peer] = now + self.cfg.subscription_ttl
        self._reply(peer, "ok subscribe ttl=%g" % self.cfg.subscription_ttl)
        if new:
            # A new subscriber learns the current state at once (someone may
            # already be standing on the line).
            log.info("subscriber %s:%d" % peer)
            self.send("event service " + format_fields(self.service_fields()), peer)
            self.send("event line " + format_fields(self.tracker.current(self._reason())), peer)
        return None

    def _cmd_unsubscribe(self, command, peer, now):
        self.subscribers.pop(peer, None)
        return "ok unsubscribe"

    def _cmd_calibrate(self, command, peer, now):
        if self.job is not None:
            return "err calibrate already running"
        if self.camera.state != "streaming":
            return "err calibrate camera not available (%s)" % (self.camera.reason or self.camera.state)
        self._start_job(peer, command.value, now)
        return None

    def _cmd_forget(self, command, peer, now):
        try:
            os.remove(self.cfg.calibration_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return "err forget cannot remove %s: %s" % (self.cfg.calibration_path, exc.strerror or exc)
        self.calibration = None
        self.detector = None
        self.geometry = None
        self._update_watch(now)
        log.info("calibration forgotten")
        return "ok forget"

    def _cmd_snapshot(self, command, peer, now):
        if self.camera.state != "streaming":
            return "err snapshot camera not available (%s)" % (self.camera.reason or self.camera.state)
        if not self.snapshot_peers:
            self.snapshot_deadline = now + SNAPSHOT_TIMEOUT
        if peer not in self.snapshot_peers:
            self.snapshot_peers.append(peer)
        return None

    def _cmd_camera(self, command, peer, now):
        if self.camera.state != "streaming":
            return "err camera not available (%s)" % (self.camera.reason or self.camera.state)
        try:
            values = self.camera.set_controls(**command.params) if command.params else dict(self.camera.controls)
        except CameraError as exc:
            return "err camera %s" % exc
        if not values:
            return "err camera exposure and gain cannot be read"
        return "ok camera " + format_fields(sorted(values.items()))

    def _cmd_sim(self, command, peer, now):
        if not self.simulate:
            return "err sim only available with --simulate"
        if command.value == "clear":
            self.camera.clear_feet()
            return "ok sim clear"
        p = command.params
        lo, hi = self.camera.add_foot(p["pos"], p["width"], p["duration"], p["lift"], now)
        return "ok sim foot from=%.3f to=%.3f duration=%g" % (lo, hi, p["duration"])


# --- network loop ------------------------------------------------------------

def open_socket(address, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        # Windows reports an ICMP "port unreachable" (a client that left
        # before reading our reply) as an error on the *next* recvfrom.
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except (OSError, ValueError):
            pass
    sock.bind((address, port))
    sock.setblocking(False)
    return sock


def make_sender(sock):
    def send(text, peer):
        try:
            sock.sendto(text.encode("ascii", "backslashreplace") + b"\n", peer)
        except OSError:
            pass  # the client may be gone; replies and events are best effort
    return send


def drain(sock, service, clock=time.monotonic):
    for _ in range(DRAIN_LIMIT):
        try:
            data, peer = sock.recvfrom(RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except ConnectionResetError:
            continue
        except OSError as exc:
            log.warning("recvfrom failed: %s" % exc)
            return
        reply = service.handle(data, peer, clock())
        if reply is not None:
            log.debug("%s:%d %r -> %s" % (peer[0], peer[1], data[:60], reply))
            service.send(reply, peer)


class _Stop:
    requested = False


def serve(service, sock, clock=time.monotonic):
    stop = _Stop()
    selector = selectors.DefaultSelector()
    selector.register(sock, selectors.EVENT_READ, "udp")

    def on_signal(signum, frame):
        stop.requested = True

    previous = {}
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
    wake_r = wake_w = None
    if os.name == "posix":
        # PEP 475 retries select() after a signal handler that does not raise;
        # the wakeup pipe (a pipe, not a socketpair: the unit only allows
        # AF_INET) makes SIGTERM end the wait immediately.
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        selector.register(wake_r, selectors.EVENT_READ, "wake")
    registered = None        # (fd, generation) of the camera registration
    try:
        while not stop.requested:
            camera = service.camera
            fd = camera.fileno()
            key = (fd, getattr(camera, "generation", 0)) if fd is not None else None
            if key != registered:
                if registered is not None:
                    try:
                        selector.unregister(registered[0])
                    except (KeyError, ValueError, OSError):
                        pass
                if key is not None:
                    selector.register(fd, selectors.EVENT_READ, "camera")
                registered = key
            now = clock()
            timeout = max(0.0, service.next_deadline(now) - now)
            for sel_key, _ in selector.select(timeout):
                if sel_key.data == "udp":
                    drain(sock, service, clock)
                elif sel_key.data == "camera":
                    service.on_camera_ready(clock())
                else:
                    try:
                        os.read(wake_r, 512)
                    except OSError:
                        pass
            if stop.requested:
                break
            now = clock()
            if camera.fileno() is None and now >= camera.next_deadline(now):
                service.on_camera_ready(now)       # simulated camera
            service.tick(clock())
    finally:
        selector.close()
        if wake_w is not None:
            signal.set_wakeup_fd(-1)
            os.close(wake_r)
            os.close(wake_w)
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass


# --- command line ------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="udg-throw-line-service",
        description="Watch the throw line (CSI camera + laser line) and notify the game over local UDP.",
    )
    parser.add_argument("--config", default=config.DEFAULT_ETC_PATH,
                        help="main configuration file (default: %(default)s)")
    parser.add_argument("--data-dir", help="persistent directory: udg-throw-line.conf override and "
                        "calibration (default: [service] data_dir, /data/udg-throw-line)")
    parser.add_argument("--run-dir", help="volatile directory for snapshots (default: [service] run_dir)")
    parser.add_argument("--simulate", action="store_true",
                        help="no hardware: synthetic camera with a laser line, `sim foot` command")
    parser.add_argument("--laser-off", action="store_true",
                        help="switch the laser output off and exit (for ExecStopPost=)")
    parser.add_argument("--address", help="UDP address to bind")
    parser.add_argument("--port", help="UDP port")
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                        help="override any configuration value (repeatable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return parser


SHORTCUTS = {
    "address": ("network", "address"),
    "port": ("network", "port"),
    "run_dir": ("service", "run_dir"),
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = {}
    for text in args.set:
        try:
            key, value = config.parse_override(text)
        except config.ConfigError as exc:
            parser.error(str(exc))
        overrides[key] = value
    for option, key in SHORTCUTS.items():
        value = getattr(args, option)
        if value is not None:
            overrides[key] = value

    log.configure(verbose=args.verbose)
    cfg, messages = config.load(args.config, args.data_dir, overrides)
    for level, text in messages:
        (log.error if level == "error" else log.warning)(text)

    if args.laser_off:
        laser = make_laser(cfg)
        ok = laser.set(False)
        laser.close()
        return 0 if ok else 1

    log.info("udg-throw-line-service %s (protocol %d), configuration: %s"
             % (VERSION, API_VERSION, ", ".join(cfg.sources) or "built-in defaults"))
    log.debug("configuration: " + config.describe(cfg))
    if args.simulate:
        laser = SimLaser()
        camera = SimCamera(cfg, laser)
    else:
        laser = make_laser(cfg)
        camera = V4L2Camera(cfg)
    log.info("laser output: %s, startup %s" % (laser.describe(), "on" if cfg.laser_startup else "off"))

    try:
        sock = open_socket(cfg.address, cfg.port)
    except OSError as exc:
        log.error("cannot listen on UDP %s:%d: %s" % (cfg.address, cfg.port, exc.strerror or exc))
        return 1
    service = Service(cfg, camera, laser, make_sender(sock), simulate=args.simulate)
    log.info("listening on udp://%s:%d" % (cfg.address, cfg.port))
    try:
        service.start(time.monotonic())
        serve(service, sock)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stopping: laser off")
        service.shutdown(time.monotonic())
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
