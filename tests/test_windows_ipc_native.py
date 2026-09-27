"""W1 native validation: the Windows named-pipe transport itself.

These tests need a real Windows named pipe, so they are skipped everywhere
else with an explicit reason. They exist so the Windows CI job proves the
transport, not merely that a mocked platform branch was taken. A Linux run
of this file reports "skipped", never "passed".

The server is a tiny byte-mode pipe fixture written against the same Win32
APIs mpv uses; it is not a network listener and never touches a real mpv
endpoint.
"""

import ctypes
import gc
import json
import sys
import threading
import time
import uuid
from ctypes import wintypes

import pytest

from ytm.player import Player, PlayerError

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="native Windows named-pipe tests (run in the Windows CI job)",
)

if sys.platform.startswith("win"):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_UNLIMITED_INSTANCES = 255
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_PIPE_CONNECTED = 535

    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]


class PipeServer:
    """A byte-mode named-pipe server with deterministic modes."""

    def __init__(self, mode="reply", reply="ok", split=None):
        self.path = rf"\\.\pipe\ytm-test-{uuid.uuid4().hex}"
        self.mode = mode  # reply | silent | events | close
        self.reply = reply
        self.split = split
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._buffer = b""
        self.handle = kernel32.CreateNamedPipeW(
            self.path, PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, None,
        )
        if self.handle == INVALID_HANDLE_VALUE:
            raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _read_line(self):
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line, self._buffer = self._buffer[:newline], self._buffer[newline + 1:]
                return json.loads(line.decode("utf-8"))
            chunk = ctypes.create_string_buffer(1)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(self.handle, chunk, 1, ctypes.byref(read), None):
                return None
            if read.value == 0:
                return None
            self._buffer += chunk.raw[: read.value]

    def _write(self, data):
        view = memoryview(data)
        while view:
            chunk = ctypes.create_string_buffer(bytes(view))
            written = wintypes.DWORD()
            if not kernel32.WriteFile(
                self.handle, chunk, len(view), ctypes.byref(written), None
            ):
                raise OSError(ctypes.get_last_error(), "WriteFile failed")
            view = view[written.value:]

    def _serve(self):
        connected = kernel32.ConnectNamedPipe(self.handle, None)
        if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
            return
        self.connected.set()
        try:
            if self.mode == "unread":
                self._stop.wait(10)
                return
            while not self._stop.is_set():
                request = self._read_line()
                if request is None:
                    return
                if self.mode == "silent":
                    continue  # consume, never answer
                if self.mode == "close":
                    return
                if self.mode == "events":
                    self._write(json.dumps(
                        {"event": "property-change", "name": "unrelated", "data": 1}
                    ).encode() + b"\n")
                    continue
                payload = json.dumps({
                    "request_id": request.get("request_id"),
                    "error": "success",
                    "data": self.reply,
                }).encode() + b"\n"
                if self.split:
                    self._write(payload[: self.split])
                    time.sleep(0.05)
                    self._write(payload[self.split:])
                else:
                    self._write(payload)
        except OSError:
            pass
        finally:
            kernel32.DisconnectNamedPipe(self.handle)

    def close(self):
        if self.handle is None:
            return
        self._stop.set()
        kernel32.CancelIoEx(self.handle, None)  # unblock connect/read
        self._thread.join(3)
        kernel32.CloseHandle(self.handle)
        self.handle = None


@pytest.fixture
def pipe_server():
    servers = []

    def make(**kwargs):
        server = PipeServer(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _handle_count():
    count = wintypes.DWORD()
    kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    return count.value


def test_windows_pipe_command_times_out_without_reply(pipe_server):
    server = pipe_server(mode="silent")
    player = Player(ipc_path=server.path, spawn=False, timeout=0.3)
    try:
        started = time.monotonic()
        with pytest.raises(PlayerError, match="did not answer within the command timeout"):
            player.command("get_property", "pause")
        assert time.monotonic() - started < 3.0
    finally:
        player.close()


def test_windows_pipe_events_do_not_extend_reply_deadline(pipe_server):
    server = pipe_server(mode="events")
    player = Player(ipc_path=server.path, spawn=False, timeout=0.3)
    try:
        started = time.monotonic()
        with pytest.raises(PlayerError, match="did not answer"):
            player.command("get_property", "pause")
        assert time.monotonic() - started < 3.0
    finally:
        player.close()


def test_windows_pipe_partial_unicode_reply_is_reassembled(pipe_server):
    server = pipe_server(mode="reply", reply="héllo wörld", split=8)
    player = Player(ipc_path=server.path, spawn=False, timeout=2.0)
    try:
        assert player.command("get_property", "media-title") == "héllo wörld"
    finally:
        player.close()


def test_windows_pipe_close_cancels_blocked_observer(pipe_server):
    server = pipe_server(mode="silent")
    player = Player(ipc_path=server.path, spawn=False, timeout=None)
    finished = threading.Event()

    def observe():
        try:
            for _ in player.observe_events("pause"):
                pass
        except PlayerError:
            pass
        finished.set()

    thread = threading.Thread(target=observe, daemon=True)
    thread.start()
    time.sleep(0.2)
    player.close()
    assert finished.wait(3), "close() did not cancel the blocked observer"


def test_windows_pipe_repeated_cycles_do_not_leak_handles(pipe_server):
    # Close test-server handles and join its threads before measuring the
    # client. Python 3.13 retains a native thread handle until join/GC.
    for _ in range(3):
        server = pipe_server(mode="silent")
        player = Player(ipc_path=server.path, spawn=False, timeout=0.1)
        with pytest.raises(PlayerError):
            player.command("get_property", "pause")
        player.close()
        server.close()
    gc.collect()  # discard pytest/context-manager traceback cycles before both samples
    baseline = _handle_count()
    for _ in range(15):
        server = pipe_server(mode="silent")
        player = Player(ipc_path=server.path, spawn=False, timeout=0.1)
        transport = player._transport
        reader = transport._stream._thread
        with pytest.raises(PlayerError):
            player.command("get_property", "pause")
        player.close()
        server.close()
        assert not reader.is_alive(), "cancelled pipe reader survived close"
        assert transport._handle is None and not transport._pending
    gc.collect()
    assert _handle_count() <= baseline + 3


def test_windows_pipe_write_has_a_deadline_when_peer_does_not_read(pipe_server):
    server = pipe_server(mode="unread")
    player = Player(ipc_path=server.path, spawn=False, timeout=0.2)
    try:
        started = time.monotonic()
        with pytest.raises(PlayerError):
            player.command("oversized-test-request", "x" * (1024 * 1024))
        assert time.monotonic() - started < 3
    finally:
        player.close()
