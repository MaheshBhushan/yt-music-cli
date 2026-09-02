"""Tests for ytm.daemon.server and ytm.daemon.state.

Everything here is offline: a fake player, a fake queue and a fake
ytmusicapi client. No mpv, no yt-dlp, no network.
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ytm.api import Track
from ytm.auth import AuthExpired
from ytm.daemon import server as server_mod
from ytm.daemon import state as state_mod
from ytm.daemon.player import Player
from ytm.daemon.server import Daemon


def make_track(video_id, title=None):
    return Track(
        video_id=video_id,
        title=title or f"title-{video_id}",
        artist="artist",
        album="album",
        duration="3:00",
        duration_seconds=180,
    )


class FakePlayer:
    """Accepts every Player call the daemon makes and records it."""

    def __init__(self):
        self.calls = []
        self._on_position = None
        self._on_eof = None
        self._on_error = None
        self.closed = False
        self.paused = False

    def load(self, url):
        self.calls.append(("load", url))
        self.paused = False

    def pause(self):
        self.calls.append(("pause",))
        self.paused = True

    def resume(self):
        self.calls.append(("resume",))
        self.paused = False

    def toggle(self):
        self.calls.append(("toggle",))
        self.paused = not self.paused

    def seek(self, amount, absolute=False):
        self.calls.append(("seek", amount, absolute))

    def set_volume(self, level):
        self.calls.append(("volume", level))

    def close(self):
        self.closed = True

    def on_position(self, callback):
        self._on_position = callback

    def on_eof(self, callback):
        self._on_eof = callback

    def on_error(self, callback):
        self._on_error = callback

    def emit_position(self, value):
        self._on_position(value)

    def emit_eof(self):
        self._on_eof()

    def emit_error(self, message=None):
        self._on_error(message)


class FakeQueue:
    """A queue with the public surface the daemon uses, no resolution."""

    def __init__(self, player):
        self._tracks = []
        self._index = -1
        self.player = player
        self._on_error = None
        player.on_eof(lambda: None)
        player.on_position(lambda value: None)

    def on_error(self, callback):
        self._on_error = callback

    @property
    def tracks(self):
        return list(self._tracks)

    @property
    def index(self):
        return self._index

    @property
    def current(self):
        if 0 <= self._index < len(self._tracks):
            return self._tracks[self._index]
        return None

    def __len__(self):
        return len(self._tracks)

    def enqueue(self, tracks, position=None):
        if isinstance(tracks, Track):
            tracks = [tracks]
        tracks = list(tracks)
        was_empty = not self._tracks
        if position is None:
            self._tracks.extend(tracks)
        else:
            position = max(0, min(len(self._tracks), position))
            self._tracks[position:position] = tracks
        if was_empty:
            self._index = 0

    def move(self, from_index, to_index):
        if not (0 <= from_index < len(self._tracks)):
            raise IndexError(from_index)
        self._tracks.insert(to_index, self._tracks.pop(from_index))

    def remove(self, index):
        if not (0 <= index < len(self._tracks)):
            raise IndexError(index)
        self._tracks.pop(index)
        if not self._tracks:
            self._index = -1
        elif self._index >= len(self._tracks):
            self._index = len(self._tracks) - 1

    def clear(self):
        self._tracks = []
        self._index = -1

    def next(self):
        if self._index < len(self._tracks) - 1:
            self._index += 1
        return self.current

    def prev(self):
        if self._index > 0:
            self._index -= 1
        return self.current

    def play_at(self, index):
        if not (0 <= index < len(self._tracks)):
            raise IndexError(index)
        self._index = index
        return self.current


class FakeYT:
    """Stands in for ytmusicapi: search plus get_watch_playlist."""

    def __init__(self, results=None, radio=None, error=None, lyrics=None):
        self.results = results if results is not None else []
        self.radio = radio if radio is not None else []
        self.error = error
        self.lyrics = lyrics
        self.lyrics_calls = 0

    def search(self, query, filter=None, limit=None):
        if self.error:
            raise self.error
        return self.results

    def get_watch_playlist(self, videoId=None):
        data = {"tracks": self.radio}
        if self.lyrics is not None:
            data["lyrics"] = "lyrics-browse-id"
        return data

    def get_lyrics(self, browseId):
        self.lyrics_calls += 1
        if self.lyrics is None:
            return None
        return {"lyrics": self.lyrics, "source": "Source"}


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "ytmd.sock"


def build_daemon(tmp_path, sock_path, yt=None, tracks=()):
    player = FakePlayer()
    queue = FakeQueue(player)
    for track in tracks:
        queue.enqueue(track)
    daemon = Daemon(
        path=sock_path,
        player=player,
        queue=queue,
        state_path=tmp_path / "state.json",
        yt=yt if yt is not None else FakeYT(),
    )
    return daemon, player, queue


class Client:
    """A newline-JSON client over the daemon socket."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.events = []

    async def send_raw(self, text):
        self.writer.write(text.encode("utf-8"))
        await self.writer.drain()

    async def recv(self):
        line = await asyncio.wait_for(self.reader.readline(), timeout=5)
        return json.loads(line)

    async def call(self, request_id, cmd, **args):
        await self.send_raw(json.dumps({"id": request_id, "cmd": cmd, "args": args}) + "\n")
        while True:
            message = await self.recv()
            if "event" in message:
                self.events.append(message)
                continue
            return message

    async def next_event(self, name=None):
        for index, event in enumerate(self.events):
            if name is None or event["event"] == name:
                return self.events.pop(index)
        while True:
            message = await self.recv()
            if "event" in message and (name is None or message["event"] == name):
                return message

    async def close(self):
        self.writer.close()


