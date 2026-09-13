"""Tests for the new CLI over the mpv-backed core.

The Player and the catalogue are both faked at the module boundary the CLI
uses, so these tests assert on what the user sees (text and JSON) and on the
mpv commands that would have been sent, not on any transport.
"""

import io
import json

import pytest

from ytm import cli, state
from ytm.music import Track


def track(video_id, title="T", artist="A", album="B", seconds=200):
    return Track(video_id, title, artist, album, f"{seconds // 60}:{seconds % 60:02d}", seconds)


class FakePlayer:
    """Records calls; reports a tiny playlist as mpv would."""

    mixer = None  # mpv's own volume, no system mixer (see Player.mixer)

    def __init__(self):
        self.calls = []
        self.entries = []
        self.paused = False
        self.vol = 70.0
        self.pos = 134.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _current(self):
        return next((e for e in self.entries if e["current"]), None)

    def _add(self, url, title, current):
        video_id = url.rsplit("v=", 1)[-1]
        if current:
            for e in self.entries:
                e["current"] = False
        self.entries.append({"url": url, "video_id": video_id, "title": title, "current": current})

    def play(self, url, title=None):
        self.calls.append(("play", url, title))
        self._add(url, title, True)

    def enqueue(self, url, title=None):
        self.calls.append(("enqueue", url, title))
        self._add(url, title, not self.entries)

    def enqueue_many(self, items):
        added = 0
        for url, title in items:
            if url.rsplit("v=", 1)[-1] in self.queued_ids():
                continue
            self.enqueue(url, title)
            added += 1
        return added

    def enqueue_next(self, url, title=None):
        self.calls.append(("enqueue_next", url, title))
        cur = self._current()
        self._add(url, title, cur is None)
        if cur is not None:
            entry = self.entries.pop()
            self.entries.insert(self.entries.index(cur) + 1, entry)

    def playlist(self):
        return list(self.entries)

    def queued_ids(self):
        return {e["video_id"] for e in self.entries}

    def status(self):
        cur = self._current()
        return {
            "idle": cur is None,
            "title": cur["title"] if cur else None,
            "position": self.pos if cur else 0.0,
            "duration": 253.0 if cur else 0.0,
            "paused": self.paused,
            "volume": self.vol,
            "index": self.entries.index(cur) if cur else -1,
            "count": len(self.entries),
        }

    def volume(self, level=None):
        if level is not None:
            self.vol = max(0, min(100, level))
        return self.vol

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name,) + args)
            if name == "pause":
                self.paused = True
            if name == "resume":
                self.paused = False
            if name == "toggle":
                self.paused = not self.paused
            if name == "stop":
                self.entries.clear()

        return call


@pytest.fixture
def fake(monkeypatch, tmp_path):
    p = FakePlayer()
    monkeypatch.setattr(cli, "player", lambda spawn=True: p)
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "session.json")
    return p


@pytest.fixture
def catalogue(monkeypatch):
    from ytm import music

    calls = []
    results = {
        "arctic monkeys": [track("id505", "505", "Arctic Monkeys", "Favourite Worst Nightmare", 253),
                           track("idIWB", "I Wanna Be Yours", "Arctic Monkeys", "AM", 184)],
    }
    monkeypatch.setattr(music, "search", lambda q, limit=20, yt=None: (calls.append(("search", q)), results.get(q, []))[1])
    monkeypatch.setattr(music, "song", lambda vid, yt=None: (calls.append(("song", vid)), track(vid, "By Id", "Someone", "", 100))[1])
    monkeypatch.setattr(music, "radio", lambda vid, limit=25, yt=None: (calls.append(("radio", vid)), [track("r1", "R1", "X"), track("r2", "R2", "Y")])[1])
    monkeypatch.setattr(music, "like", lambda vid, yt=None: calls.append(("like", vid)))
    monkeypatch.setattr(music, "get_lyrics", lambda vid, yt=None: (calls.append(("lyrics", vid)), ("la la", "Musixmatch"))[1])
    from ytm.music import Playlist
    monkeypatch.setattr(music, "mixes", lambda yt=None: (calls.append(("mixes",)), [
        Playlist("RDTMAKsuper", "My Supermix", None),
        Playlist("RDTMAKdisc", "Discover Mix", None),
    ])[1])
    monkeypatch.setattr(music, "get_playlist", lambda pid, limit=100, yt=None: (
        calls.append(("get_playlist", pid)),
        (Playlist(pid, "Discover Mix", 2), [track("m1", "M1", "X"), track("m2", "M2", "Y")]),
    )[1])
    return calls


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# -- the bare command ---------------------------------------------------------


