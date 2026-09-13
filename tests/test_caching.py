"""What ytm stops asking for twice.

Every test here pins one repeat that used to happen: a state file re-read
per queue row, a ytmusicapi client (and the home-page fetch that comes with
it) per catalogue call, a playlist fetched from mpv per track appended, a
track count per refresh of the playlists pane.
"""

import json
import time

import pytest

from ytm import music, state
from ytm.music import Track


def track(video_id, title="T", artist="A"):
    return Track(video_id, title, artist, "", "3:00", 180)


# -- state: one read serves the whole queue -----------------------------------


def _count_reads(monkeypatch):
    """Count how many times the state file is opened for reading."""
    import builtins

    reads = []
    real_open = builtins.open

    def counting_open(file, mode="r", *args, **kwargs):
        if "r" in mode and "w" not in mode:
            reads.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    return reads


def test_tracks_for_reads_the_state_file_once(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    state.remember_tracks([track("a"), track("b"), track("c")], path)
    reads = _count_reads(monkeypatch)
    found = state.tracks_for(["a", "b", "zz", "", None], path)
    assert sorted(found) == ["a", "b"]
    assert found["b"].title == "T"
    assert len(reads) <= 1


def test_an_unchanged_state_file_is_parsed_once(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    state.remember_tracks([track("a")], path)
    reads = _count_reads(monkeypatch)
    for _ in range(5):
        assert state.track_for("a", path).video_id == "a"
    assert len(reads) <= 1


def test_a_state_file_rewritten_elsewhere_is_picked_up(tmp_path):
    """The `ytm radio` mpv spawns for autoplay writes this file too."""
    path = tmp_path / "s.json"
    state.remember_tracks([track("a", "First")], path)
    assert state.track_for("a", path).title == "First"
    path.write_text(json.dumps({
        "last_search": [],
        "tracks": {"a": {"video_id": "a", "title": "Second", "artist": "", "album": "",
                         "duration": "", "duration_seconds": 0, "thumbnail": ""}},
    }))
    assert state.track_for("a", path).title == "Second"


def test_the_temp_file_is_this_process_alone(tmp_path, monkeypatch):
    """A shared temp name let one writer rename another's half-written file
    into place; the CLI, the TUI and autoplay all write this file."""
    import builtins
    import os

    written = []
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open",
        lambda file, mode="r", *a, **k: (written.append((str(file), mode)),
                                         real_open(file, mode, *a, **k))[1],
    )
    path = tmp_path / "s.json"
    state.save({"last_search": [], "tracks": {}}, path)
    temp_names = [name for name, mode in written if "w" in mode]
    assert temp_names and all(str(os.getpid()) in name for name in temp_names)
    assert not list(tmp_path.glob("*.tmp*"))  # renamed into place, nothing left


def test_a_stored_track_with_an_unknown_field_does_not_crash_playback(tmp_path):
    """A session file written by another version must not break `ytm play 3`."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "last_search": [{"video_id": "a", "title": "Song", "rating": "from the future"}],
        "tracks": {},
    }))
    assert [t.title for t in state.last_search(path)] == ["Song"]


# -- the catalogue client is built once ---------------------------------------


class FakeYTMusic:
    """Stands in for ytmusicapi.YTMusic, counting how many were built."""

    built = 0

    def __init__(self, headers):
        FakeYTMusic.built += 1
        self.headers_given = dict(headers)
        # ytmusicapi computes base_headers lazily and caches them here
        self.__dict__["base_headers"] = dict(headers)

    def search(self, query, filter=None, limit=20):
        return []


@pytest.fixture
def browser_auth(monkeypatch, tmp_path):
    """Browser credentials at the patched AUTH_PATH, with a fake ytmusicapi."""
    from ytm import auth

    FakeYTMusic.built = 0
    path = auth.AUTH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookie": "SID=x", "user-agent": "ua"}))
    monkeypatch.setattr(auth.ytmusicapi, "YTMusic", FakeYTMusic)
    return path


def test_one_client_serves_every_call(browser_auth):
    first = music.shared_client()
    assert music.shared_client() is first
    assert music.search("a", yt=None) == [] and music.search("b", yt=None) == []
    assert FakeYTMusic.built == 1


def test_re_authenticating_retires_the_cached_client(browser_auth):
    first = music.shared_client()
    browser_auth.write_text(json.dumps({"cookie": "SID=new", "user-agent": "ua"}))
    second = music.shared_client()
    assert second is not first
    assert second.headers_given["cookie"] == "SID=new"


def test_a_missing_auth_file_still_raises_every_time():
    """A failure is never what gets cached: `ytm auth` in another terminal
    has to be enough to make the next call work."""
    from ytm.auth import AuthMissing

    for _ in range(2):
        with pytest.raises(AuthMissing):
            music.shared_client()


def test_a_stored_visitor_id_spares_the_home_page_fetch(browser_auth, monkeypatch, tmp_path):
    """Without it ytmusicapi downloads music.youtube.com on first use, once
    per client, purely to read a visitor id out of the page."""
    monkeypatch.setattr(music, "VISITOR_PATH", tmp_path / "visitor.json")
    music._write_visitor_id("VISITOR-1")
    built = music.shared_client()
    assert built.headers_given["X-Goog-Visitor-Id"] == "VISITOR-1"


def test_a_stale_visitor_id_is_not_reused(browser_auth, monkeypatch, tmp_path):
    monkeypatch.setattr(music, "VISITOR_PATH", tmp_path / "visitor.json")
    (tmp_path / "visitor.json").write_text(json.dumps({
        "visitor_id": "OLD", "written_at": time.time() - music.VISITOR_TTL - 1,
    }))
    assert music._read_visitor_id() is None
    assert "X-Goog-Visitor-Id" not in music.shared_client().headers_given


def test_the_visitor_id_youtube_hands_out_is_remembered(browser_auth, monkeypatch, tmp_path):
    monkeypatch.setattr(music, "VISITOR_PATH", tmp_path / "visitor.json")
    client = music.shared_client()
    client.__dict__["base_headers"]["X-Goog-Visitor-Id"] = "LEARNED"
    music.search("anything")
    assert music._read_visitor_id() == "LEARNED"


def test_a_broken_visitor_file_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(music, "VISITOR_PATH", tmp_path / "visitor.json")
    (tmp_path / "visitor.json").write_text("{not json")
    assert music._read_visitor_id() is None


# -- mpv: one playlist read per batch, one batch per status -------------------


def test_enqueue_many_reads_the_playlist_once(monkeypatch):
    """Appending one at a time re-read the playlist per track, on a list that
    grew with every one of them."""
    from ytm.player import Player

    player = Player.__new__(Player)
    reads = []
    loaded = []
    player.playlist = lambda: (reads.append(1), [
        {"url": "https://music.youtube.com/watch?v=a", "video_id": "a", "title": "A", "current": True},
    ])[1]
    player._loadfile = lambda url, flags, title: loaded.append((url, flags, title))

    added = player.enqueue_many([
        ("https://music.youtube.com/watch?v=a", "A"),   # already queued
        ("https://music.youtube.com/watch?v=b", "B"),
        ("https://music.youtube.com/watch?v=c", "C"),
        ("https://music.youtube.com/watch?v=b", "B"),   # a repeat within the batch
    ])
    assert added == 2
    assert len(reads) == 1
    assert [url for url, _, _ in loaded] == [
        "https://music.youtube.com/watch?v=b", "https://music.youtube.com/watch?v=c",
    ]


def test_enqueue_many_of_nothing_asks_mpv_nothing():
    from ytm.player import Player

    player = Player.__new__(Player)
    player.playlist = lambda: pytest.fail("must not read the playlist")
    assert player.enqueue_many([]) == 0


# -- the TUI backend ----------------------------------------------------------


@pytest.fixture
def backend(monkeypatch, tmp_path):
    from tests.test_cli_core import FakePlayer
    from ytm.tui.backend import Backend

    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "session.json")
    fake = FakePlayer()
    made = Backend(player_factory=lambda spawn=True, timeout=None: fake)
    made.fake = fake
    return made


def test_a_missing_track_count_is_asked_for_once(backend, monkeypatch, tmp_path):
    from ytm import playlists_local

    monkeypatch.setattr(playlists_local, "DEFAULT_PATH", tmp_path / "pl.json")
    monkeypatch.setattr(music, "library_playlists",
                        lambda limit=25, yt=None: [music.Playlist("LM", "Liked Music", 0)])
    monkeypatch.setattr(music, "mixes", lambda yt=None: [])
    asked = []
    monkeypatch.setattr(music, "playlist_count", lambda pid, yt=None: (asked.append(pid), 9)[1])

    for _ in range(3):
        listed = backend.request("playlist_list")["playlists"]
    assert asked == ["LM"]
    assert listed[0]["track_count"] == 9


def test_an_empty_mix_list_is_not_asked_for_again(backend, monkeypatch, tmp_path):
    """A signed-out home feed has no mixes; that is an answer, not a reason
    to fetch the whole feed again on every refresh of the pane."""
    from ytm import playlists_local

    monkeypatch.setattr(playlists_local, "DEFAULT_PATH", tmp_path / "pl.json")
    monkeypatch.setattr(music, "library_playlists",
                        lambda limit=25, yt=None: [music.Playlist("PL1", "Mine", 4)])
    monkeypatch.setattr(music, "playlist_count", lambda pid, yt=None: 0)
    calls = []
    monkeypatch.setattr(music, "mixes", lambda yt=None: (calls.append(1), [])[1])

    backend.request("playlist_list")
    backend.request("playlist_list")
    assert len(calls) == 1
    backend.request("mixes_refresh")  # `r` in the TUI asks for today's again
    assert len(calls) == 2


def test_lyrics_are_fetched_once_per_track(backend, monkeypatch):
    calls = []
    monkeypatch.setattr(music, "get_lyrics",
                        lambda vid, yt=None: (calls.append(vid), ("words", "src"))[1])
    assert backend.request("lyrics", {"video_id": "v1"})["lyrics"] == "words"
    backend.request("lyrics", {"video_id": "v2"})
    assert backend.request("lyrics", {"video_id": "v1"})["lyrics"] == "words"
    assert calls == ["v1", "v2"]


def test_a_repeated_query_is_not_searched_again(backend, monkeypatch):
    calls = []

    def search(query, limit=20, yt=None):
        calls.append(query)
        return [Track("s1", "Song", "Band", "LP", "3:20", 200)]

    monkeypatch.setattr(music, "search", search)
    first = backend.request("search", {"query": "daft punk"})
    backend.request("search", {"query": "daft"})
    again = backend.request("search", {"query": "daft punk"})
    assert calls == ["daft punk", "daft"]
    assert again == first
    # and the cached hit is still what `ytm play 3` picks from
    assert [t.video_id for t in state.last_search()] == ["s1"]


def test_a_search_is_asked_again_once_it_goes_stale(backend, monkeypatch):
    from ytm.tui import backend as backend_mod

    calls = []
    monkeypatch.setattr(music, "search", lambda q, limit=20, yt=None: (calls.append(q), [])[1])
    backend.request("search", {"query": "x"})
    real = time.monotonic
    monkeypatch.setattr(backend_mod.time, "monotonic", lambda: real() + backend_mod.SEARCH_TTL + 1)
    backend.request("search", {"query": "x"})
    assert calls == ["x", "x"]