async def connect(sock_path):
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    # let the server's connection handler register us before events fire
    await asyncio.sleep(0.05)
    return Client(reader, writer)


# -- criterion 1: every command routes ------------------------------------


ALL_COMMANDS = [
    ("search", {"query": "hello", "filter": "songs"}),
    ("play", {"video_id": "vid1"}),
    ("enqueue", {"video_id": "vid2", "position": 0}),
    ("pause", {}),
    ("resume", {}),
    ("toggle", {}),
    ("next", {}),
    ("prev", {}),
    ("seek", {"seconds": 10, "absolute": False}),
    ("volume", {"level": 55}),
    ("status", {}),
    ("queue_get", {}),
    ("queue_move", {"from_index": 0, "to_index": 1}),
    ("queue_remove", {"index": 0}),
    ("radio", {"video_id": "vid1"}),
    ("lyrics", {"video_id": "vid1"}),
    ("queue_clear", {}),
    ("shutdown", {}),
]


@pytest.mark.parametrize("cmd,args", ALL_COMMANDS)
def test_every_command_routes(tmp_path, sock_path, cmd, args):
    yt = FakeYT(
        results=[{"videoId": "s1", "title": "song", "duration_seconds": 100}],
        radio=[{"videoId": "r1", "title": "radio", "duration_seconds": 100}],
    )

    async def scenario():
        daemon, player, queue = build_daemon(tmp_path, sock_path, yt=yt)
        # two tracks pre-seeded so index-taking commands have something to hit
        queue.enqueue([make_track("a"), make_track("b")])
        await daemon.start()
        client = await connect(sock_path)
        response = await client.call(7, cmd, **args)
        await client.close()
        await daemon.stop()
        return response

    response = asyncio.run(scenario())
    assert response["id"] == 7, response
    assert response["ok"] is True, response
    assert "data" in response


def test_all_listed_commands_are_routed(tmp_path, sock_path):
    daemon, _, _ = build_daemon(tmp_path, sock_path)
    expected = {
        "search", "play", "enqueue", "pause", "resume", "toggle", "next", "prev",
        "seek", "volume", "status", "queue_get", "queue_clear", "queue_move",
        "queue_remove", "radio", "lyrics", "shutdown",
        "playlist_list", "playlist_get", "playlist_create", "playlist_add",
        "playlist_remove", "playlist_delete",
    }
    assert set(daemon._routes) == expected


# -- criterion 2: bad input never kills the connection or daemon ----------


def test_bad_input_keeps_connection_and_daemon_alive(tmp_path, sock_path):
    async def scenario():
        daemon, _, queue = build_daemon(tmp_path, sock_path)
        queue.enqueue(make_track("a"))
        await daemon.start()
        client = await connect(sock_path)
        results = []

        await client.send_raw("{not json at all\n")
        results.append(await client.recv())
        results.append(await client.call(2, "status"))

        results.append(await client.call(3, "does_not_exist"))
        results.append(await client.call(4, "status"))

        results.append(await client.call(5, "seek", seconds="ten"))
        results.append(await client.call(6, "status"))

        results.append(await client.call(7, "play"))
        results.append(await client.call(8, "status"))

        results.append(await client.call(9, "queue_remove", index=99))
        results.append(await client.call(10, "status"))

        await client.send_raw(json.dumps({"id": 11}) + "\n")
        results.append(await client.recv())
        results.append(await client.call(12, "status"))

        await client.close()
        await daemon.stop()
        return results

    results = asyncio.run(scenario())
    bad = results[0::2]
    good = results[1::2]
    for response in bad:
        assert response["ok"] is False, response
        assert response["error"]
    for response in good:
        assert response["ok"] is True, response


def test_client_disconnect_midrequest_leaves_daemon_serving(tmp_path, sock_path):
    async def scenario():
        daemon, _, _ = build_daemon(tmp_path, sock_path)
        await daemon.start()
        rude = await connect(sock_path)
        await rude.send_raw(json.dumps({"id": 1, "cmd": "status"}) + "\n")
        rude.writer.close()  # vanish before reading the reply
        await asyncio.sleep(0.05)
        client = await connect(sock_path)
        response = await client.call(2, "status")
        await client.close()
        await daemon.stop()
        return response

    assert asyncio.run(scenario())["ok"] is True


def test_auth_expired_is_a_clean_error(tmp_path, sock_path):
    yt = FakeYT(error=AuthExpired("credentials expired, run 'ytm auth'"))

    async def scenario():
        daemon, _, _ = build_daemon(tmp_path, sock_path, yt=yt)
        await daemon.start()
        client = await connect(sock_path)
        response = await client.call(1, "search", query="x")
        follow_up = await client.call(2, "status")
        await client.close()
        await daemon.stop()
        return response, follow_up

    response, follow_up = asyncio.run(scenario())
    assert response["ok"] is False
    assert response["kind"] == "auth"
    assert "ytm auth" in response["error"]
    assert "Traceback" not in response["error"]
    assert follow_up["ok"] is True


