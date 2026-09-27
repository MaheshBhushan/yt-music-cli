"""W1: bounded, cancelable line framing for the mpv transport.

The Windows named-pipe glue cannot be exercised on Linux, so these tests
drive the platform-neutral engine (`LineStream`) with a real OS pipe and a
fake blocking reader, and drive `Player.command` with a fake transport.
The native named-pipe path itself is covered by
`tests/test_windows_ipc_native.py` in the Windows CI job; a green run here
is not native validation.
"""

import json
import os
import threading
import time

import pytest

from ytm import player as player_mod
from ytm.player import Player, PlayerError
from ytm.windows_ipc import LineStream, PipeClosed, PipeTimeout


class PipeReader:
    """A blocking `read_chunk` backed by a real OS pipe.

    Cancellation wakes the blocked reader with one byte (closing a file
    descriptor being read by another thread does not reliably unblock it),
    so the pump always notices the close and exits.
    """

    def __init__(self):
        self.read_fd, self.write_fd = os.pipe()
        self.cancelled = threading.Event()

    def read_chunk(self):
        if self.cancelled.is_set():
            raise OSError("cancelled")
        return os.read(self.read_fd, 4096)

    def cancel(self):
        self.cancelled.set()
        try:
            os.write(self.write_fd, b"x")
        except OSError:
            pass

    def send(self, data):
        os.write(self.write_fd, data)

    def close(self):
        self.cancel()
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _stream(reader, max_buffer=None):
    if max_buffer is None:
        return LineStream(reader.read_chunk, reader.cancel)
    return LineStream(reader.read_chunk, reader.cancel, max_buffer=max_buffer)


def test_partial_frames_are_reassembled():
    reader = PipeReader()
    stream = _stream(reader)
    try:
        reader.send(b'{"a":')
        time.sleep(0.05)
        reader.send(b"1}\n")
        assert stream.readline(2) == '{"a":1}'
    finally:
        stream.close()
        reader.close()


def test_utf8_split_across_chunks_is_reassembled():
    reader = PipeReader()
    stream = _stream(reader)
    try:
        encoded = "héllo\n".encode("utf-8")
        reader.send(encoded[:2])  # splits the two-byte é
        time.sleep(0.05)
        reader.send(encoded[2:])
        assert stream.readline(2) == "héllo"
    finally:
        stream.close()
        reader.close()


def test_several_lines_in_one_chunk_are_preserved():
    reader = PipeReader()
    stream = _stream(reader)
    try:
        reader.send(b"one\ntwo\nthree\n")
        assert [stream.readline(2) for _ in range(3)] == ["one", "two", "three"]
    finally:
        stream.close()
        reader.close()


def test_read_line_times_out_without_a_reply():
    reader = PipeReader()
    stream = _stream(reader)
    try:
        started = time.monotonic()
        with pytest.raises(PipeTimeout):
            stream.readline(0.1)
        assert time.monotonic() - started < 2.0
    finally:
        stream.close()
        reader.close()


def test_timeout_does_not_poison_the_next_line():
    reader = PipeReader()
    stream = _stream(reader)
    try:
        with pytest.raises(PipeTimeout):
            stream.readline(0.05)
        reader.send(b"late\n")
        assert stream.readline(2) == "late"
    finally:
        stream.close()
        reader.close()


def test_close_cancels_a_blocked_reader():
    reader = PipeReader()
    stream = _stream(reader)
    finished = threading.Event()

    def waiter():
        try:
            stream.readline(None)
        except (PipeClosed, OSError):
            pass
        finished.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.05)
    stream.close()
    assert finished.wait(2), "close() did not cancel the blocked reader"
    assert stream.closed
    reader.close()


def test_an_unterminated_oversized_response_is_refused():
    reader = PipeReader()
    stream = _stream(reader, max_buffer=64)
    try:
        reader.send(b"x" * 200)  # no newline anywhere
        with pytest.raises(PipeClosed, match="unterminated"):
            stream.readline(2)
    finally:
        stream.close()
        reader.close()


def test_eof_raises_pipe_closed():
    reader = PipeReader()
    stream = _stream(reader)
    try:
        os.close(reader.write_fd)
        with pytest.raises(PipeClosed):
            stream.readline(2)
    finally:
        stream.close()
        reader.close()


def test_repeated_cycles_do_not_accumulate_threads():
    baseline = threading.active_count()
    for _ in range(30):
        reader = PipeReader()
        stream = _stream(reader)
        reader.send(b"ok\n")
        assert stream.readline(2) == "ok"
        stream.close()
        reader.close()
    # allow the last pump threads a moment to finish their joins
    deadline = time.monotonic() + 2
    while threading.active_count() > baseline + 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= baseline + 1


# -- Player deadline semantics over a fake transport --------------------------


class FakeTransport:
    """A transport whose replies come from a responder callback."""

    def __init__(self):
        self.responder = None
        self.writes = []
        self.timeouts = []
        self.closed = False

    def write_all(self, data):
        self.writes.append(json.loads(data.decode("utf-8")))

    def readline(self, timeout=None):
        self.timeouts.append(timeout)
        if self.responder is not None:
            reply = self.responder(self.writes[-1])
            if reply is not None:
                return json.dumps(reply)
        raise TimeoutError("no reply")

    def close(self):
        self.closed = True


@pytest.fixture
def fake_transport(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(player_mod, "open_transport", lambda path, timeout: transport)
    return transport


def test_command_times_out_with_a_safe_message(fake_transport, tmp_path):
    player = Player(ipc_path=str(tmp_path / "mpv.sock"), spawn=False, timeout=0.15)
    try:
        with pytest.raises(PlayerError, match="did not answer within the command timeout"):
            player.command("get_property", "pause")
    finally:
        player.close()
    assert fake_transport.closed


def test_unrelated_events_do_not_extend_the_command_deadline(fake_transport, tmp_path):
    fake_transport.responder = lambda request: {"request_id": 999_999, "error": "success"}
    player = Player(ipc_path=str(tmp_path / "mpv.sock"), spawn=False, timeout=0.2)
    try:
        started = time.monotonic()
        with pytest.raises(PlayerError, match="did not answer"):
            player.command("get_property", "pause")
        elapsed = time.monotonic() - started
    finally:
        player.close()
    assert elapsed < 2.0, "unrelated events extended the deadline"
    # each read received the remaining budget, not a fresh timeout
    assert fake_transport.timeouts
    assert fake_transport.timeouts[-1] <= fake_transport.timeouts[0]
    assert fake_transport.timeouts[-1] > 0


def test_a_stale_reply_cannot_satisfy_the_next_command(fake_transport, tmp_path):
    stale = {"request_id": 424_242, "error": "success", "data": "stale"}
    fake_transport.responder = lambda request: stale
    player = Player(ipc_path=str(tmp_path / "mpv.sock"), spawn=False, timeout=0.15)
    try:
        with pytest.raises(PlayerError, match="did not answer"):
            player.command("get_property", "pause")
        # the next command replies with its own id and is answered correctly
        fake_transport.responder = lambda request: {
            "request_id": request["request_id"], "error": "success", "data": "fresh",
        }
        assert player.command("get_property", "volume") == "fresh"
    finally:
        player.close()


def test_close_is_idempotent(fake_transport, tmp_path):
    player = Player(ipc_path=str(tmp_path / "mpv.sock"), spawn=False, timeout=0.1)
    player.close()
    player.close()
    assert fake_transport.closed