def test_bare_ytm_opens_the_tui_and_exits_cleanly(monkeypatch):
    """`cmd_tui` answers (data, text) like every other command, and a
    two-None tuple is truthy: `main` used to hand that tuple to sys.exit, so
    every TUI session printed "(None, None)" and left exit code 1 behind."""
    opened = []
    monkeypatch.setattr(cli, "cmd_tui", lambda args: (opened.append(1), (None, None))[1])
    code, out, err = run()
    assert opened == [1]
    assert code == 0
    assert (out, err) == ("", "")


def test_a_network_failure_is_reported_not_traced(fake, monkeypatch):
    import requests

    from ytm import music

    def unreachable(query, limit=20, yt=None):
        raise requests.exceptions.ConnectionError("name resolution failed")

    monkeypatch.setattr(music, "search", unreachable)
    code, out, err = run("search", "anything")
    assert code == 1
    assert "could not reach YouTube Music" in err
    assert "Traceback" not in err

# -- installing mpv -----------------------------------------------------------


def test_the_mpv_log_directory_is_made_before_mpv_is_told_to_write_there(tmp_path, monkeypatch):
    """mpv does not create it and does not complain when it cannot write, so
    the log the README points at never appeared -- and it is the only place a
    failed resolve or a dead audio device is ever reported."""
    log = tmp_path / "state" / "ytm" / "mpv.log"
    monkeypatch.setattr(cli, "LOG_PATH", str(log))
    assert not log.parent.exists()
    assert cli._log_path() == str(log)
    assert log.parent.is_dir()


