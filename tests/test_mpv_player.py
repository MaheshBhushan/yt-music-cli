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
import time

import pytest

from ytm import player as player_mod
from ytm.player import (
    Player,
    PlayerError,
    default_ipc_path,
    mpv_args,
    mpv_install_command,
    mpv_missing_message,
    spawn_mpv,
    startup_log_path,
    video_id_of,
)


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
                try:
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
                except (ConnectionResetError, OSError):
                    pass

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
        elif name == "playlist-move" and 0 <= command[1] < len(self.playlist):
            src, dst = command[1], command[2]
            entry = self.playlist.pop(src)
            self.playlist.insert(dst - 1 if src < dst else dst, entry)
            cur = next((i for i, e in enumerate(self.playlist) if e.get("current")), -1)
            self.props["playlist-pos"] = cur
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
    with pytest.raises(PlayerError, match="did not open its IPC endpoint"):
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
    assert video_id_of("https://music.youtube.com/watch?list=RD&v=abc&t=3") == "abc"
    assert video_id_of("https://youtu.be/x") is None
    assert video_id_of(None) is None
    # "v=" also occurs inside another parameter's name; only the real one counts
    assert video_id_of("https://example.com/watch?rv=notmine") is None
    assert video_id_of("/tmp/tracks/dQw4w9WgXcQ.m4a") == "dQw4w9WgXcQ"
    assert video_id_of(r"C:\cache\dQw4w9WgXcQ.webm") == "dQw4w9WgXcQ"


