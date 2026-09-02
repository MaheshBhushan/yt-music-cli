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

    def playlist(self):
        return list(self.entries)

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
    return calls


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


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
