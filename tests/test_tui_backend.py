"""Tests for the TUI backend: the old request/event vocabulary over the
mpv Player and the catalogue, with both faked."""

import pytest

from ytm import music, state
from ytm.tui.backend import Backend, BackendError
from tests.test_cli_core import FakePlayer, track


@pytest.fixture
def backend(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "session.json")
    fake = FakePlayer()
    b = Backend(player_factory=lambda spawn=True, timeout=None: fake)
    b.fake = fake
    return b


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.setattr(music, "search", lambda q, limit=20, yt=None: [track("s1", "Song", "Band", "LP", 200)])
    monkeypatch.setattr(music, "radio", lambda vid, limit=25, yt=None: [track("r1", "R1", "X"), track("r2", "R2", "Y")])
    monkeypatch.setattr(music, "get_lyrics", lambda vid, yt=None: ("words", "src"))
    monkeypatch.setattr(music, "library_playlists", lambda limit=25, yt=None: [music.Playlist("PL1", "Liked", 3)])


def test_status_idle_matches_the_daemon_shape(backend):
    s = backend.request("status")
    assert s == {"current": None, "index": -1, "count": 0, "paused": False, "volume": 70.0, "position": 0.0}


def test_play_then_status_reports_the_track(backend):
    backend.request("play", {"video_id": "abc", "title": "T", "artist": "A", "album": "B", "duration": "3:20", "duration_seconds": 200})
    assert backend.fake.calls[0] == ("play", "https://music.youtube.com/watch?v=abc", "T / A")
    s = backend.request("status")
    assert s["current"]["title"] == "T" and s["current"]["album"] == "B"
    assert s["index"] == 0 and s["count"] == 1


def test_play_requires_a_video_id(backend):
    with pytest.raises(BackendError, match="video_id"):
        backend.request("play", {"title": "no id"})


def test_search_remembers_results(backend, catalogue):
    data = backend.request("search", {"query": "x"})
    assert data["tracks"][0]["video_id"] == "s1"
    assert state.last_search()[0].title == "Song"


def test_queue_get_after_enqueue(backend):
    backend.request("play", {"video_id": "a", "title": "A"})
    backend.request("enqueue", {"video_id": "b", "title": "B", "artist": "Bb"})
    q = backend.request("queue_get")
    assert [t["title"] for t in q["tracks"]] == ["A", "B"]
    assert q["index"] == 0


def test_transport_and_volume(backend):
    backend.request("toggle")
    assert backend.request("status")["paused"] is True
    assert backend.request("volume", {"level": 40})["volume"] == 40
    backend.request("seek", {"seconds": -5})
    assert ("seek", -5.0) in backend.fake.calls or any(c[0] == "seek" for c in backend.fake.calls)


def test_radio_replaces_the_queue(backend, catalogue):
    q = backend.request("radio", {"video_id": "seed", "title": "Seed", "artist": "S"})
    names = [c[0] for c in backend.fake.calls]
    assert names == ["stop", "play", "enqueue", "enqueue"]
    assert [t["title"] for t in q["tracks"]] == ["Seed", "R1", "R2"]


def test_lyrics_and_playlists(backend, catalogue, monkeypatch, tmp_path):
    from ytm import playlists_local

    monkeypatch.setattr(playlists_local, "DEFAULT_PATH", tmp_path / "pl.json")
    assert backend.request("lyrics", {"video_id": "v"})["lyrics"] == "words"
    lists = backend.request("playlist_list")["playlists"]
    assert lists[-1] == {"playlist_id": "PL1", "title": "Liked", "track_count": 3, "local": False}


def test_unknown_command(backend):
    with pytest.raises(BackendError, match="unknown command"):
        backend.request("dance")


def test_shutdown_quits_mpv(backend):
    assert backend.request("shutdown") == {"stopping": True}
    assert ("quit",) in backend.fake.calls


def test_listen_translates_property_changes_into_events(backend, monkeypatch):
    backend.request("play", {"video_id": "a", "title": "A", "artist": "Ar"})
    changes = [("volume", 70.0), ("pause", False), ("playlist-pos", 0), ("duration", 200.0), ("time-pos", 12.5)]

    class Observer:
        def observe(self, *names):
            for change in changes:
                yield change
            raise_after()

        def close(self):
            pass

    def raise_after():
        from ytm.player import PlayerError
        backend.close()  # simulates the TUI closing; listen must return quietly
        raise PlayerError("closed")

    backend._make_player = lambda spawn=True, timeout=None: Observer()
    events = []
    backend.on_event(lambda e, d: events.append((e, d)))
    backend._closed = False
    backend.listen()
    names = [e for e, _ in events]
    assert names == ["state_changed", "state_changed", "queue_changed", "track_changed", "position"]
    assert events[3][1]["title"] == "A"
    assert events[4][1] == {"position": 12.5, "video_id": "a", "duration_seconds": 200.0}
