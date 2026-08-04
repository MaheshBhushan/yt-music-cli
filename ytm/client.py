"""Client for the daemon's newline-delimited JSON socket protocol.

Used by both the one-shot CLI commands and the TUI. Reuses
``ytm.daemon.server.socket_path`` for the socket location rather than
duplicating it, and auto-spawns ``ytmd`` if the socket is not there.
"""

import json
import socket
import subprocess
import sys
import threading
import time

from ytm.daemon.server import socket_path

#: how long to wait for a freshly spawned daemon to open its socket
SPAWN_TIMEOUT = 10.0

#: how long a single request waits for its response before giving up
REQUEST_TIMEOUT = 60.0


class ClientError(Exception):
    """A clear, user-facing error: the daemon could not be reached or started."""


class Client:
    """A connection to the ytm daemon.

    A single background reader thread owns every socket read: it routes
    responses to the waiting caller by ``id`` and dispatches event pushes
    to subscribed callbacks, so no message is lost or misrouted even when
    several threads are talking to the daemon at once.
    """

    def __init__(self, path=None, auto_spawn=True):
        self._path = path if path is not None else socket_path()
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()
        self._next_id = 1
        self._subscribers = []
        self._pending = {}
        self._reader = None
        self._reader_error = None
        self._stopped = threading.Event()
        self._connect(auto_spawn=auto_spawn)
        self._start_reader()

    # -- connection ----------------------------------------------------

    def _connect(self, auto_spawn=True):
        try:
            self._open()
            return
        except OSError:
            pass
        if not auto_spawn:
            raise ClientError(
                f"cannot reach the ytm daemon at {self._path}. "
                "Start it with 'ytmd' and try again."
            )
        self._spawn_daemon()
        deadline = time.monotonic() + SPAWN_TIMEOUT
        last_error = None
        while time.monotonic() < deadline:
            try:
                self._open()
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.1)
        raise ClientError(
            f"could not start the ytm daemon (socket never appeared at "
            f"{self._path}): {last_error}"
        )

    def _open(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self._path))
        self._sock = sock

    def _spawn_daemon(self):
        try:
            subprocess.Popen(
                [sys.executable, "-m", "ytm.daemon.server"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise ClientError(f"could not spawn the ytm daemon: {exc}") from None

    def _start_reader(self):
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        """The one and only reader of the socket."""
        try:
            while True:
                message = self._readline()
                if "event" in message:
                    self._dispatch_event(message)
                    continue
                self._deliver_response(message)
        except ClientError as exc:
            error = exc
        except (OSError, ValueError) as exc:
            error = ClientError(f"lost connection to the ytm daemon: {exc}")
        else:  # pragma: no cover - the loop only exits by raising
            error = ClientError("the ytm daemon connection ended")
        self._fail_pending(error)

    def _deliver_response(self, message):
        with self._lock:
            slot = self._pending.pop(message.get("id"), None)
        if slot is None:
            # a response for some earlier, already-abandoned request
            return
        slot["message"] = message
        slot["ready"].set()

    def _fail_pending(self, error):
        with self._lock:
            self._reader_error = error
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = error
            slot["ready"].set()
        self._stopped.set()

    def close(self):
        sock = self._sock
        if sock is not None:
            self._sock = None
            try:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            finally:
                sock.close()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        self._fail_pending(ClientError("the client connection was closed"))

    # -- events ----------------------------------------------------------

    def on_event(self, callback):
        """Register `callback(event_name, data)` for pushed daemon events."""
        self._subscribers.append(callback)

    def _dispatch_event(self, message):
        event = message.get("event")
        data = message.get("data")
        for callback in list(self._subscribers):
            callback(event, data)

    # -- request/response --------------------------------------------------

    def _readline(self):
        """Read one newline-delimited JSON message off the socket."""
        while b"\n" not in self._buf:
            sock = self._sock
            if sock is None:
                raise ClientError("the client connection was closed")
            try:
                chunk = sock.recv(65536)
            except OSError as exc:
                raise ClientError(f"lost connection to the ytm daemon: {exc}") from None
            if not chunk:
                raise ClientError("the ytm daemon closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def request(self, cmd, args=None):
        """Send one command and return its response's `data`, blocking.

        The reader thread routes the response back here by ``id``; event
        pushes that arrive first (or interleaved) go to subscribers.
        """
        slot = {"ready": threading.Event(), "message": None, "error": None}
        with self._lock:
            if self._reader_error is not None:
                raise ClientError(str(self._reader_error))
            sock = self._sock
            if sock is None:
                raise ClientError("the client connection was closed")
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = slot
            payload = json.dumps({"id": request_id, "cmd": cmd, "args": args or {}})
            try:
                sock.sendall(payload.encode("utf-8") + b"\n")
            except OSError as exc:
                self._pending.pop(request_id, None)
                raise ClientError(f"could not reach the ytm daemon: {exc}") from None
        if not slot["ready"].wait(REQUEST_TIMEOUT):
            with self._lock:
                self._pending.pop(request_id, None)
            raise ClientError(f"the ytm daemon did not answer '{cmd}' in time")
        if slot["error"] is not None:
            raise ClientError(str(slot["error"]))
        message = slot["message"]
        if not message.get("ok"):
            raise ClientError(message.get("error") or f"'{cmd}' failed")
        return message.get("data")

    def listen(self):
        """Block until the connection ends, while events reach subscribers.

        Reading is done by the client's own reader thread; this simply
        waits for it, raising `ClientError` when the connection drops.
        """
        self._stopped.wait()
        if self._reader_error is not None:
            raise ClientError(str(self._reader_error))