def test_an_unwritable_log_directory_does_not_stop_playback(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_PATH", "/proc/nope/ytm/mpv.log")
    assert cli._log_path() == "/proc/nope/ytm/mpv.log"  # no raise




def test_install_mpv_runs_the_command_for_this_machine(monkeypatch):
    ran = []
    # mpv is on PATH only once the package manager has actually run
    monkeypatch.setattr(cli, "find_mpv", lambda: "/opt/homebrew/bin/mpv" if ran else None)
    monkeypatch.setattr(cli, "mpv_install_command", lambda: ["brew", "install", "mpv"])
    monkeypatch.setattr(cli.subprocess, "call", lambda command: (ran.append(command), 0)[1])
    code, out, _ = run("install-mpv", "--yes")
    assert ran == [["brew", "install", "mpv"]]
    assert code == 0
    assert "mpv installed at /opt/homebrew/bin/mpv" in out


def test_install_mpv_does_nothing_when_mpv_is_already_there(monkeypatch):
    monkeypatch.setattr(cli, "find_mpv", lambda: "/usr/bin/mpv")
    monkeypatch.setattr(cli.subprocess, "call", lambda command: pytest.fail("must not install"))
    code, out, _ = run("install-mpv", "--yes")
    assert code == 0 and "already installed" in out


def test_install_mpv_reports_a_failing_package_manager(monkeypatch):
    monkeypatch.setattr(cli, "find_mpv", lambda: None)
    monkeypatch.setattr(cli, "mpv_install_command", lambda: ["sudo", "apt-get", "install", "-y", "mpv"])
    monkeypatch.setattr(cli.subprocess, "call", lambda command: 100)
    code, _, err = run("install-mpv", "--yes")
    assert code == 1
    assert "sudo apt-get install -y mpv failed (exit 100)" in err


def test_install_mpv_without_a_package_manager_says_where_to_get_it(monkeypatch):
    monkeypatch.setattr(cli, "find_mpv", lambda: None)
    monkeypatch.setattr(cli, "mpv_install_command", lambda: None)
    code, _, err = run("install-mpv", "--yes")
    assert code == 1
    assert "https://mpv.io" in err


def test_a_package_manager_that_lies_about_success_is_caught(monkeypatch):
    """`apt install` exiting 0 with nothing on PATH is worse than a failure:
    the next run would report the same missing mpv with no explanation."""
    monkeypatch.setattr(cli, "find_mpv", lambda: None)
    monkeypatch.setattr(cli, "mpv_install_command", lambda: ["brew", "install", "mpv"])
    monkeypatch.setattr(cli.subprocess, "call", lambda command: 0)
    code, _, err = run("install-mpv", "--yes")
    assert code == 1 and "still not on PATH" in err


# -- search and play ----------------------------------------------------------


def test_search_prints_numbered_results_and_remembers_them(fake, catalogue):
    code, out, _ = run("search", "arctic monkeys")
    assert code == 0
    assert out.splitlines() == [
        "1. 505 — Arctic Monkeys  (4:13)",
        "2. I Wanna Be Yours — Arctic Monkeys  (3:04)",
    ]
    assert [t.video_id for t in state.last_search()] == ["id505", "idIWB"]


def test_search_json_bypasses_formatting(fake, catalogue):
    code, out, _ = run("--json", "search", "arctic monkeys")
    data = json.loads(out)
    assert data["tracks"][0]["title"] == "505"
    assert data["tracks"][0]["duration_seconds"] == 253


def test_play_by_query_plays_the_first_hit(fake, catalogue):
    code, out, _ = run("play", "arctic", "monkeys")
    assert code == 0
    assert fake.calls == [("play", "https://music.youtube.com/watch?v=id505", "505 / Arctic Monkeys")]
    assert out.splitlines() == ["Playing:", "505", "Arctic Monkeys", "Favourite Worst Nightmare"]


def test_play_by_result_number_uses_the_last_search(fake, catalogue):
    run("search", "arctic monkeys")
    code, out, _ = run("play", "2")
    assert code == 0
    assert fake.calls[-1][1].endswith("v=idIWB")
    assert ("search", "2") not in catalogue  # no second network search


def test_play_by_number_without_a_search_is_a_clear_error(fake, catalogue):
    code, out, err = run("play", "3")
    assert code == 1
    assert "no previous search" in err


def test_play_by_number_out_of_range(fake, catalogue):
    run("search", "arctic monkeys")
    code, _, err = run("play", "7")
    assert code == 1 and "had 2 results" in err


def test_play_by_video_id_fetches_its_metadata(fake, catalogue):
    code, out, _ = run("play", "dQw4w9WgXcQ")
    assert code == 0
    assert ("song", "dQw4w9WgXcQ") in catalogue
    assert fake.calls[0][2] == "By Id / Someone"


def test_play_with_nothing_found(fake, catalogue):
    code, _, err = run("play", "zzz")
    assert code == 1 and "nothing found" in err


def test_play_without_arguments_resumes(fake, catalogue):
    fake.paused = True
    code, out, _ = run("play")
    assert code == 0 and out.strip() == "resumed"
    assert ("resume",) in fake.calls


def test_add_enqueues_without_interrupting(fake, catalogue):
    run("play", "arctic monkeys")
    code, out, _ = run("add", "1")
    assert code == 0
    assert fake.calls[-1][0] == "enqueue"
    assert out.startswith("Queued:")


# -- transport is local only ----------------------------------------------------


def test_local_commands_never_import_the_catalogue(fake, monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "ytm.music", raising=False)
    monkeypatch.delitem(sys.modules, "ytmusicapi", raising=False)
    for argv in (["pause"], ["resume"], ["toggle"], ["next"], ["prev"], ["volume", "40"], ["seek", "-5"], ["clear"], ["shuffle"]):
        assert run(*argv)[0] == 0
    assert "ytmusicapi" not in sys.modules


def test_transport_commands_map_to_player_calls(fake):
    run("pause"); run("resume"); run("toggle"); run("next"); run("prev"); run("stop")
    run("seek", "-5"); run("seek", "90", "--to"); run("clear"); run("shuffle")
    assert fake.calls == [
        ("pause",), ("resume",), ("toggle",), ("next",), ("prev",), ("stop",),
        ("seek", -5.0), ("seek", 90.0), ("clear",), ("shuffle",),
    ]


def test_seek_passes_absolute_flag(fake):
    # FakePlayer.__getattr__ drops kwargs; check via a dedicated recorder
    seen = {}
    fake.seek = lambda s, absolute=False: seen.update(s=s, absolute=absolute)
    run("seek", "90", "--to")
    assert seen == {"s": 90.0, "absolute": True}


def test_toggle_reports_the_resulting_state(fake):
    assert run("toggle")[1].strip() == "paused"
    assert run("toggle")[1].strip() == "resumed"


def test_volume_shows_and_sets(fake):
    assert run("volume")[1].strip() == "volume 70"
    assert run("volume", "35")[1].strip() == "volume 35"
    assert json.loads(run("--json", "volume")[1]) == {"volume": 35}


def test_player_not_running_is_a_one_line_error(monkeypatch, tmp_path):
    from ytm.player import PlayerError

    def no_player(spawn=True):
        raise PlayerError("mpv is not running (no IPC endpoint at /x)")

    monkeypatch.setattr(cli, "player", no_player)
    code, out, err = run("pause")
    assert code == 1 and out == "" and "mpv is not running" in err


# -- status and queue ---------------------------------------------------------


def test_status_when_idle(fake):
    code, out, _ = run("status")
    assert out.strip() == "Nothing playing (volume 70)"


def test_status_uses_remembered_metadata_for_the_current_track(fake, catalogue):
    run("play", "arctic monkeys")
    code, out, _ = run("status")
    assert out.splitlines() == [
        "505",
        "Arctic Monkeys",
        "Favourite Worst Nightmare",
        "02:14 / 04:13",
        "Playing  track 1 of 1  volume 70",
    ]


def test_status_json_includes_the_track(fake, catalogue):
    run("play", "arctic monkeys")
    data = json.loads(run("--json", "status")[1])
    assert data["track"]["artist"] == "Arctic Monkeys"
    assert data["paused"] is False and data["index"] == 0


def test_status_falls_back_to_mpv_title_for_unknown_entries(fake):
    fake.entries.append({"url": "u", "video_id": None, "title": "Something / Someone", "current": True})
    out = run("status")[1]
    assert out.splitlines()[0] == "Something / Someone"


def test_queue_marks_the_current_entry(fake, catalogue):
    run("play", "arctic monkeys")
    run("add", "2")
    out = run("queue")[1]
    assert out.splitlines() == [
        "▶ 1. 505 / Arctic Monkeys",
        "  2. I Wanna Be Yours / Arctic Monkeys",
    ]


def test_queue_empty(fake):
    assert run("queue")[1].strip() == "Queue is empty"


# -- radio, lyrics, like ---------------------------------------------------------


def test_radio_from_the_current_track_appends(fake, catalogue):
    run("play", "arctic monkeys")
    code, out, _ = run("radio")
    assert code == 0
    assert ("radio", "id505") in catalogue
    assert [c[0] for c in fake.calls[1:]] == ["enqueue", "enqueue"]
    assert out.strip() == "Radio from 505 — Arctic Monkeys: 2 tracks queued"


def test_radio_from_a_query_replaces_the_queue(fake, catalogue):
    run("play", "arctic monkeys")
    run("radio", "arctic", "monkeys")
    names = [c[0] for c in fake.calls]
    assert names == ["play", "stop", "play", "enqueue", "enqueue"]


def test_radio_with_nothing_playing_and_no_seed(fake, catalogue):
    code, _, err = run("radio")
    assert code == 1 and "nothing is playing" in err


# -- mix ----------------------------------------------------------------------


def test_mix_lists_names(fake, catalogue):
    code, out, _ = run("mix")
    assert code == 0
    assert out.strip().splitlines() == ["My Supermix", "Discover Mix"]


def test_mix_play_matches_case_insensitively_by_substring(fake, catalogue):
    code, out, _ = run("mix", "discover")
    assert code == 0
    assert ("get_playlist", "RDTMAKdisc") in catalogue
    assert [c[0] for c in fake.calls] == ["stop", "play", "enqueue"]
    assert out.strip() == "Playing Discover Mix: 2 tracks queued"


def test_mix_play_no_match(fake, catalogue):
    code, _, err = run("mix", "nonexistent")
    assert code == 1 and "no mix matching" in err


def test_lyrics_for_the_current_track(fake, catalogue):
    run("play", "arctic monkeys")
    code, out, _ = run("lyrics")
    assert code == 0 and out.splitlines() == ["la la", "", "— Musixmatch"]
    assert ("lyrics", "id505") in catalogue


def test_like_the_current_track(fake, catalogue):
    run("play", "arctic monkeys")
    code, out, _ = run("like")
    assert code == 0 and out.strip() == "liked 505 — Arctic Monkeys"
    assert ("like", "id505") in catalogue


def test_like_with_nothing_playing(fake, catalogue):
    assert run("like")[0] == 1


# -- select() edge cases ------------------------------------------------------------


def test_select_treats_eleven_char_tokens_as_ids_only_when_they_look_like_one(fake, catalogue):
    assert cli.select("dQw4w9WgXcQ").title == "By Id"
    assert cli.select("arctic monkeys").video_id == "id505"


def test_radio_skips_tracks_already_in_the_queue(fake, catalogue, monkeypatch):
    from ytm import music

    run("play", "arctic monkeys")
    run("add", "2")  # second hit of the last search -> queue: 505, I Wanna Be Yours
    monkeypatch.setattr(music, "radio", lambda vid, limit=25, yt=None: [
        track("idIWB", "I Wanna Be Yours", "Arctic Monkeys"),  # already queued
        track("id505", "505", "Arctic Monkeys"),  # the seed itself
        track("r1", "R1", "X"), track("r2", "R2", "Y"), track("r1", "R1", "X"),  # r1 twice
    ])
    code, out, _ = run("radio", "-n", "5")
    assert code == 0
    added = [c[1].rsplit("v=", 1)[-1] for c in fake.calls if c[0] == "enqueue"][1:]
    assert added == ["r1", "r2"]
    assert "2 tracks queued" in out


def test_add_next_puts_the_song_right_after_the_current_one(fake, catalogue):
    run("search", "arctic monkeys")
    run("play", "1")                      # 505 playing
    run("add", "abcdefghijk")             # by id, goes to the end
    code, out, _ = run("add", "--next", "2")
    assert code == 0 and out.startswith("Up next:")
    assert fake.calls[-1][0] == "enqueue_next"
    assert [e["video_id"] for e in fake.entries] == ["id505", "idIWB", "abcdefghijk"]


def test_install_mpv_accepts_winget_already_installed(monkeypatch):
    """`winget install` of a package that is already there exits 0x8A15002B
    ("No available upgrade found"); that is mpv being present, not a failure."""
    found = []
    monkeypatch.setattr(cli, "find_mpv", lambda: r"C:\Users\u\AppData\Local\Microsoft\WinGet\Links\mpv.exe" if found else None)
    monkeypatch.setattr(cli, "mpv_install_command", lambda: ["winget", "install", "-e", "--id", "shinchiro.mpv"])
    monkeypatch.setattr(cli.subprocess, "call", lambda command: (found.append(1), 2316632107)[1])
    code, out, err = run("install-mpv", "--yes")
    assert code == 0, err
    assert "mpv installed at" in out and "WinGet" in out


def test_install_mpv_finds_an_mpv_the_shell_cannot_see_yet(monkeypatch):
    monkeypatch.setattr(cli, "find_mpv", lambda: r"C:\Users\u\scoop\shims\mpv.exe")
    monkeypatch.setattr(cli.subprocess, "call", lambda command: pytest.fail("must not install"))
    code, out, _ = run("install-mpv", "--yes")
    assert code == 0 and "already installed" in out


def test_auth_defaults_to_oauth_and_from_browser_imports_cookies(monkeypatch, tmp_path):
    from ytm import auth
    calls = []
    monkeypatch.setattr(auth, "oauth_setup", lambda **kw: (calls.append(("oauth", kw)), tmp_path / "auth.json")[1])
    monkeypatch.setattr(auth, "from_browser", lambda browser, **kw: (calls.append(("browser", browser, kw)), tmp_path / "auth.json")[1])
    monkeypatch.setattr(auth, "cookies_file", lambda: None)
    assert run("auth")[0] == 0
    assert calls[-1][0] == "oauth" and calls[-1][1]["client_file"] is None
    assert run("auth", "--client-file", "c.json")[0] == 0
    assert calls[-1] == ("oauth", {"client_id": None, "client_secret": None, "client_file": "c.json"})
    assert run("auth", "--from-browser")[0] == 0
    assert calls[-1][:2] == ("browser", None)
    assert run("auth", "--from-browser", "helium", "--profile", "Profile 1")[0] == 0
    assert calls[-1] == ("browser", "helium", {"profile": "Profile 1", "authuser": None})
    for gone in ("--manual", "--oauth"):  # no header-pasting mode, and OAuth needs no flag
        with pytest.raises(SystemExit):
            run("auth", gone)