def test_get_many_reads_every_property_in_one_round_trip(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        values = p.get_many("volume", "pause", "nonsuch", default="?")
    assert values == {"volume": 70.0, "pause": False, "nonsuch": "?"}
    assert [c for c in mpv.commands if c[0] == "get_property"] == [
        ["get_property", "volume"], ["get_property", "pause"], ["get_property", "nonsuch"],
    ]


def test_status_asks_mpv_once_for_everything_it_needs(mpv):
    """Eight separate round trips is eight chances to wait on a busy mpv,
    and the TUI asks for a status after every transport key."""
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        mpv.commands.clear()
        p.status()
    batches = [c for c in mpv.commands if c[0] != "get_property"]
    assert batches == []  # nothing but property reads
    assert len(mpv.commands) == 8  # one read each, all in flight together


def test_enqueue_many_appends_without_re_reading_the_playlist(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("https://music.youtube.com/watch?v=a", title="A")
        mpv.commands.clear()
        added = p.enqueue_many([
            ("https://music.youtube.com/watch?v=a", "A"),  # already playing
            ("https://music.youtube.com/watch?v=b", "B"),
            ("https://music.youtube.com/watch?v=c", "C"),
        ])
        assert added == 2
        reads = [c for c in mpv.commands if c == ["get_property", "playlist"]]
        assert len(reads) == 1  # not one per track appended
        assert [e["video_id"] for e in p.playlist()] == ["a", "b", "c"]


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


# -- no duplicates in the queue -------------------------------------------------


def test_enqueue_skips_a_track_that_is_already_queued(mpv):
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        assert p.enqueue("https://music.youtube.com/watch?v=abc", title="A") is True
        assert p.enqueue("https://music.youtube.com/watch?v=abc", title="A again") is False
        assert p.enqueue("https://music.youtube.com/watch?v=def") is True
    assert [e["filename"] for e in mpv.playlist] == [
        "https://music.youtube.com/watch?v=abc", "https://music.youtube.com/watch?v=def",
    ]


def test_play_of_a_queued_track_jumps_to_it_instead_of_inserting(mpv):
    mpv.playlist.extend({"filename": f"https://music.youtube.com/watch?v={v}", "title": None} for v in "abc")
    mpv.props["playlist-pos"] = 0
    with Player(ipc_path=mpv.path, spawner=no_spawn) as p:
        p.play("https://music.youtube.com/watch?v=c")
    assert len(mpv.playlist) == 3
    assert ["playlist-play-index", 2] in mpv.commands
    assert not any(c[0] == "loadfile" for c in mpv.commands)


def test_enqueue_next_inserts_after_current_without_interrupting(mpv):
    p = Player(mpv.path, timeout=2)
    for i, name in enumerate("ABC"):
        p.enqueue(f"https://music.youtube.com/watch?v={name * 11}", title=name)
    p.play_index(0)
    assert p.enqueue_next("https://music.youtube.com/watch?v=" + "N" * 11, title="N") is True
    assert [e["title"] for e in p.playlist()] == ["A", "N", "B", "C"]
    assert p.status()["index"] == 0  # still playing A
    # already up next: nothing to do
    assert p.enqueue_next("https://music.youtube.com/watch?v=" + "N" * 11) is False


def test_enqueue_next_moves_an_already_queued_track_up(mpv):
    p = Player(mpv.path, timeout=2)
    for name in "ABCD":
        p.enqueue(f"https://music.youtube.com/watch?v={name * 11}", title=name)
    p.play_index(1)  # playing B
    assert p.enqueue_next("https://music.youtube.com/watch?v=" + "D" * 11) is True  # from behind
    assert [e["title"] for e in p.playlist()] == ["A", "B", "D", "C"]
    assert p.enqueue_next("https://music.youtube.com/watch?v=" + "A" * 11) is True  # from in front
    assert [e["title"] for e in p.playlist()] == ["B", "A", "D", "C"]
    assert p.status()["index"] == 0 and p.playlist()[0]["current"]
    assert p.enqueue_next("https://music.youtube.com/watch?v=" + "B" * 11) is False  # it is playing
    assert not [c for c in mpv.commands if c[0] == "loadfile" and "%" not in str(c[4]) and c[2] == "insert-next"]


# -- mpv is not a Python package ----------------------------------------------


def _which(*present):
    found = set(present)
    return lambda tool: f"/usr/bin/{tool}" if tool in found else None


def test_install_command_picks_the_package_manager_that_is_here():
    assert mpv_install_command(platform="darwin", which=_which("brew")) == ["brew", "install", "mpv"]
    assert mpv_install_command(platform="linux", which=_which("dnf", "sudo")) == [
        "sudo", "dnf", "install", "-y", "mpv",
    ]
    # the first one on PATH wins, in table order
    assert mpv_install_command(platform="linux", which=_which("apt-get", "pacman", "sudo"))[1] == "apt-get"
    assert mpv_install_command(platform="win32", which=_which("scoop")) == ["scoop", "install", "mpv"]


def test_homebrew_is_never_run_under_sudo():
    """brew refuses to run as root and says so at length."""
    command = mpv_install_command(platform="darwin", which=_which("brew", "sudo"), root=False)
    assert command == ["brew", "install", "mpv"]


def test_root_and_sudoless_systems_skip_the_sudo_prefix():
    assert mpv_install_command(platform="linux", which=_which("apk", "sudo"), root=True) == [
        "apk", "add", "mpv",
    ]
    assert mpv_install_command(platform="linux", which=_which("apk"), root=False) == ["apk", "add", "mpv"]


def test_nothing_known_is_not_a_guess():
    assert mpv_install_command(platform="linux", which=_which()) is None
    assert mpv_install_command(platform="haiku", which=_which("brew")) is None


def test_the_missing_mpv_message_says_what_to_run():
    message = mpv_missing_message(platform="darwin", which=_which("brew"))
    assert "ytm install-mpv" in message and "brew install mpv" in message
    # and where there is nothing to run, it does not pretend there is
    bare = mpv_missing_message(platform="linux", which=_which())
    assert "install-mpv" not in bare and "https://mpv.io" in bare


def test_spawning_a_missing_mpv_explains_instead_of_reporting_errno(monkeypatch):
    """The bare OSError ("[Errno 2] No such file or directory: 'mpv'") is the
    first thing a fresh `uv tool install ytm` shows, and it does not say that
    mpv is a separate program, let alone how to get one."""
    monkeypatch.setattr(player_mod.shutil, "which", lambda tool: None)
    with pytest.raises(PlayerError) as excinfo:
        spawn_mpv(["mpv", "--idle=yes"])
    assert "[Errno 2]" not in str(excinfo.value)
    assert "separate program" in str(excinfo.value)


def test_an_mpv_that_is_present_is_still_spawned(monkeypatch):
    spawned = []
    monkeypatch.setattr(player_mod.shutil, "which", lambda tool: "/usr/bin/mpv")
    monkeypatch.setattr(player_mod.subprocess, "Popen", lambda args, **kw: spawned.append(args))
    spawn_mpv(["mpv", "--idle=yes"])
    assert spawned == [["mpv", "--idle=yes"]]


# -- a slow start is not a failed one -----------------------------------------


class _Process:
    """A spawned mpv: alive until `exits_after` polls, then gone."""

    def __init__(self, exits_after=None, returncode=1):
        self.polls = 0
        self._exits_after = exits_after
        self.returncode = returncode

    def poll(self):
        self.polls += 1
        if self._exits_after is None or self.polls < self._exits_after:
            return None
        return self.returncode


def test_an_mpv_that_is_merely_slow_is_waited_for(tmp_path, monkeypatch):
    """mpv's first start after installation took 11 s on macOS -- one second
    past the old 10 s limit -- so ytm gave up on an mpv that was coming up
    fine, and the next run worked."""
    path = str(tmp_path / "mpv.sock")
    started = []

    def spawner(args):
        threading.Timer(0.4, lambda: started.append(FakeMpv(path))).start()
        return _Process()

    with Player(ipc_path=path, spawner=spawner, timeout=2.0) as p:
        assert p.get("volume") == 70.0
    started[0].close()


def test_an_mpv_that_died_is_reported_at_once(tmp_path, monkeypatch):
    """Waiting out the full timeout to say what was known immediately is the
    difference between a one-second error and a minute of nothing."""
    monkeypatch.setattr(player_mod, "SPAWN_TIMEOUT", 30.0)
    process = _Process(exits_after=2, returncode=4)
    start = time.monotonic()
    with pytest.raises(PlayerError, match="exited straight away"):
        Player(ipc_path=str(tmp_path / "none.sock"), spawner=lambda args: process)
    assert time.monotonic() - start < 5, "waited out the timeout instead of watching mpv"


def test_what_mpv_printed_while_failing_is_quoted(tmp_path, monkeypatch):
    """mpv runs with --no-terminal and nothing attached; without keeping its
    output there is nothing to report but 'no socket appeared'."""
    ipc = tmp_path / "mpv.sock"
    (tmp_path / "mpv-start.log").write_text("Error parsing option ytdl-raw-options\n")
    monkeypatch.setattr(player_mod, "SPAWN_TIMEOUT", 30.0)
    with pytest.raises(PlayerError, match="Error parsing option"):
        Player(ipc_path=str(ipc), spawner=lambda args: _Process(exits_after=1, returncode=2))


def test_a_silent_death_says_where_to_look_instead(tmp_path, monkeypatch):
    """--no-terminal silences mpv's own messages, and an option it refuses is
    rejected before the log file is even opened, so the useful thing left to
    say is where the rest is and how to see it."""
    monkeypatch.setattr(player_mod, "SPAWN_TIMEOUT", 30.0)
    spawner = lambda args: _Process(exits_after=1, returncode=1)
    with pytest.raises(PlayerError) as excinfo:
        Player(
            ipc_path=str(tmp_path / "mpv.sock"),
            spawner=spawner,
            extra_args=[f"--log-file={tmp_path / 'mpv.log'}"],
        )
    message = str(excinfo.value)
    assert str(tmp_path / "mpv.log") in message
    assert "in a terminal" in message


def test_a_spawner_that_returns_nothing_still_times_out(tmp_path, monkeypatch):
    """The injected spawners in these tests hand back no process; the wait
    then has only the clock to go on, as it always did."""
    monkeypatch.setattr(player_mod, "SPAWN_TIMEOUT", 0.2)
    with pytest.raises(PlayerError, match="did not open its IPC endpoint"):
        Player(ipc_path=str(tmp_path / "none.sock"), spawner=lambda args: None)


def test_mpv_startup_output_is_kept_beside_the_socket(tmp_path):
    args = ["mpv", f"--input-ipc-server={tmp_path / 'mpv.sock'}"]
    assert startup_log_path(args) == tmp_path / "mpv-start.log"
    # a Windows named pipe is not a path with a directory to write into
    assert startup_log_path(["mpv", r"--input-ipc-server=\\.\pipe\ytm-mpv"]) is None
    assert startup_log_path(["mpv", "--idle=yes"]) is None


def test_spawn_keeps_what_mpv_says(tmp_path, monkeypatch):
    captured = {}

    def popen(args, **kwargs):
        captured.update(kwargs)
        kwargs["stderr"].write(b"mpv complained")
        return "process"

    monkeypatch.setattr(player_mod.shutil, "which", lambda tool: "/usr/bin/mpv")
    monkeypatch.setattr(player_mod.subprocess, "Popen", popen)
    args = ["mpv", f"--input-ipc-server={tmp_path / 'mpv.sock'}"]
    assert spawn_mpv(args) == "process"
    assert (tmp_path / "mpv-start.log").read_bytes() == b"mpv complained"
