"""Tests for ytm.player, the thin control layer over a persistent mpv.

A FakeMpv listens on a Unix socket and answers the handful of commands the
Player sends, keeping just enough state (pause, volume, playlist) to check
that the wrapper asks mpv the right questions and shapes the answers the way
the CLI expects. No real mpv, no network.
"""

import json
import os
import socket
import threading

import pytest

from ytm import player as player_mod
from ytm.player import Player, PlayerError, default_ipc_path, mpv_args, video_id_of


class FakeMpv:
    """Speaks mpv's JSON IPC over a Unix socket, one client at a time."""

    def __init__(self, path):
        self.path = path
        self.commands = []
        self.props = {
            "pause": False,
            "volume": 70.0,
            "playlist-pos": -1,
            "playlist-count": 0,
            "idle-active": True,
        }
        self.playlist = []
        self.fail_next = None
        self.pending_events = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb", buffering=0) as f:
                for line in f:
                    request = json.loads(line)
                    reply = self._handle(request["command"])
                    reply["request_id"] = request["request_id"]
                    # an unsolicited event first, to prove replies are matched
                    f.write(b'{"event":"property-change","name":"x"}\n')
                    for event in self.pending_events:
                        f.write((json.dumps(event) + "\n").encode())
                    self.pending_events.clear()
                    f.write((json.dumps(reply) + "\n").encode())
                    if request["command"][0] == "quit":
                        return

    def _handle(self, command):
        self.commands.append(command)
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            return {"error": error}
        name = command[0]
        if name == "get_property":
            prop = command[1]
            if prop == "playlist":
                return {"error": "success", "data": self.playlist}
            if prop not in self.props:
                return {"error": "property unavailable"}
            return {"error": "success", "data": self.props[prop]}
        if name == "observe_property":
            prop = command[2]
            self.pending_events.append(
                {"event": "property-change", "id": command[1], "name": prop, "data": self.props.get(prop)}
            )
        elif name == "set_property":
            self.props[command[1]] = command[2]
        elif name == "cycle" and command[1] == "pause":
            self.props["pause"] = not self.props["pause"]
        elif name == "loadfile":
            url, flags = command[1], command[2]
            title = command[4].removeprefix("force-media-title=") or None
            if title and title.startswith("%"):
                title = title.split("%", 2)[2]  # %n%literal
            entry = {"filename": url, "title": title}
            if flags == "insert-next" and self.props["playlist-pos"] >= 0:
                self.playlist.insert(self.props["playlist-pos"] + 1, entry)
            else:
                self.playlist.append(entry)
            self.props["playlist-count"] = len(self.playlist)
        elif name == "playlist-play-index":
            self.props["playlist-pos"] = command[1]
            self.props["idle-active"] = False
            entry = self.playlist[command[1]]
            self.props["media-title"] = entry["title"] or entry["filename"]
            self.props["playback-time"] = 0.0
            self.props["duration"] = 200.0
            for i, e in enumerate(self.playlist):
                e["current"] = i == command[1]
        return {"error": "success", "data": None}

    def close(self):
        self._server.close()
        if os.path.exists(self.path):
            os.unlink(self.path)


@pytest.fixture
def mpv(tmp_path):
    fake = FakeMpv(str(tmp_path / "mpv.sock"))
    yield fake
    fake.close()


def no_spawn(args):
    raise AssertionError(f"should not have spawned mpv: {args}")


# -- connection -------------------------------------------------------------