# -- criterion 3: events reach every connected client ---------------------


def test_event_reaches_two_clients(tmp_path, sock_path):
    async def scenario():
        daemon, player, queue = build_daemon(tmp_path, sock_path)
        await daemon.start()
        first = await connect(sock_path)
        second = await connect(sock_path)
        await first.call(1, "play", video_id="vid1", title="Song One")
        events = await asyncio.gather(
            first.next_event("track_changed"), second.next_event("track_changed")
        )
        await first.close()
        await second.close()
        await daemon.stop()
        return events

    events = asyncio.run(scenario())
    for event in events:
        assert event["event"] == "track_changed"
        assert event["data"]["video_id"] == "vid1"
        assert event["data"]["title"] == "Song One"


def test_position_and_state_events_are_pushed(tmp_path, sock_path):
    async def scenario():
        daemon, player, queue = build_daemon(tmp_path, sock_path)
        queue.enqueue(make_track("a"))
        await daemon.start()
        client = await connect(sock_path)
        await client.call(1, "pause")
        state_event = await client.next_event("state_changed")
        player.emit_position(30.0)
        position_event = await client.next_event("position")
        await client.close()
        await daemon.stop()
        return state_event, position_event

    state_event, position_event = asyncio.run(scenario())
    assert state_event["data"]["paused"] is True
    assert position_event["data"]["position"] == 30.0


def test_eof_pushes_track_changed(tmp_path, sock_path):
    async def scenario():
        daemon, player, queue = build_daemon(tmp_path, sock_path)
        queue.enqueue([make_track("a"), make_track("b")])
        await daemon.start()
        client = await connect(sock_path)
        queue.next()
        player.emit_eof()
        event = await client.next_event("track_changed")
        await client.close()
        await daemon.stop()
        return event

    assert asyncio.run(scenario())["data"]["video_id"] == "b"


# -- criterion 4: single instance, stale socket ---------------------------


def test_second_instance_refuses_to_start(tmp_path, sock_path):
    async def scenario():
        daemon, _, _ = build_daemon(tmp_path, sock_path)
        await daemon.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                server_mod.claim_socket(sock_path)
            second, _, _ = build_daemon(tmp_path, sock_path)
            with pytest.raises(RuntimeError, match="already running"):
                await second.start()
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_stale_socket_is_cleaned_up_and_startup_proceeds(tmp_path, sock_path):
    # a socket file bound by a process that is no longer listening
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock_path))
    dead.close()
    assert sock_path.exists()
    assert server_mod.is_socket_alive(sock_path) is False

    async def scenario():
        daemon, _, _ = build_daemon(tmp_path, sock_path)
        await daemon.start()
        client = await connect(sock_path)
        response = await client.call(1, "status")
        await client.close()
        await daemon.stop()
        return response

    assert asyncio.run(scenario())["ok"] is True
    assert not sock_path.exists()


def test_socket_path_falls_back_without_xdg_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = server_mod.socket_path()
    assert path.name == "ytmd.sock"
    assert str(path.parent.parent).startswith(("/tmp", "/var"))

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert str(server_mod.socket_path()) == "/run/user/1000/ytm/ytmd.sock"


# -- criterion 5: state round-trips, no URLs, atomic write ----------------


def test_state_round_trips_across_restart(tmp_path, sock_path):
    state_path = tmp_path / "state.json"

    async def first_run():
        player = FakePlayer()
        queue = FakeQueue(player)
        daemon = Daemon(
            path=sock_path, player=player, queue=queue, state_path=state_path,
            yt=FakeYT(),
        )
        await daemon.start()
        client = await connect(sock_path)
        await client.call(1, "enqueue", video_id="a", title="A")
        await client.call(2, "enqueue", video_id="b", title="B")
        await client.call(3, "next")
        await client.call(4, "volume", level=42)
        await client.close()
        await daemon.stop()

    asyncio.run(first_run())

    player = FakePlayer()
    queue = FakeQueue(player)
    restored = Daemon(
        path=sock_path, player=player, queue=queue, state_path=state_path,
        yt=FakeYT(),
    )
    restored.restore()
    assert [t.video_id for t in queue.tracks] == ["a", "b"]
    assert queue.index == 1
    assert restored._status_data()["volume"] == 42
    assert restored._status_data()["last_played"] == "b"
    # restoring must not start playback (that would resolve a stream URL)
    assert ("load", ) not in [(c[0],) for c in player.calls]


# -- config wiring: autoplay_radio, confirm_remote_delete, volume ---------


