"""Tests for ytm.daemon.player against a fake mpv IPC socket.

These tests never spawn real mpv for the unit-level checks; they patch
Player._start_mpv to instead spawn a fake unix-socket server that speaks
mpv's newline-delimited JSON IPC protocol, and drive the real Player logic
against it. Reap-on-close is verified separately with a tiny fake "mpv"
process (a shell script) so we can assert no orphan process survives
without depending on real mpv or audio.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ytm import cache
from ytm.daemon.player import PAUSE_OBSERVE_ID, TIME_POS_OBSERVE_ID, Player


class FakeMpvServer:
    """A minimal unix-socket server that mimics mpv's JSON IPC."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.commands = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(socket_path)
        self._server.listen(1)
        self._conn = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        self._conn = conn
        f = conn.makefile("r")
        while True:
            try:
                line = f.readline()
            except OSError:
                return
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            with self._lock:
                self.commands.append(msg["command"])
            reply = json.dumps({"error": "success", "request_id": msg.get("request_id")})
            try:
                conn.sendall((reply + "\n").encode("utf-8"))
            except OSError:
                return

    def send_event(self, event_dict):
        if self._conn is not None:
            self._conn.sendall((json.dumps(event_dict) + "\n").encode("utf-8"))

    def wait_for_commands(self, n, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if len(self.commands) >= n:
                    return list(self.commands)
            time.sleep(0.01)
        with self._lock:
            return list(self.commands)

    def close(self):
        try:
            if self._conn:
                self._conn.close()
        except OSError:
            pass
        try:
            self._server.close()
        except OSError:
            pass


@pytest.fixture
def fake_player(monkeypatch):
    """Create a Player wired to a FakeMpvServer instead of real mpv."""
    servers = {}

    def fake_start_mpv(self):
        # No real subprocess; just spin up the fake IPC server at the
        # socket path Player already generated.
        server = FakeMpvServer(self._socket_path)
        servers["server"] = server
        self._proc = None  # no real subprocess for this fixture

    monkeypatch.setattr(Player, "_start_mpv", fake_start_mpv)

    player = Player()
    yield player, servers["server"]

    servers["server"].close()
    try:
        os.unlink(player._socket_path)
    except OSError:
        pass


def test_load_issues_loadfile_command(fake_player):
    player, server = fake_player
    player.load("https://example.com/stream.m4a")
    cmds = server.wait_for_commands(3)  # 2 observes + loadfile
    assert ["loadfile", "https://example.com/stream.m4a", "replace"] in cmds


def test_pause_resume_toggle_issue_correct_commands(fake_player):
    player, server = fake_player
    player.pause()
    player.resume()
    player.toggle()
    cmds = server.wait_for_commands(5)  # 2 observes + 3 above
    assert ["set_property", "pause", True] in cmds
    assert ["set_property", "pause", False] in cmds
    assert ["cycle", "pause"] in cmds


def test_load_unpauses_mpv_when_paused(fake_player):
    """Regression: a track can load and buffer fully while mpv sits paused
    (a prior deliberate pause, or a restored session), and just sits there
    never playing. `load()` must explicitly tell mpv to unpause -- loading a
    track means the caller intends to play it now."""
    player, server = fake_player
    player.pause()
    server.wait_for_commands(3)  # 2 observes + pause
    player.load("https://example.com/stream.m4a")
    cmds = server.wait_for_commands(5)  # 2 observes + pause + loadfile + unpause
    load_index = cmds.index(
        ["loadfile", "https://example.com/stream.m4a", "replace"]
    )
    # the unpause must come from load() itself, i.e. some command after the
    # loadfile explicitly clears pause.
    assert ["set_property", "pause", False] in cmds[load_index:]


def test_paused_property_reflects_pause_resume_toggle_and_load(fake_player):
    player, server = fake_player
    assert player.paused is False
    player.pause()
    assert player.paused is True
    player.resume()
    assert player.paused is False
    player.pause()
    player.toggle()
    assert player.paused is False
    player.pause()
    assert player.paused is True
    player.load("https://example.com/stream.m4a")
    assert player.paused is False


def test_seek_relative_and_absolute(fake_player):
    player, server = fake_player
    player.seek(10)
    player.seek(30, absolute=True)
    cmds = server.wait_for_commands(4)
    assert ["seek", 10, "relative"] in cmds
    assert ["seek", 30, "absolute"] in cmds


def test_volume_clamped_low_and_high(fake_player):
    player, server = fake_player
    player.set_volume(-50)
    player.set_volume(500)
    cmds = server.wait_for_commands(4)
    assert ["set_property", "volume", 0] in cmds
    assert ["set_property", "volume", 100] in cmds


def test_eof_event_dispatched_to_observer(fake_player):
    player, server = fake_player

    received = threading.Event()

    def on_eof():
        received.set()

    player.on_eof(on_eof)
    # give the reader thread a moment to be attached and receiving
    time.sleep(0.1)
    server.send_event({"event": "end-file", "reason": "eof"})
    assert received.wait(timeout=2.0)


def test_stop_reason_does_not_fire_eof_callback(fake_player):
    """Regression: `load()` issues `loadfile ... replace` on every play/next,
    which makes real mpv emit end-file with reason "stop" -- not "eof".
    Before the fix, `_handle_event` fired on_eof for *any* end-file event,
    so this "stop" (the direct result of our own replace) was treated as a
    real end-of-track and immediately triggered another advance/replace,
    which produced another "stop", forever: a self-feeding runaway skip
    through the whole queue. This must not fire on_eof at all."""
    player, server = fake_player

    called = threading.Event()
    player.on_eof(called.set)
    time.sleep(0.1)
    server.send_event({"event": "end-file", "reason": "stop"})
    assert not called.wait(timeout=0.5)


def test_error_reason_fires_on_error_callback_not_eof(fake_player):
    """A stream that fails to load emits end-file reason "error". It must
    not be silently treated as an eof (which would advance blindly); it
    should be surfaced via on_error instead."""
    player, server = fake_player

    eof_called = threading.Event()
    error_messages = []
    player.on_eof(eof_called.set)
    player.on_error(error_messages.append)
    time.sleep(0.1)
    server.send_event(
        {"event": "end-file", "reason": "error", "file_error": "loading failed"}
    )
    time.sleep(0.3)
    assert not eof_called.is_set()
    assert error_messages == ["loading failed"]


def test_position_observer_dispatched(fake_player):
    player, server = fake_player

    positions = []

    def on_pos(value):
        positions.append(value)

    player.on_position(on_pos)
    time.sleep(0.1)
    server.send_event(
        {"event": "property-change", "id": TIME_POS_OBSERVE_ID, "name": "time-pos", "data": 42.5}
    )
    deadline = time.time() + 2.0
    while time.time() < deadline and not positions:
        time.sleep(0.01)
    assert positions == [42.5]


def test_load_plays_cached_file_when_video_id_cached(fake_player, monkeypatch, tmp_path):
    player, server = fake_player
    cached_path = tmp_path / "cached123.m4a"
    cached_path.write_bytes(b"audio")
    monkeypatch.setattr(cache, "get_cached_path", lambda video_id, **kw: cached_path)

    player.load("https://example.com/should-be-ignored.m4a", video_id="cached123")
    cmds = server.wait_for_commands(3)
    assert ["loadfile", str(cached_path), "replace"] in cmds
    assert ["loadfile", "https://example.com/should-be-ignored.m4a", "replace"] not in cmds


def test_load_falls_through_to_url_when_not_cached(fake_player, monkeypatch):
    player, server = fake_player
    monkeypatch.setattr(cache, "get_cached_path", lambda video_id, **kw: None)

    player.load("https://example.com/stream.m4a", video_id="uncached123")
    cmds = server.wait_for_commands(3)
    assert ["loadfile", "https://example.com/stream.m4a", "replace"] in cmds


def test_close_reaps_subprocess_no_orphan():
    """Use a real subprocess (a tiny long-running script, not real mpv) to
    verify Player.close() terminates the process it spawned and leaves no
    orphan behind."""
    socket_path = os.path.join(tempfile.gettempdir(), "ytm-test-reap.sock")
    try:
        os.unlink(socket_path)
    except OSError:
        pass

    # A stand-in for mpv: a script that creates the ipc socket file (so
    # Player._connect can succeed) and then idles until killed.
    script = f"""
import socket, time, os
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind({socket_path!r})
s.listen(1)
while True:
    time.sleep(0.1)
"""
    fake_bin = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    )
    fake_bin.write(script)
    fake_bin.close()

    class FakePlayer(Player):
        def _start_mpv(self):
            self._proc = subprocess.Popen([sys.executable, fake_bin.name])

    pid = None
    try:
        # Build the object manually mirroring Player.__init__ but with a
        # fixed, known socket path matching the fake mpv script above.
        obj = FakePlayer.__new__(FakePlayer)
        obj._mpv_bin = "unused"
        obj._socket_path = socket_path
        obj._proc = None
        obj._sock = None
        obj._sock_file = None
        obj._lock = threading.Lock()
        obj._reader_thread = None
        obj._stop_reader = threading.Event()
        obj._request_id = 0
        obj._on_position = None
        obj._on_eof = None

        obj._start_mpv()
        pid = obj._proc.pid
        obj._connect()
        obj._observe_property("time-pos", TIME_POS_OBSERVE_ID)

        # confirm process alive before close
        assert obj._proc.poll() is None

        obj.close()

        # After close, process must be reaped (no longer running).
        time.sleep(0.2)
        alive = False
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert not alive
    finally:
        try:
            os.unlink(fake_bin.name)
        except OSError:
            pass
        try:
            os.unlink(socket_path)
        except OSError:
            pass


