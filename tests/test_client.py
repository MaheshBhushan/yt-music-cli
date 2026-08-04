"""Tests for ytm.client: offline, against a stub socket server we control.

No real daemon, no network. Covers request/response id-correlation, event
delivery interleaved with responses, auto-spawning the daemon, and a clean
error when the daemon cannot be reached or started.
"""

import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ytm.client import Client, ClientError


class StubServer:
    """A tiny unix-socket server that speaks the daemon's line protocol.

    `script` is a list of callables `(conn) -> None` run one per accepted
    connection, in order, letting each test control exactly what bytes are
    sent back and when.
    """

    def __init__(self, path, handler):
        self._path = path
        self._handler = handler
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            self._handler(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self._sock.close()
        self._thread.join(timeout=1)


def _send(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _recv_line(conn):
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf += chunk
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "ytmd.sock"


def test_request_response_round_trip(sock_path):
    def handler(conn):
        request = _recv_line(conn)
        assert request["cmd"] == "status"
        _send(conn, {"id": request["id"], "ok": True, "data": {"paused": True}})

    server = StubServer(sock_path, handler)
    try:
        client = Client(path=sock_path, auto_spawn=False)
        data = client.request("status")
        assert data == {"paused": True}
        client.close()
    finally:
        server.close()


def test_event_before_and_interleaved_with_response_goes_to_callback(sock_path):
    def handler(conn):
        request = _recv_line(conn)
        # an event arrives before the response...
        _send(conn, {"event": "state_changed", "data": {"paused": True}})
        # ...and another interleaved right alongside it.
        _send(conn, {"event": "position", "data": {"position": 1.5}})
        _send(conn, {"id": request["id"], "ok": True, "data": {"ok": True}})

    server = StubServer(sock_path, handler)
    try:
        client = Client(path=sock_path, auto_spawn=False)
        events = []
        client.on_event(lambda name, data: events.append((name, data)))
        data = client.request("pause")
        assert data == {"ok": True}
        assert events == [
            ("state_changed", {"paused": True}),
            ("position", {"position": 1.5}),
        ]
        client.close()
    finally:
        server.close()


def test_request_correlates_by_id_not_first_reply(sock_path):
    def handler(conn):
        request = _recv_line(conn)
        # a stale response for some other id must be skipped, not returned
        _send(conn, {"id": request["id"] + 999, "ok": True, "data": "wrong"})
        _send(conn, {"id": request["id"], "ok": True, "data": "right"})

    server = StubServer(sock_path, handler)
    try:
        client = Client(path=sock_path, auto_spawn=False)
        assert client.request("status") == "right"
        client.close()
    finally:
        server.close()


def test_error_response_raises_client_error(sock_path):
    def handler(conn):
        request = _recv_line(conn)
        _send(conn, {"id": request["id"], "ok": False, "error": "boom"})

    server = StubServer(sock_path, handler)
    try:
        client = Client(path=sock_path, auto_spawn=False)
        with pytest.raises(ClientError, match="boom"):
            client.request("status")
        client.close()
    finally:
        server.close()


def test_unreachable_daemon_without_auto_spawn_raises_clean_error(sock_path):
    with pytest.raises(ClientError):
        Client(path=sock_path, auto_spawn=False)


def test_auto_spawn_starts_a_stub_daemon(sock_path, monkeypatch):
    """auto_spawn calls the daemon-spawning hook; we replace it with a stub
    that opens the socket itself, so we prove the client waits for and then
    connects to a socket that appears after construction begins."""
    spawned = []

    def fake_spawn(self):
        spawned.append(True)

        def start_late():
            time.sleep(0.2)
            server = StubServer(sock_path, lambda conn: _send(
                conn, {"id": _recv_line(conn)["id"], "ok": True, "data": "up"}
            ))
            # leak intentionally; process-local test, closed by tmp_path cleanup
            self._test_server = server

        threading.Thread(target=start_late, daemon=True).start()

    monkeypatch.setattr(Client, "_spawn_daemon", fake_spawn)
    client = Client(path=sock_path, auto_spawn=True)
    assert spawned == [True]
    assert client.request("status") == "up"
    client.close()


def test_unstartable_daemon_raises_clean_error_not_traceback(sock_path, monkeypatch):
    def fake_spawn(self):
        pass  # never actually starts anything; socket never appears

    monkeypatch.setattr(Client, "_spawn_daemon", fake_spawn)
    monkeypatch.setattr("ytm.client.SPAWN_TIMEOUT", 0.3)
    with pytest.raises(ClientError):
        Client(path=sock_path, auto_spawn=True)