def test_config_autoplay_radio_reaches_the_real_queue(tmp_path, sock_path):
    from ytm.daemon.queue import Queue

    player = FakePlayer()
    daemon = Daemon(
        path=sock_path,
        player=player,
        state_path=tmp_path / "state.json",
        yt=FakeYT(),
        config={
            "audio": {"volume": 70, "device": "auto"},
            "behaviour": {"autoplay_radio": False, "confirm_remote_delete": True},
            "ui": {"theme": "dark"},
            "keys": {"toggle": "space", "next": "n", "prev": "p", "search": "/", "quit": "q"},
        },
    )
    assert isinstance(daemon._queue, Queue)
    assert daemon._queue._autoplay_radio is False


class _RemotePlaylistYT:
    """Stands in for ytmusicapi's playlist deletion/removal calls."""

    def __init__(self):
        self.deleted = []
        self.removed = []

    def get_playlist(self, playlist_id, limit=None):
        return {"tracks": [{"videoId": "v1"}]}

    def remove_playlist_items(self, playlist_id, items):
        self.removed.append((playlist_id, items))
        return {}

    def delete_playlist(self, playlist_id):
        self.deleted.append(playlist_id)
        return {}


def _daemon_with_confirm_config(tmp_path, sock_path, confirm_remote_delete, yt):
    player = FakePlayer()
    queue = FakeQueue(player)
    return Daemon(
        path=sock_path,
        player=player,
        queue=queue,
        state_path=tmp_path / "state.json",
        yt=yt,
        config={
            "audio": {"volume": 70, "device": "auto"},
            "behaviour": {
                "autoplay_radio": True,
                "confirm_remote_delete": confirm_remote_delete,
            },
            "ui": {"theme": "dark"},
            "keys": {"toggle": "space", "next": "n", "prev": "p", "search": "/", "quit": "q"},
        },
    )


def test_confirm_remote_delete_default_true_enforces_the_gate(tmp_path, sock_path):
    yt = _RemotePlaylistYT()
    daemon = _daemon_with_confirm_config(tmp_path, sock_path, True, yt)
    with pytest.raises(server_mod.ProtocolError):
        daemon._cmd_playlist_delete({"playlist_id": "remote-1"})
    assert yt.deleted == []
    with pytest.raises(server_mod.ProtocolError):
        daemon._cmd_playlist_remove({"playlist_id": "remote-1", "video_ids": ["v1"]})
    assert yt.removed == []
    # confirm=true still goes through under the default
    daemon._cmd_playlist_delete({"playlist_id": "remote-1", "confirm": True})
    assert yt.deleted == ["remote-1"]


def test_confirm_remote_delete_false_relaxes_the_gate(tmp_path, sock_path):
    yt = _RemotePlaylistYT()
    daemon = _daemon_with_confirm_config(tmp_path, sock_path, False, yt)
    daemon._cmd_playlist_delete({"playlist_id": "remote-1"})
    assert yt.deleted == ["remote-1"]
    daemon._cmd_playlist_remove({"playlist_id": "remote-1", "video_ids": ["v1"]})
    assert yt.removed


def test_state_volume_wins_over_config_default_when_state_exists(tmp_path, sock_path):
    state_path = tmp_path / "state.json"
    state_mod.save({"tracks": [], "index": -1, "volume": 33, "last_played": None}, state_path)
    player = FakePlayer()
    queue = FakeQueue(player)
    daemon = Daemon(
        path=sock_path, player=player, queue=queue, state_path=state_path, yt=FakeYT(),
        config={
            "audio": {"volume": 70, "device": "auto"},
            "behaviour": {"autoplay_radio": True, "confirm_remote_delete": True},
            "ui": {"theme": "dark"},
            "keys": {"toggle": "space", "next": "n", "prev": "p", "search": "/", "quit": "q"},
        },
    )
    daemon.restore()
    assert daemon._volume == 33


def test_config_volume_used_when_no_state_file_exists(tmp_path, sock_path):
    state_path = tmp_path / "state.json"  # never written
    player = FakePlayer()
    queue = FakeQueue(player)
    daemon = Daemon(
        path=sock_path, player=player, queue=queue, state_path=state_path, yt=FakeYT(),
        config={
            "audio": {"volume": 55, "device": "auto"},
            "behaviour": {"autoplay_radio": True, "confirm_remote_delete": True},
            "ui": {"theme": "dark"},
            "keys": {"toggle": "space", "next": "n", "prev": "p", "search": "/", "quit": "q"},
        },
    )
    daemon.restore()
    assert daemon._volume == 55


def test_state_file_never_contains_a_stream_url(tmp_path):
    state_path = tmp_path / "state.json"
    track = make_track("a")
    state_mod.save(
        {"tracks": [track], "index": 0, "volume": 80, "last_played": "a"}, state_path
    )
    raw = state_path.read_text(encoding="utf-8")
    assert "http" not in raw
    for key in json.loads(raw)["tracks"][0]:
        assert key in state_mod._TRACK_FIELDS