def _wait_for(predicate, timeout=2.0):
    """Poll until `predicate` holds; the reader thread applies events async."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_player_observes_the_pause_property(fake_player):
    player, server = fake_player
    cmds = server.wait_for_commands(2)
    assert ["observe_property", PAUSE_OBSERVE_ID, "pause"] in cmds


def test_pause_state_follows_mpv_not_only_our_own_commands(fake_player):
    """mpv owns the pause state. The mirror kept inside Player is written
    optimistically by pause/resume/toggle/load so reads are immediate, but
    mpv's own report is what settles it."""
    player, server = fake_player
    server.wait_for_commands(2)
    assert player.paused is False

    server.send_event(
        {"event": "property-change", "id": PAUSE_OBSERVE_ID, "name": "pause", "data": True}
    )
    assert _wait_for(lambda: player.paused is True)

    server.send_event(
        {"event": "property-change", "id": PAUSE_OBSERVE_ID, "name": "pause", "data": False}
    )
    assert _wait_for(lambda: player.paused is False)


def test_mpv_report_overrides_a_locally_derived_pause_value(fake_player):
    """`toggle()` cannot know mpv's state -- it flips whatever the mirror
    already held, so a wrong value stays wrong and inverts on every toggle.
    mpv's own report has to win over that derived value."""
    player, server = fake_player
    server.wait_for_commands(2)
    player.toggle()
    assert player.paused is True  # derived from the mirror, not from mpv

    # mpv reports it is in fact still playing.
    server.send_event(
        {"event": "property-change", "id": PAUSE_OBSERVE_ID, "name": "pause", "data": False}
    )
    assert _wait_for(lambda: player.paused is False)