def test_connects_to_a_running_mpv_without_spawning(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        assert p.get("volume") == 70.0


def test_spawns_mpv_when_nothing_listens_and_waits_for_the_socket(tmp_path):
    path = str(tmp_path / "mpv.sock")
    spawned = []

    def spawner(args):
        spawned.append(args)
        # mpv takes a moment to open its socket; simulate that
        threading.Timer(0.2, lambda: spawned.append(FakeMpv(path))).start()

    with Player(ipc_path=path, spawner=spawner, ytdlp_path="/v/bin/yt-dlp") as p:
        assert p.get("volume") == 70.0
    assert spawned[0][0] == "mpv"
    assert f"--input-ipc-server={path}" in spawned[0]
    spawned[1].close()


def test_refuses_to_spawn_when_asked_not_to(tmp_path):
    with pytest.raises(PlayerError, match="not running"):
        Player(ipc_path=str(tmp_path / "none.sock"), spawn=False)


def test_spawn_that_never_opens_the_socket_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(player_mod, "SPAWN_TIMEOUT", 0.2)
    with pytest.raises(PlayerError, match="never opened"):
        Player(ipc_path=str(tmp_path / "none.sock"), spawner=lambda args: None)


def test_replies_are_matched_by_request_id_across_events(mpv):
    # FakeMpv writes an event line before every reply
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        assert p.get("volume") == 70.0
        assert p.get("pause") is False


def test_mpv_error_becomes_player_error(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        mpv.fail_next = "invalid parameter"
        with pytest.raises(PlayerError, match="rejected seek: invalid parameter"):
            p.seek(10)


def test_unavailable_property_yields_the_default(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        assert p.get("media-title", "nothing") == "nothing"


# -- mpv command line -------------------------------------------------------


def test_mpv_args_route_everything_ytdlp_needs_through_raw_options():
    args = mpv_args(
        "/run/ytm/mpv.sock",
        ytdlp_path="/venv/bin/yt-dlp",
        cookies_file="/home/u/.config/ytm/cookies.txt",
        extractor_args="youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
        js_runtimes="node",
        audio_device="pipewire",
    )
    assert "--idle=yes" in args and "--no-video" in args
    assert "--script-opts=ytdl_hook-ytdl_path=/venv/bin/yt-dlp" in args
    raw = next(a for a in args if a.startswith("--ytdl-raw-options="))
    assert raw == (
        "--ytdl-raw-options=cookies=/home/u/.config/ytm/cookies.txt,"
        "extractor-args=youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416,"
        "js-runtimes=node"
    )
    assert "--audio-device=pipewire" in args


def test_mpv_args_omit_what_is_not_configured():
    args = mpv_args("/tmp/s", audio_device="auto")
    assert not any(a.startswith("--ytdl-raw-options") for a in args)
    assert not any(a.startswith("--audio-device") for a in args)


def test_default_ipc_path_is_a_socket_on_posix_and_a_pipe_on_windows(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert default_ipc_path("linux") == "/run/user/1000/ytm/mpv.sock"
    assert default_ipc_path("win32") == r"\\.\pipe\ytm-mpv"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert default_ipc_path("linux").endswith("/mpv.sock")


# -- loading and transport --------------------------------------------------


def test_play_appends_and_starts_with_a_forced_title(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("https://music.youtube.com/watch?v=abc", title="505 / Arctic Monkeys")
    assert mpv.commands[-3:] == [
        [
            "loadfile",
            "https://music.youtube.com/watch?v=abc",
            "insert-next",
            -1,
            "force-media-title=%20%505 / Arctic Monkeys",
        ],
        ["playlist-play-index", 0],
        ["set_property", "pause", False],
    ]


def test_play_while_playing_inserts_next_and_jumps_to_it(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("u1", title="one")
        p.enqueue("u3", title="three")
        p.play("u2", title="two")
        assert [e["title"] for e in p.playlist()] == ["one", "two", "three"]
        assert [e["current"] for e in p.playlist()] == [False, True, False]
    assert ["playlist-play-index", 1] in mpv.commands


def test_enqueue_appends_without_interrupting(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("u1", title="one")
        p.enqueue("u2", title="two")
        assert mpv.props["playlist-pos"] == 0
        assert [e["title"] for e in p.playlist()] == ["one", "two"]
        assert [e["current"] for e in p.playlist()] == [True, False]


def test_transport_maps_to_mpv_commands(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.pause()
        p.resume()
        p.toggle()
        p.next()
        p.prev()
        p.seek(-5)
        p.seek(30, absolute=True)
        p.stop()
    assert mpv.commands == [
        ["set_property", "pause", True],
        ["set_property", "pause", False],
        ["cycle", "pause"],
        ["playlist-next", "force"],
        ["playlist-prev", "force"],
        ["seek", -5, "relative"],
        ["seek", 30, "absolute"],
        ["stop"],
    ]


def test_volume_clamps_and_reads_back(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        assert p.volume(140) == 100
        assert p.volume(-3) == 0
        assert p.volume() == 0


def test_playlist_edits(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.remove(2)
        p.move(3, 1)
        p.clear()
        p.shuffle()
    assert mpv.commands == [
        ["playlist-remove", 2],
        ["playlist-move", 3, 1],
        ["playlist-clear"],
        ["playlist-shuffle"],
    ]


def test_play_index_jumps_and_unpauses(mpv):
    mpv.playlist.extend({"filename": f"u{i}", "title": None} for i in range(5))
    mpv.props["pause"] = True
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play_index(4)
    assert mpv.commands == [["playlist-play-index", 4], ["set_property", "pause", False]]
    assert mpv.props["pause"] is False


# -- status -----------------------------------------------------------------


def test_status_when_idle(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        assert p.status() == {
            "idle": True,
            "title": None,
            "position": 0.0,
            "duration": 0.0,
            "paused": False,
            "volume": 70.0,
            "index": -1,
            "count": 0,
        }


def test_status_while_playing_reads_mpv_as_the_source_of_truth(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("https://music.youtube.com/watch?v=abc", title="505")
        mpv.props["playback-time"] = 134.2
        mpv.props["pause"] = True  # as if mpv reported it, not us
        s = p.status()
    assert s["idle"] is False
    assert s["title"] == "505"
    assert s["position"] == pytest.approx(134.2)
    assert s["duration"] == 200.0
    assert s["paused"] is True
    assert (s["index"], s["count"]) == (0, 1)


def test_playlist_entries_carry_their_video_id(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("https://music.youtube.com/watch?v=abc123&list=x", title="t")
        assert p.playlist()[0]["video_id"] == "abc123"


def test_quit_tolerates_mpv_closing_first(mpv):
    p = Player(ipc_path=mpv.path, spawner=no_spawn)
    p.quit()
    assert mpv.commands[-1] == ["quit"]
    with pytest.raises(PlayerError, match="closed"):
        p.get("volume")


def test_video_id_of():
    assert video_id_of("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id_of("https://youtu.be/x") is None
    assert video_id_of(None) is None


def test_observe_delivers_every_initial_value(mpv):
    """mpv reports each observed property once immediately. Registering
    several must not lose the early ones while later replies are awaited."""
    with Player(ipc_path=mpv.path, spawner=no_spawn, timeout=2.0) as p:
        seen = {}
        for name, value in p.observe("pause", "volume", "playlist-pos", "playlist-count"):
            if name != "x":
                seen[name] = value
            if len(seen) == 4:
                break
    assert seen == {"pause": False, "volume": 70.0, "playlist-pos": -1, "playlist-count": 0}


def test_option_list_quotes_values_so_commas_and_unicode_survive():
    from ytm.player import option_list

    assert option_list(**{"force-media-title": "Hello, World"}) == "force-media-title=%12%Hello, World"
    assert option_list(**{"force-media-title": "Zoë"}) == "force-media-title=%4%Zoë"  # UTF-8 bytes


def test_play_with_a_comma_in_the_title_keeps_the_title(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("https://music.youtube.com/watch?v=abc", title="Hello, World / X")
    assert mpv.playlist[-1]["title"] == "Hello, World / X"