def test_interrupted_write_does_not_corrupt_existing_state(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    good = {"tracks": [make_track("a")], "index": 0, "volume": 30, "last_played": "a"}
    state_mod.save(good, state_path)
    before = state_path.read_text(encoding="utf-8")

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("crashed before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        state_mod.save({"tracks": [], "index": -1, "volume": 99}, state_path)
    monkeypatch.setattr(os, "replace", real_replace)

    assert state_path.read_text(encoding="utf-8") == before
    loaded = state_mod.load(state_path)
    assert [t.video_id for t in loaded["tracks"]] == ["a"]
    assert loaded["volume"] == 30


def test_load_tolerates_corrupt_state(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"tracks": [{"vid', encoding="utf-8")
    assert state_mod.load(state_path) == state_mod.empty()
    assert state_mod.load(tmp_path / "missing.json") == state_mod.empty()


# -- shutdown ------------------------------------------------------------


def test_shutdown_stops_serving_and_removes_socket(tmp_path, sock_path):
    async def scenario():
        daemon, player, _ = build_daemon(tmp_path, sock_path)
        await daemon.start()
        client = await connect(sock_path)
        response = await client.call(1, "shutdown")
        await asyncio.wait_for(daemon.serve_forever(), timeout=5)
        await daemon.stop()
        return response, player

    response, player = asyncio.run(scenario())
    assert response["ok"] is True
    assert not sock_path.exists()
    # the daemon does not own an injected player, so it must not close it
    assert player.closed is False
    assert ("pause",) in player.calls


class _StuckThenKillableProc:
    """A stand-in mpv process that ignores SIGTERM within Player.close()'s
    5s grace period and, even after SIGKILL, isn't reaped by the first
    post-kill wait() either -- exactly the case that let a live mpv process
    survive Daemon.stop() silently, because the second TimeoutExpired had
    nowhere left to go but out through the `contextlib.suppress` around
    Player.close()."""

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_calls <= 2:
            raise subprocess.TimeoutExpired(cmd="mpv", timeout=timeout or 0)
        return 0


def test_shutdown_reaps_a_slow_to_die_mpv_process(tmp_path, sock_path, monkeypatch):
    """Daemon-level regression: a daemon-owned Player whose mpv doesn't die
    within the terminate+wait grace period, and needs a second wait even
    after SIGKILL, must still end up fully reaped once Daemon.stop() (the
    real `shutdown` command's teardown path) returns -- not abandoned as a
    live orphan because the failure was swallowed by
    `contextlib.suppress(Exception)` in Daemon.stop().
    """
    fake_proc = _StuckThenKillableProc()
    monkeypatch.setattr(
        Player, "_start_mpv", lambda self: setattr(self, "_proc", fake_proc)
    )
    monkeypatch.setattr(Player, "_connect", lambda self, timeout=5.0: None)
    monkeypatch.setattr(
        Player, "_observe_property", lambda self, name, observe_id=1: None
    )

    async def scenario():
        # No player/queue injected: the daemon builds and owns its own
        # Player, exactly as it does in production via ytm.daemon.server.run().
        daemon = Daemon(
            path=sock_path,
            state_path=tmp_path / "state.json",
            yt=FakeYT(),
        )
        await daemon.start()
        client = await connect(sock_path)
        response = await client.call(1, "shutdown")
        await asyncio.wait_for(daemon.serve_forever(), timeout=5)
        await daemon.stop()
        return response, daemon

    response, daemon = asyncio.run(scenario())
    assert response["ok"] is True
    assert fake_proc.terminated is True
    assert fake_proc.killed is True
    # the process must have been actually reaped, not abandoned after the
    # second TimeoutExpired -- this is the orphan the bug report described.
    assert fake_proc.wait_calls == 3
    assert daemon._player._proc is None


# -- signal handling -------------------------------------------------------


class _CleanProc:
    """A stand-in mpv process that dies immediately on terminate()."""

    def __init__(self):
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_sigterm_stops_daemon_and_reaps_mpv(tmp_path, sock_path, monkeypatch):
    """A daemon run through `server_mod.run()` (the real entry point `main()`
    drives) must, on SIGTERM, run the same teardown `shutdown` uses -- close
    the player, reap mpv, save state and remove the socket -- rather than
    dying at the OS's default disposition and orphaning mpv.
    """
    import signal

    fake_proc = _CleanProc()
    monkeypatch.setattr(
        Player, "_start_mpv", lambda self: setattr(self, "_proc", fake_proc)
    )
    monkeypatch.setattr(Player, "_connect", lambda self, timeout=5.0: None)
    monkeypatch.setattr(
        Player, "_observe_property", lambda self, name, observe_id=1: None
    )
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", state_path)

    async def scenario():
        task = asyncio.ensure_future(server_mod.run(path=sock_path))
        for _ in range(100):
            if sock_path.exists():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("daemon never bound its socket")
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())
    assert result == 0
    assert not sock_path.exists()
    assert fake_proc.terminated is True
    assert state_path.exists()


def test_sigint_stops_daemon_and_reaps_mpv(tmp_path, sock_path, monkeypatch):
    """Same teardown guarantee as SIGTERM, for SIGINT (Ctrl-C)."""
    import signal

    fake_proc = _CleanProc()
    monkeypatch.setattr(
        Player, "_start_mpv", lambda self: setattr(self, "_proc", fake_proc)
    )
    monkeypatch.setattr(Player, "_connect", lambda self, timeout=5.0: None)
    monkeypatch.setattr(
        Player, "_observe_property", lambda self, name, observe_id=1: None
    )
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", state_path)

    async def scenario():
        task = asyncio.ensure_future(server_mod.run(path=sock_path))
        for _ in range(100):
            if sock_path.exists():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("daemon never bound its socket")
        os.kill(os.getpid(), signal.SIGINT)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())
    assert result == 0
    assert not sock_path.exists()
    assert fake_proc.terminated is True
    assert state_path.exists()


def test_double_sigterm_during_shutdown_does_not_hang(tmp_path, sock_path, monkeypatch):
    """Two signals in a row -- or a signal arriving while a `shutdown`
    command is already in flight -- must not hang or crash the teardown.
    """
    import signal

    fake_proc = _CleanProc()
    monkeypatch.setattr(
        Player, "_start_mpv", lambda self: setattr(self, "_proc", fake_proc)
    )
    monkeypatch.setattr(Player, "_connect", lambda self, timeout=5.0: None)
    monkeypatch.setattr(
        Player, "_observe_property", lambda self, name, observe_id=1: None
    )
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", state_path)

    async def scenario():
        task = asyncio.ensure_future(server_mod.run(path=sock_path))
        for _ in range(100):
            if sock_path.exists():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("daemon never bound its socket")
        # a `shutdown` command in flight, immediately followed by two signals
        client = await connect(sock_path)
        send_shutdown = asyncio.ensure_future(client.call(1, "shutdown"))
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)
        result = await asyncio.wait_for(task, timeout=5)
        with contextlib.suppress(Exception):
            await send_shutdown
        return result

    result = asyncio.run(scenario())
    assert result == 0
    assert not sock_path.exists()


# -- play must not duplicate a track already in the queue -----------------


def test_play_existing_track_moves_cursor_without_duplicating(tmp_path, sock_path):
    daemon, player, queue = build_daemon(tmp_path, sock_path)
    queue.enqueue([make_track("a"), make_track("b"), make_track("c")])
    queue._index = 0
    daemon._cmd_play({"video_id": "c"})
    assert [t.video_id for t in queue.tracks] == ["a", "b", "c"]
    assert queue.index == 2


def test_play_existing_track_jumps_to_first_occurrence(tmp_path, sock_path):
    daemon, player, queue = build_daemon(tmp_path, sock_path)
    queue.enqueue([make_track("a"), make_track("b"), make_track("a")])
    queue._index = 1
    daemon._cmd_play({"video_id": "a"})
    assert [t.video_id for t in queue.tracks] == ["a", "b", "a"]
    assert queue.index == 0


def test_play_new_track_still_inserts_after_current(tmp_path, sock_path):
    daemon, player, queue = build_daemon(tmp_path, sock_path)
    queue.enqueue([make_track("a"), make_track("b")])
    queue._index = 0
    daemon._cmd_play({"video_id": "z"})
    assert [t.video_id for t in queue.tracks] == ["a", "z", "b"]
    assert queue.index == 1


def test_play_current_track_restarts_rather_than_advances(tmp_path, sock_path):
    daemon, player, queue = build_daemon(tmp_path, sock_path)
    queue.enqueue([make_track("a"), make_track("b")])
    queue._index = 0
    daemon._cmd_play({"video_id": "a"})
    assert [t.video_id for t in queue.tracks] == ["a", "b"]
    assert queue.index == 0


def test_enqueue_still_appends_duplicates_unchanged(tmp_path, sock_path):
    daemon, player, queue = build_daemon(tmp_path, sock_path)
    daemon._cmd_enqueue({"video_id": "a"})
    daemon._cmd_enqueue({"video_id": "a"})
    assert [t.video_id for t in queue.tracks] == ["a", "a"]


def test_play_existing_track_invalidates_prefetch_via_real_queue(tmp_path, sock_path):
    from ytm.daemon.queue import Queue

    player = FakePlayer()
    resolved = []

    def resolver(video_id):
        resolved.append(video_id)
        return f"url-{video_id}"

    queue = Queue(player, resolver=resolver, yt=FakeYT(), autoplay_radio=False)
    daemon = Daemon(
        path=sock_path,
        player=player,
        queue=queue,
        state_path=tmp_path / "state.json",
        yt=FakeYT(),
    )
    queue.enqueue([make_track("a"), make_track("b"), make_track("c")])
    # prime a prefetch of "b" (the track after current "a")
    queue._prefetch_next()
    assert queue._prefetch is not None and queue._prefetch[0] == "b"
    # jumping straight to "c" must drop that stale prefetch
    daemon._cmd_play({"video_id": "c"})
    assert queue.index == 2
    assert queue._prefetch is None


# -- lyrics command ---------------------------------------------------------


def test_lyrics_returns_normalised_text(tmp_path, sock_path):
    yt = FakeYT(lyrics="line one\nline two")
    daemon, _, _ = build_daemon(tmp_path, sock_path, yt=yt)
    response = daemon._cmd_lyrics({"video_id": "PYgcJpC6WAQ"})
    assert response == {
        "video_id": "PYgcJpC6WAQ",
        "lyrics": "line one\nline two",
        "source": "Source",
    }


def test_lyrics_missing_is_ok_true_with_null(tmp_path, sock_path):
    yt = FakeYT(lyrics=None)
    daemon, _, _ = build_daemon(tmp_path, sock_path, yt=yt)
    response = daemon._cmd_lyrics({"video_id": "no-lyrics-track"})
    assert response == {"video_id": "no-lyrics-track", "lyrics": None, "source": None}


def test_lyrics_cache_prevents_second_fetch(tmp_path, sock_path):
    yt = FakeYT(lyrics="cached text")
    daemon, _, _ = build_daemon(tmp_path, sock_path, yt=yt)
    daemon._cmd_lyrics({"video_id": "v1"})
    daemon._cmd_lyrics({"video_id": "v1"})
    assert yt.lyrics_calls == 1


def test_lyrics_malformed_args_returns_ok_false(tmp_path, sock_path):
    async def scenario():
        daemon, _, _ = build_daemon(tmp_path, sock_path)
        await daemon.start()
        client = await connect(sock_path)
        response = await client.call(1, "lyrics", video_id=123)
        follow_up = await client.call(2, "status")
        await client.close()
        await daemon.stop()
        return response, follow_up

    response, follow_up = asyncio.run(scenario())
    assert response["ok"] is False
    assert follow_up["ok"] is True


# -- pause/resume/toggle and the paused/status desync ----------------------


def _daemon_with_real_queue(tmp_path, sock_path, autoplay_radio=False):
    """A Daemon wired to the real Queue (not FakeQueue) so that playing a
    track actually drives FakePlayer.load(), which is what flips
    FakePlayer.paused back to False -- exactly the mechanism under test.

    The Queue is built on `daemon._player` -- the tap -- and not on the raw
    FakePlayer, because that is what production does: `Daemon.__init__`
    wraps the player first, then constructs the Queue over the wrapper. Wire
    it the other way round and the tap's own `on_position`/`on_eof`/
    `on_error` registrations overwrite the Queue's on the bare player, so
    the Queue silently stops receiving player callbacks entirely.
    """
    from ytm.daemon.queue import Queue

    player = FakePlayer()
    daemon = Daemon(
        path=sock_path, player=player, queue=FakeQueue(player),
        state_path=tmp_path / "state.json", yt=FakeYT(),
    )
    queue = Queue(
        daemon._player, resolver=lambda video_id: f"url-{video_id}",
        yt=FakeYT(), autoplay_radio=autoplay_radio,
    )
    daemon._queue = queue
    queue.on_error(daemon._pushed_error)
    return daemon, player, queue


def test_status_reflects_the_players_real_pause_state_not_a_shadow_copy(
    tmp_path, sock_path
):
    """Regression for the daemon/mpv pause desync: the daemon used to track
    its own `self._paused` flag, set only from the handful of call sites it
    remembered to update. If the player's real pause state ever changes
    through any other path, that shadow copy goes stale. `status` must read
    the player's actual state directly instead."""
    daemon, player, queue = build_daemon(tmp_path, sock_path)

    # Simulate the player's real state changing without going through any
    # daemon command that a shadow-flag implementation might have hooked --
    # exactly the kind of path a future caller could add and forget to
    # mirror into a separate flag.
    player.paused = True
    assert daemon._status_data()["paused"] is True

    player.paused = False
    assert daemon._status_data()["paused"] is False


def test_pause_resume_toggle_still_work(tmp_path, sock_path):
    daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
    daemon._cmd_play({"video_id": "a"})
    assert daemon._status_data()["paused"] is False

    daemon._cmd_pause({})
    assert ("pause",) in player.calls
    assert daemon._status_data()["paused"] is True

    daemon._cmd_resume({})
    assert ("resume",) in player.calls
    assert daemon._status_data()["paused"] is False

    daemon._cmd_pause({})
    daemon._cmd_toggle({})
    assert ("toggle",) in player.calls
    assert daemon._status_data()["paused"] is False


def test_next_and_prev_while_paused_start_playing(tmp_path, sock_path):
    """A deliberate pause must not survive advancing to a different track --
    the user asked for the next/previous track, which means play it."""
    daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
    daemon._cmd_play({"video_id": "a"})
    daemon._cmd_enqueue({"video_id": "b"})
    daemon._cmd_pause({})
    assert daemon._status_data()["paused"] is True

    daemon._cmd_next({})
    assert daemon._status_data()["paused"] is False

    daemon._cmd_pause({})
    assert daemon._status_data()["paused"] is True
    daemon._cmd_prev({})
    assert daemon._status_data()["paused"] is False


# -- events for state the daemon changes indirectly ------------------------


def test_playback_error_skip_pushes_track_changed(tmp_path, sock_path):
    """Regression: a track that fails to load is skipped by the queue, which
    starts playing a different track. The daemon emitted only an `error`
    event, so clients kept displaying the failed track while unrelated audio
    played."""
    async def scenario():
        daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
        queue.enqueue([make_track("a"), make_track("b")])
        await daemon.start()
        client = await connect(sock_path)
        player.emit_error("stream unavailable")
        event = await client.next_event("track_changed")
        await client.close()
        await daemon.stop()
        return event, queue.current.video_id

    event, current = asyncio.run(scenario())
    assert current == "b"
    assert event["data"]["video_id"] == "b"


def test_playback_error_still_pushes_the_error_event(tmp_path, sock_path):
    """The error event must survive the skip notification, and must still
    describe the failure rather than the track that replaced it."""
    async def scenario():
        daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
        queue.enqueue([make_track("a"), make_track("b")])
        await daemon.start()
        client = await connect(sock_path)
        player.emit_error("stream unavailable")
        event = await client.next_event("error")
        await client.close()
        await daemon.stop()
        return event

    event = asyncio.run(scenario())
    assert event["data"]["kind"] == "playback"
    assert "stream unavailable" in event["data"]["error"]


def test_next_and_prev_push_state_changed_after_clearing_a_pause(
    tmp_path, sock_path
):
    """Regression: `next`/`prev` clear the pause state as a side effect of
    loading a track, but emitted no `state_changed`, so a client that paused
    and then skipped kept rendering a paused indicator over playing audio."""
    async def scenario():
        daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
        await daemon.start()
        client = await connect(sock_path)
        await client.call(1, "play", video_id="a", title="A")
        await client.call(2, "enqueue", video_id="b", title="B")

        await client.call(3, "pause")
        client.events.clear()
        await client.call(4, "next")
        after_next = await client.next_event("state_changed")

        await client.call(5, "pause")
        client.events.clear()
        await client.call(6, "prev")
        after_prev = await client.next_event("state_changed")

        await client.close()
        await daemon.stop()
        return after_next, after_prev

    after_next, after_prev = asyncio.run(scenario())
    assert after_next["data"]["paused"] is False
    assert after_prev["data"]["paused"] is False


def _record_emits(daemon):
    """Capture every event the daemon pushes, in order.

    The socket Client pops the first matching event and discards the rest,
    so it cannot assert ordering, absence, or "exactly once" -- which is
    what the no-skip paths below need.
    """
    seen = []
    daemon._emit = lambda event, data=None: seen.append((event, data or {}))
    return seen


def test_daemon_registers_its_error_reporter_on_the_queue(tmp_path, sock_path):
    """`Daemon.__init__` must wire the queue's error callback; without it a
    playback failure is never reported to clients at all. The real-queue
    fixture re-registers this by hand, so it needs its own guard."""
    daemon, player, queue = build_daemon(tmp_path, sock_path)
    seen = _record_emits(daemon)
    queue._on_error("stream unavailable")
    assert seen == [
        ("error", {"error": "stream unavailable", "kind": "playback"})
    ]


def test_tripped_breaker_does_not_announce_a_track_that_never_started(
    tmp_path, sock_path
):
    """Once MAX_CONSECUTIVE_FAILURES is reached the queue stops advancing and
    nothing is loaded. Announcing `track_changed` there would claim a track
    is playing while silence plays -- and `_emit_track_changed` records
    `_last_played`, which `save()` persists to disk."""
    from ytm.daemon.queue import MAX_CONSECUTIVE_FAILURES

    daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
    daemon._cmd_play({"video_id": "a"})
    for video_id in "bcdef":
        daemon._cmd_enqueue({"video_id": video_id})

    seen = _record_emits(daemon)
    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        player.emit_error("boom")
    assert [data["video_id"] for name, data in seen if name == "track_changed"] == [
        "b",
        "c",
    ]

    seen.clear()
    player.emit_error("boom")
    assert queue.current.video_id == "c"
    assert [name for name, _ in seen] == ["error"]
    assert daemon._last_played == "c"


def test_error_at_the_tail_announces_only_the_error(tmp_path, sock_path):
    """With autoplay off, an error on the last track leaves the cursor put
    and loads nothing, so there is no new track to announce."""
    daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
    daemon._cmd_play({"video_id": "a"})

    seen = _record_emits(daemon)
    player.emit_error("boom")
    assert queue.current.video_id == "a"
    assert [name for name, _ in seen] == ["error"]


def test_removing_the_playing_track_announces_the_one_that_replaces_it(
    tmp_path, sock_path
):
    """Removing the current track makes the next one current and starts it,
    which also clears a pause -- the same silent state change `next`/`prev`
    had."""
    daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
    daemon._cmd_play({"video_id": "a"})
    daemon._cmd_enqueue({"video_id": "b"})
    daemon._cmd_pause({})
    assert daemon._status_data()["paused"] is True

    seen = _record_emits(daemon)
    daemon._cmd_queue_remove({"index": 0})
    assert queue.current.video_id == "b"
    assert daemon._status_data()["paused"] is False
    names = [name for name, _ in seen]
    assert "track_changed" in names and "state_changed" in names
    assert names.count("queue_changed") == 1


def test_removing_another_track_does_not_announce_a_track_change(
    tmp_path, sock_path
):
    """Removing a track that isn't playing moves nothing, so the only thing
    that changed is the queue itself."""
    daemon, player, queue = _daemon_with_real_queue(tmp_path, sock_path)
    daemon._cmd_play({"video_id": "a"})
    daemon._cmd_enqueue({"video_id": "b"})

    seen = _record_emits(daemon)
    daemon._cmd_queue_remove({"index": 1})
    assert queue.current.video_id == "a"
    assert [name for name, _ in seen] == ["queue_changed"]