def test_pause_observation_does_not_disturb_position_reporting(fake_player):
    """Both properties are observed; the pause branch must not swallow
    time-pos updates."""
    player, server = fake_player
    seen = []
    player.on_position(seen.append)
    server.wait_for_commands(2)
    server.send_event(
        {"event": "property-change", "id": PAUSE_OBSERVE_ID, "name": "pause", "data": True}
    )
    server.send_event(
        {"event": "property-change", "id": TIME_POS_OBSERVE_ID, "name": "time-pos", "data": 12.5}
    )
    assert _wait_for(lambda: seen == [12.5])
    assert player.paused is True


# -- the reader thread must outlive a failing observer ---------------------


_poll_until = _wait_for


def _raise(message):
    def observer(*args):
        raise RuntimeError(message)

    return observer


def test_reader_thread_survives_an_observer_that_raises(fake_player):
    """Regression: observers do real work on the IPC reader thread -- the
    queue resolves a stream URL inside `on_eof`, and yt-dlp raises when it
    gives up. Letting that escape killed the reader, and the daemon went
    deaf to mpv for the rest of its life: no position, no end-of-track, no
    errors, playback silently stuck with nothing reported."""
    player, server = fake_player
    server.wait_for_commands(1)
    player.on_eof(_raise("resolver gave up"))
    positions = []
    player.on_position(positions.append)

    server.send_event({"event": "end-file", "reason": "eof"})
    server.send_event(
        {"event": "property-change", "id": 1, "name": "time-pos", "data": 42.0}
    )
    assert _poll_until(lambda: positions == [42.0])
    assert player._reader_thread.is_alive()


def test_observer_failure_is_logged_with_its_traceback(fake_player, caplog):
    """Anything reaching the backstop is a bug in an observer, not a
    playback failure, so it must not be reshaped into one -- the queue would
    respond to our own crash by skipping the user's music. It is logged
    instead, with the traceback, so it does not vanish."""
    player, server = fake_player
    server.wait_for_commands(1)
    errors = []
    player.on_error(errors.append)
    player.on_eof(_raise("observer is buggy"))

    with caplog.at_level(logging.ERROR, logger="ytm.daemon.player"):
        server.send_event({"event": "end-file", "reason": "eof"})
        assert _poll_until(lambda: "observer is buggy" in caplog.text)

    assert "Traceback" in caplog.text
    assert errors == []  # not reported as a playback error


def test_reader_thread_survives_a_failing_error_observer(fake_player):
    """The error observer runs the same queue code that just failed, so it
    can fail too. That second failure must not kill the reader either."""
    player, server = fake_player
    server.wait_for_commands(1)
    player.on_error(_raise("skip failed too"))
    player.on_eof(_raise("resolver gave up"))
    positions = []
    player.on_position(positions.append)

    server.send_event({"event": "end-file", "reason": "eof"})
    server.send_event(
        {"event": "property-change", "id": 1, "name": "time-pos", "data": 7.0}
    )
    assert _poll_until(lambda: positions == [7.0])
    assert player._reader_thread.is_alive()
