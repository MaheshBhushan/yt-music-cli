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


def test_play_keeps_the_remembered_thumbnail_when_the_client_omits_it(backend, catalogue):
    backend.request("search", {"query": "x"})  # remembers s1 with its thumbnail
    state.remember_tracks([track("s1", "Song", "Band", "LP", 200).__class__(
        "s1", "Song", "Band", "LP", "3:20", 200, thumbnail="https://img/s1.jpg")])
    backend.request("play", {"video_id": "s1", "title": "Song"})
    assert state.track_for("s1").thumbnail == "https://img/s1.jpg"


def test_listen_reannounces_position_once_the_duration_is_known(backend):
    backend.request("play", {"video_id": "a", "title": "A"})
    changes = [("time-pos", 3.0), ("duration", 200.0), ("time-pos", 4.0)]

    class Observer:
        def observe(self, *names):
            assert names.index("duration") < names.index("time-pos")
            yield from changes
            backend.close()
            from ytm.player import PlayerError
            raise PlayerError("closed")

        def close(self):
            pass

    backend._make_player = lambda spawn=True, timeout=None: Observer()
    events = []
    backend.on_event(lambda e, d: events.append((e, d)))
    backend._closed = False
    backend.listen()
    positions = [d for e, d in events if e == "position"]
    assert [(p["position"], p["duration_seconds"]) for p in positions] == [(3.0, 0), (3.0, 200.0), (4.0, 200.0)]


def test_queue_play_jumps_to_the_index(backend):
    backend.request("play", {"video_id": "a", "title": "A"})
    backend.request("enqueue", {"video_id": "b", "title": "B"})
    backend.request("queue_play", {"index": 1})
    assert ("play_index", 1) in backend.fake.calls


def test_playlist_play_replaces_the_queue(backend, monkeypatch):
    from ytm.music import Playlist

    monkeypatch.setattr(
        music, "get_playlist",
        lambda pid, limit=100, yt=None: (Playlist(pid, "Mix", 2), [track("p1", "P1", "X"), track("p2", "P2", "Y")]),
    )
    q = backend.request("playlist_play", {"playlist_id": "PLx"})
    names = [c[0] for c in backend.fake.calls]
    assert names == ["stop", "play", "enqueue"]
    assert [t["title"] for t in q["tracks"]] == ["P1", "P2"]


def test_default_player_passes_no_timeout_through_for_the_observer(monkeypatch):
    from ytm import cli
    from ytm.tui.backend import _default_player

    seen = []
    monkeypatch.setattr(cli, "player", lambda spawn=True, **kw: seen.append((spawn, kw)))
    _default_player(spawn=False, timeout=None)
    assert seen == [(False, {"timeout": None})]


def test_playlist_list_fills_in_missing_remote_counts(backend, catalogue, monkeypatch, tmp_path):
    from ytm import playlists_local

    monkeypatch.setattr(playlists_local, "DEFAULT_PATH", tmp_path / "pl.json")
    monkeypatch.setattr(music, "library_playlists", lambda limit=25, yt=None: [
        music.Playlist("LM", "Liked Music", 0), music.Playlist("PL9", "Mix", 12)])
    asked = []
    monkeypatch.setattr(music, "playlist_count", lambda pid, yt=None: (asked.append(pid), 9)[1])
    lists = backend.request("playlist_list")["playlists"]
    assert asked == ["LM"]  # only the one without a count
    assert [(p["title"], p["track_count"]) for p in lists] == [("Liked Music", 9), ("Mix", 12)]


def test_playlist_create_remote_and_local(backend, monkeypatch, tmp_path):
    from ytm import playlists_local

    monkeypatch.setattr(playlists_local, "DEFAULT_PATH", tmp_path / "pl.json")
    monkeypatch.setattr(music, "create_playlist", lambda title, description="", privacy="PRIVATE", yt=None: "PLx")
    assert backend.request("playlist_create", {"title": "Road Trip"}) == {"playlist_id": "PLx", "title": "Road Trip", "local": False}
    made = backend.request("playlist_create", {"title": "Offline", "local": True})
    assert made["local"] is True and playlists_local.is_local_id(made["playlist_id"])
    with pytest.raises(BackendError, match="name"):
        backend.request("playlist_create", {"title": "  "})
