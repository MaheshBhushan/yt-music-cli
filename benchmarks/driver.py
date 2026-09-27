"""Real-PTY driver, trace tailer, and mpv JSON IPC observer.

Drives the actual console entry point in a PTY with fixed dimensions. The
application is not modified: boundaries come from the app's own TUI trace
(app-measured request durations, receipt times for the rest) and from a
read-only second mpv IPC connection. See the report's method notes for the
consequences of that choice.
"""

from __future__ import annotations

import errno
import json
import os
import re
import select
import signal
import socket
import struct
import time

from time import monotonic_ns as now_ns


def read_text(path):
    # The collector itself is Linux-specific; trace parsing is portable.
    from collector import read_text as read
    return read(path)

STARTUP_TIMEOUT = 60.0
SEARCH_TIMEOUT = 60.0
PLAYBACK_TIMEOUT = 90.0

_REQUEST_DONE = re.compile(r"^request (\w+) done in ([0-9.]+)s$")
_REQUEST_FAIL = re.compile(r"^request (\w+) failed after ([0-9.]+)s: ?(.*)$")
_REQUEST_START = re.compile(r"^request (\w+) (.*)$")
_KEY = re.compile(r"^key '([^']*)' focus=(\S+)$")
_FOCUS = re.compile(r"^focus -> (\S+)$")
_STARTED = re.compile(r"^ytm (\S+) started, size (\d+)x(\d+)$")
_SELECTED = re.compile(r"^selected (\S+) row=(-?\d+)")


def parse_trace_line(text: str) -> dict:
    """Turn one tui.log line (after the HH:MM:SS prefix) into a record."""
    match = _REQUEST_DONE.match(text)
    if match:
        return {"kind": "request_done", "cmd": match.group(1),
                "duration_s": float(match.group(2))}
    match = _REQUEST_FAIL.match(text)
    if match:
        return {"kind": "request_failed", "cmd": match.group(1),
                "duration_s": float(match.group(2)), "error": match.group(3)}
    match = _KEY.match(text)
    if match:
        return {"kind": "key", "key": match.group(1), "focus": match.group(2)}
    match = _FOCUS.match(text)
    if match:
        return {"kind": "focus", "widget": match.group(1)}
    match = _STARTED.match(text)
    if match:
        return {"kind": "started", "version": match.group(1),
                "size": (int(match.group(2)), int(match.group(3)))}
    match = _SELECTED.match(text)
    if match:
        return {"kind": "selected", "table": match.group(1),
                "row": int(match.group(2))}
    match = _REQUEST_START.match(text)
    if match:
        return {"kind": "request_start", "cmd": match.group(1),
                "args": match.group(2)}
    return {"kind": "other", "text": text}


