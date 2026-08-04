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


class ClientError(Exception):
    """A clear, user-facing error: the daemon could not be reached or started."""


class Client:
    """A connection to the ytm daemon.

    Sends requests and correlates responses by ``id``; event pushes that
    arrive interleaved with responses are routed to subscribed callbacks
    instead of being mistaken for a response.
    """

    def __init__(self, path=None, auto_spawn=True):
        self._path = path if path is not None else socket_path()
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()
        self._next_id = 1
        self._subscribers = []
        self._connect(auto_spawn=auto_spawn)

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

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

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
            try:
                chunk = self._sock.recv(65536)
            except OSError as exc:
                raise ClientError(f"lost connection to the ytm daemon: {exc}") from None
            if not chunk:
                raise ClientError("the ytm daemon closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def request(self, cmd, args=None):
        """Send one command and return its response's `data`, blocking.

        Any event pushes that arrive first (or interleaved) are dispatched
        to subscribers and skipped over, rather than mistaken for the
        response.
        """
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = json.dumps({"id": request_id, "cmd": cmd, "args": args or {}})
            try:
                self._sock.sendall(payload.encode("utf-8") + b"\n")
            except OSError as exc:
                raise ClientError(f"could not reach the ytm daemon: {exc}") from None
            while True:
                message = self._readline()
                if "event" in message:
                    self._dispatch_event(message)
                    continue
                if message.get("id") != request_id:
                    # a response for some earlier, already-abandoned request
                    continue
                if not message.get("ok"):
                    raise ClientError(message.get("error") or f"'{cmd}' failed")
                return message.get("data")

    def listen(self):
        """Block, dispatching every pushed event to subscribers.

        For the TUI's use: run this in a background thread once subscribed.
        """
        while True:
            message = self._readline()
            if "event" in message:
                self._dispatch_event(message)