class TraceTail:
    """Tail the app's TUI trace file, surviving truncation at start-up."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.inode = None
        self.rotations = 0
        self.records: list[dict] = []

    def poll(self) -> list[dict]:
        try:
            stat = os.stat(self.path)
        except OSError:
            return []
        if self.inode is None:
            self.inode = stat.st_ino
            self.offset = 0
        elif stat.st_ino != self.inode:
            self.inode, self.offset, self.rotations = stat.st_ino, 0, self.rotations + 1
        elif stat.st_size < self.offset:
            self.offset = 0
            self.rotations += 1
        if stat.st_size == self.offset:
            return []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []
        out = []
        for raw in data.decode("utf-8", "replace").splitlines():
            receipt = now_ns()
            _, _, rest = raw.partition(" ")
            record = parse_trace_line(rest)
            record["receipt_ns"] = receipt
            record["raw"] = raw
            out.append(record)
        self.records.extend(out)
        if len(self.records) > 20000:
            del self.records[:10000]
        return out


class MpvObserver:
    """Second, read-only mpv IPC client: observed properties and core events."""

    PROPERTIES = ("playlist-pos", "playlist-count", "pause", "path", "time-pos",
                  "duration", "idle-active", "eof-reached")

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect(socket_path)
        self.sock.setblocking(False)
        self.buffer = b""
        self.values: dict = {}
        self.events: list[dict] = []
        self.replies: dict = {}
        self._next_id = 0
        for name in self.PROPERTIES:
            self._next_id += 1
            self.send(["observe_property", self._next_id, name])
        self.connected_ns = now_ns()

    def send(self, command) -> int:
        self._next_id += 1
        payload = {"command": command, "request_id": self._next_id}
        try:
            self.sock.sendall((json.dumps(payload) + "\n").encode())
        except OSError:
            pass
        return self._next_id

    def poll(self) -> None:
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("mpv IPC closed")
                self.buffer += chunk
        except BlockingIOError:
            pass
        except OSError:
            raise
        while b"\n" in self.buffer:
            line, _, self.buffer = self.buffer.partition(b"\n")
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "request_id" in message and message.get("error") == "success":
                if message.get("data") != "property" and "data" in message:
                    # command reply: keep data for get_property callers
                    pass
            if message.get("event") == "property-change":
                name = message.get("name")
                if name:
                    self.values[name] = message.get("data")
                continue
            if "event" in message:
                message["_receipt_ns"] = now_ns()
                self.events.append(message)
                if len(self.events) > 5000:
                    del self.events[:2500]
                continue
            if "request_id" in message:
                self.replies[message["request_id"]] = message

    def get(self, name, default=None):
        return self.values.get(name, default)

    def events_since(self, ns: int) -> list[dict]:
        return [e for e in self.events if e.get("_receipt_ns", 0) >= ns]


class Session:
    """One launch of the real `ytm` console app inside its own scope + PTY."""

    def __init__(self, bench, name: str, scope, executable: str, env: dict,
                 workdir: str, terminal_size=(40, 120)):
        self.bench = bench
        self.name = name
        self.scope = scope
        self.executable = executable
        self.env = env
        self.workdir = workdir
        self.rows, self.cols = terminal_size
        self.pid: int | None = None
        self.master: int | None = None
        self.launch_ns: int | None = None
        self.exit_ns: int | None = None
        self.exit_status = None
        self.mpv: MpvObserver | None = None
        self.mpv_socket: str | None = None
        self.focused = None
        self.query = ""
        self.last_write_ns = None
        self.tp_series: list[tuple[int, float]] = []
        self.pty_written = 0
        self.pty_limit = 64 * 1024 * 1024
        session_dir = self.bench.session_dir(name)
        session_dir.mkdir(parents=True, exist_ok=True)
        self.trace = TraceTail(session_dir / "tui.log")
        self.pty_log = open(session_dir / "pty-output.bin", "wb")
        self.log_events: list[dict] = []

    # -- launch ----------------------------------------------------------

    def launch(self) -> int:
        if os.name == "nt":
            raise RuntimeError("The PTY benchmark driver requires POSIX; protocol parsing is portable.")
        import fcntl
        import termios

        self.launch_ns = now_ns()
        self.bench.event("launch.requested", session=self.name,
                         monotonic_ns=self.launch_ns)
        pid, master = os.forkpty()
        if pid == 0:  # child
            try:
                fcntl.ioctl(1, termios.TIOCSWINSZ,
                            struct.pack("HHHH", self.rows, self.cols, 0, 0))
                self.scope.move_self_into()
                os.chdir(self.workdir)
                os.execvpe(self.executable, [self.executable], self.env)
            except BaseException:
                os._exit(127)
        self.pid = pid
        self.master = master
        fcntl.ioctl(master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", self.rows, self.cols, 0, 0))
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return self.launch_ns

    # -- pumping ---------------------------------------------------------

    def drain(self) -> None:
        if self.master is None:
            return
        chunks = []
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EIO:
                    self._mark_exited()
                break
            if not chunk:
                self._mark_exited()
                break
            chunks.append(chunk)
        if not chunks:
            return
        data = b"".join(chunks)
        if self.pty_written < self.pty_limit:
            room = self.pty_limit - self.pty_written
            self.pty_log.write(data[:room])
            self.pty_written += min(room, len(data))
        self.bench.record_pty(self.name, data)

    def _mark_exited(self) -> None:
        if self.exit_ns is None:
            self.exit_ns = now_ns()
            self.bench.event("session.pty_closed", session=self.name,
                             monotonic_ns=self.exit_ns)
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None

    def poll(self) -> None:
        self.drain()
        for record in self.trace.poll():
            self.log_events.append(record)
            if record["kind"] == "focus":
                self.focused = record["widget"]
            self.bench.event("trace." + record["kind"], session=self.name,
                             monotonic_ns=record["receipt_ns"],
                             fields=self._trace_fields(record))
        self.ensure_mpv()
        if self.mpv is not None:
            try:
                self.mpv.poll()
            except (OSError, ConnectionError):
                self.bench.event("mpv.observer_lost", session=self.name, monotonic_ns=now_ns())
                self.mpv = None
                self.mpv_socket = None
            else:
                self._sample_time_pos()
        if self.pid is not None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pid, status = self.pid, None
            if pid:
                self.exit_status = status
                if self.exit_ns is None:
                    self.exit_ns = now_ns()
                self.pid = None

    def _trace_fields(self, record: dict) -> dict:
        return {k: v for k, v in record.items()
                if k not in ("raw", "receipt_ns", "kind")}

    def _sample_time_pos(self) -> None:
        value = self.mpv.get("time-pos")
        if isinstance(value, (int, float)):
            if not self.tp_series or value != self.tp_series[-1][1]:
                self.tp_series.append((now_ns(), float(value)))
                if len(self.tp_series) > 64:
                    del self.tp_series[:-32]

    def ensure_mpv(self) -> None:
        if self.mpv is not None or self.mpv_socket is not None:
            return
        for pid in self.scope.procs():
            cmdline = read_text(f"/proc/{pid}/cmdline") or ""
            for arg in cmdline.split("\0"):
                if arg.startswith("--input-ipc-server="):
                    socket_path = arg.split("=", 1)[1]
                    try:
                        self.mpv = MpvObserver(socket_path)
                    except OSError:
                        return
                    self.mpv_socket = socket_path
                    self.bench.event("mpv.observer_connected", session=self.name,
                                     monotonic_ns=self.mpv.connected_ns,
                                     fields={"socket": socket_path})
                    return

    @property
    def alive(self) -> bool:
        return self.pid is not None

    # -- sending and waiting ---------------------------------------------

    def send(self, data: bytes) -> int:
        if self.master is None:
            raise RuntimeError(f"{self.name}: PTY closed")
        os.write(self.master, data)
        self.last_write_ns = now_ns()
        return self.last_write_ns

    def send_special(self, data: bytes) -> int:
        stamp = self.send(data)
        self.bench.event("key.sent", session=self.name, monotonic_ns=stamp,
                         fields={"keys": repr(data.decode("latin-1"))})
        return stamp

    def pump_until(self, predicate, timeout: float, what: str, poll=0.01):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            self.bench.pump()
            try:
                value = predicate()
            except Exception as exc:  # pragma: no cover - defensive
                last_error = exc
            else:
                if value:
                    return value
            time.sleep(poll)
        detail = f": {last_error}" if last_error else ""
        raise TimeoutError(f"{self.name}: timed out waiting for {what}{detail}")

    def trace_wait(self, predicate, timeout: float, what: str,
                   since_index: int = 0):
        def check():
            for record in self.log_events[since_index:]:
                if predicate(record):
                    return record
            return None
        return self.pump_until(check, timeout, what)

    @property
    def trace_index(self) -> int:
        return len(self.log_events)

    # -- interaction ------------------------------------------------------

    def _escape_pulse(self, attempts: int = 3, gap: float = 0.4) -> bool:
        """Send escape until the app echoes it; a lone ESC too soon after the
        previous one can be swallowed by the terminal escape parser."""
        for _ in range(attempts):
            index = self.trace_index
            self.send_special(b"\x1b")
            try:
                self.trace_wait(lambda r: r["kind"] == "key" and r["key"] == "escape",
                                2.0, "escape key", since_index=index)
            except TimeoutError:
                time.sleep(gap)
                continue
            for record in self.log_events[index:]:
                if record["kind"] == "focus":
                    self.focused = record["widget"]
            return True
        return False

    def focus_search(self) -> dict:
        if self.focused == "search-input":
            return {"already": True}
        for _attempt in range(3):
            if self.focused in (None, "search-input"):
                self._escape_pulse()
            index = self.trace_index
            self.send_special(b"/")
            try:
                self.trace_wait(lambda r: r["kind"] == "key" and r["key"] == "slash",
                                2.5, "search key", since_index=index)
                record = self.trace_wait(
                    lambda r: r["kind"] == "focus" and r["widget"] == "search-input",
                    2.5, "search focus", since_index=index)
                self.focused = "search-input"
                return {"focus_ns": record["receipt_ns"]}
            except TimeoutError:
                # "/" went somewhere else; the input may now hold a stray
                # slash, which the next set_query clears
                self.focused = None
                self._escape_pulse()
                time.sleep(0.4)
        raise TimeoutError(f"{self.name}: could not focus the search box")

    def focus_results(self) -> dict:
        if self.focused not in (None, "search-input"):
            return {"already": True}
        for _attempt in range(3):
            index = self.trace_index
            if not self._escape_pulse():
                time.sleep(0.4)
                continue
            try:
                record = self.trace_wait(
                    lambda r: r["kind"] == "focus" and r["widget"] != "search-input",
                    1.5, "results focus", since_index=index)
                self.focused = record["widget"]
                return {"focus_ns": record["receipt_ns"]}
            except TimeoutError:
                # escape was echoed but focus did not move; try again
                time.sleep(0.4)
        return {"already": True}

    def set_query(self, query: str) -> int:
        if self.query:
            # Clear via Textual's "delete everything left" action, then wait
            # for the app to echo that key before typing: binding actions and
            # text insertion are processed asynchronously and a mixed burst
            # can drop characters. The whole query is then written in one
            # chunk, which the input treats as a paste.
            index = self.trace_index
            self.send_special(b"\x1b[F")
            self.send_special(b"\x15")
            try:
                self.trace_wait(lambda r: r["kind"] == "key" and r["key"] == "ctrl+u",
                                2.0, "clear input", since_index=index)
            except TimeoutError:
                self.send(b"\x7f" * (len(self.query) + 1))
            time.sleep(0.15)
            self.query = ""
        if query:
            self.send(query.encode("utf-8"))
            self.query = query
        return self.last_write_ns

    def ready(self, timeout: float = STARTUP_TIMEOUT) -> dict:
        """Interface usable: input round-trip accepted.

        Escape is not consumed by the search box, so it always reaches the
        app; a key sent before the input driver is live is simply retried.
        """
        index = self.trace_index
        deadline = time.monotonic() + timeout
        key = None
        last_send = 0.0
        while time.monotonic() < deadline:
            self.bench.pump()
            for record in self.log_events[index:]:
                if record["kind"] == "key":
                    key = record
                    break
            if key is not None:
                break
            if time.monotonic() - last_send > 1.0:
                self.send(b"\x1b")
                last_send = time.monotonic()
            time.sleep(0.02)
        if key is None:
            raise TimeoutError(f"{self.name}: timed out waiting for input round-trip")
        focus = None
        try:
            focus = self.trace_wait(
                lambda r: r["kind"] == "focus" and r["receipt_ns"] >= key["receipt_ns"],
                3.0, "focus after input", since_index=index)
        except TimeoutError:
            pass
        if focus:
            self.focused = focus["widget"]
        ready_ns = (focus or key)["receipt_ns"]
        return {"ready_ns": ready_ns, "key_ns": key["receipt_ns"],
                "focus": focus["widget"] if focus else None}

    def wait_quiet(self, quiet_s: float, timeout: float) -> bool:
        """Wait until no new trace or mpv activity for `quiet_s` seconds."""
        last = self._activity_stamp()
        deadline = time.monotonic() + timeout
        quiet_started = time.monotonic()
        while time.monotonic() < deadline:
            self.bench.pump()
            stamp = self._activity_stamp()
            if stamp != last:
                last = stamp
                quiet_started = time.monotonic()
            elif time.monotonic() - quiet_started >= quiet_s:
                return True
            time.sleep(0.05)
        return False

    def _activity_stamp(self):
        return (len(self.log_events),
                len(self.mpv.events) if self.mpv else 0,
                len(self.tp_series))

    # -- measured operations ----------------------------------------------

    def search_trial(self, query: str, submit_pending: bool = False,
                     timeout: float = SEARCH_TIMEOUT) -> dict:
        started = None
        for attempt in (1, 2):
            self.focus_search()
            index = self.trace_index
            t0 = self.set_query(query)
            if submit_pending:
                self.send_special(b"\r")
            try:
                started = self.trace_wait(
                    lambda r: r["kind"] == "request_start" and r["cmd"] == "search"
                    and query in r.get("args", ""),
                    6.0 if attempt == 1 else timeout,
                    f"search start for {query!r}", since_index=index)
                break
            except TimeoutError:
                if attempt == 2:
                    raise
                self.focused = None  # stale: force the escape+slash path
                time.sleep(0.3)
        done = self.trace_wait(
            lambda r: r["kind"] in ("request_done", "request_failed")
            and r["cmd"] == "search",
            timeout, f"search completion for {query!r}", since_index=index)
        return {
            "query": query,
            "submit_pending": submit_pending,
            "dispatch_offset_ms": (started["receipt_ns"] - t0) / 1e6,
            "elapsed_ms": (done["receipt_ns"] - t0) / 1e6,
            "app_duration_ms": done["duration_s"] * 1000.0,
            "cache_class": ("hit" if done["kind"] == "request_done"
                            and done["duration_s"] < 0.05 else "miss"),
            "failed": done["kind"] == "request_failed",
            "error": done.get("error"),
        }

    def wait_playback(self, previous_path, t0_ns: int, timeout: float = PLAYBACK_TIMEOUT) -> dict:
        """Selection to playback-ready (IPC proxy).

        A switch only counts once the *new* file is actually playing: the
        path must change and either mpv reports `file-loaded` after the
        action, or the position clock resets (old track ended/stopped). An
        old track's still-advancing clock can never satisfy a new switch.
        """
        result = {}
        previous_tp = None
        for stamp, value in self.tp_series:
            if stamp < t0_ns:
                previous_tp = value
        was_playing = previous_path is not None or previous_tp is not None

        def check():
            events = self.mpv.events_since(t0_ns) if self.mpv else []
            for event in events:
                if event.get("event") == "file-loaded" and "file_loaded_ns" not in result:
                    result["file_loaded_ns"] = event["_receipt_ns"]
                if event.get("event") == "playback-restart" and "restart_ns" not in result:
                    result["restart_ns"] = event["_receipt_ns"]
                if event.get("event") == "end-file":
                    result[event.get("reason", "end")] = event["_receipt_ns"]
            path = self.mpv.get("path") if self.mpv else None
            paused = self.mpv.get("pause") if self.mpv else None
            if path and path != previous_path and "path_change_ns" not in result:
                result["path_change_ns"] = now_ns()
            if not path or path == previous_path:
                return None
            if paused is not False:
                return None
            if len(self.tp_series) < 2:
                return None
            (t_a, v_a), (t_b, v_b) = self.tp_series[-2], self.tp_series[-1]
            if t_b < t0_ns or v_b <= v_a or v_b < 0.05:
                return None
            loaded_after = result.get("file_loaded_ns", 0) >= t0_ns
            clock_reset = (previous_tp is not None and v_b < previous_tp - 1.0)
            if was_playing and not (loaded_after or clock_reset):
                return None  # old track's clock still running: not this switch
            result["advancing_ns"] = t_b
            result["path"] = path
            result["playlist_pos"] = self.mpv.get("playlist-pos")
            return dict(result)

        result_holder = self.pump_until(check, timeout, "playback-ready")
        ready_ns = result_holder["advancing_ns"]
        change_ns = result_holder.get("path_change_ns")
        return {
            "endpoint": "IPC proxy (new file loaded, unpaused, advancing clock)",
            "start_ns": t0_ns,
            "file_loaded_ns": result_holder.get("file_loaded_ns"),
            "restart_ns": result_holder.get("restart_ns"),
            "path_change_ns": change_ns,
            "advancing_ns": ready_ns,
            "elapsed_ms": (ready_ns - t0_ns) / 1e6,
            "state_change_ms": ((change_ns - t0_ns) / 1e6) if change_ns else None,
            "path": result_holder.get("path"),
            "playlist_pos": result_holder.get("playlist_pos"),
        }

    def play_first(self, timeout: float = PLAYBACK_TIMEOUT) -> dict:
        previous_path = self.mpv.get("path") if self.mpv else None
        t0 = self.send_special(b"\r")
        # play is a local mpv command and is not traced; the IPC proxy is
        # the acknowledgment of record here.
        return self.wait_playback(previous_path, t0, timeout)

    def select_result_row(self, row: int = 1, timeout: float = PLAYBACK_TIMEOUT) -> dict:
        self.focus_results()
        for _ in range(row):
            self.send(b"\x1b[B")  # down arrow
        time.sleep(0.2)
        previous_path = self.mpv.get("path") if self.mpv else None
        t0 = self.send_special(b"\r")
        trial = self.wait_playback(previous_path, t0, timeout)
        trial["selected_row"] = row
        return trial

    def switch(self, direction: str, timeout: float = PLAYBACK_TIMEOUT) -> dict:
        """Manual next/prev switch with the new entry confirmed by IPC."""
        assert direction in ("next", "prev")
        self.focus_results()
        previous_pos = self.mpv.get("playlist-pos") if self.mpv else None
        previous_path = self.mpv.get("path") if self.mpv else None
        t0 = self.send_special(b"n" if direction == "next" else b"p")
        trial = self.wait_playback(previous_path, t0, timeout)
        trial["direction"] = direction
        trial["previous_playlist_pos"] = previous_pos
        return trial

    # -- shutdown ----------------------------------------------------------

    def quit(self, timeout: float = 15.0) -> dict:
        result = {"clean": False, "elapsed_ms": None}
        index = self.trace_index
        t0 = now_ns()
        try:
            self.send(b"\x03")
        except (RuntimeError, OSError):
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.bench.pump()
            if not self.alive and not self.scope.populated():
                result["clean"] = True
                break
            time.sleep(0.05)
        result["elapsed_ms"] = (now_ns() - t0) / 1e6
        if not result["clean"]:
            if self.alive:
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.scope.kill_members(signal.SIGTERM)
            time.sleep(0.5)
            self.scope.kill_members(signal.SIGKILL)
        result["scope_empty"] = self.scope.await_empty(10.0)
        result["trace_shutdown"] = any(
            r["kind"] in ("other", "request_done") and "shutdown" in r.get("text", "")
            for r in self.log_events[index:])
        return result

    def close(self) -> None:
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if self.pid is not None:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        self.pty_log.close()
