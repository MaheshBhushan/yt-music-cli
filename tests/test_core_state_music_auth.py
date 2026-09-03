"""Tests for the small core modules added by the mpv refactor:
ytm.state, the music.py additions, and auth.cookies_file."""

import json
import os
import stat

import pytest

from ytm import auth, music, state
from ytm.music import Track


def track(video_id, title="T", artist="A"):
    return Track(video_id, title, artist, "", "3:00", 180)


# -- state -----------------------------------------------------------------------


def test_state_starts_empty_and_survives_garbage(tmp_path):
    path = tmp_path / "s.json"
    assert state.load(path) == {"last_search": [], "tracks": {}}
    path.write_text("not json")
    assert state.load(path)["tracks"] == {}
    path.write_text("[1,2]")
    assert state.load(path)["last_search"] == []


def test_last_search_round_trips_tracks(tmp_path):
    path = tmp_path / "s.json"
    state.remember_search([track("a"), track("b", "Bee")], path)
    assert [t.video_id for t in state.last_search(path)] == ["a", "b"]
    assert state.last_search(path)[1].title == "Bee"
    # searched tracks are also remembered individually
    assert state.track_for("b", path).title == "Bee"


def test_remembered_tracks_are_bounded_and_most_recent_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "TRACK_MEMORY", 3)
    path = tmp_path / "s.json"
    state.remember_tracks([track("a"), track("b"), track("c")], path)
    state.remember_tracks([track("a", "A again")], path)  # refresh a
    state.remember_tracks([track("d")], path)  # evicts the oldest: b
    known = state.load(path)["tracks"]
    assert list(known) == ["c", "a", "d"]
    assert state.track_for("b", path) is None
    assert state.track_for("a", path).title == "A again"


def test_save_is_atomic(tmp_path):
    path = tmp_path / "s.json"
    state.save({"last_search": [], "tracks": {}}, path)
    assert not (tmp_path / "s.tmp").exists()
    assert json.loads(path.read_text()) == {"last_search": [], "tracks": {}}


# -- music additions -------------------------------------------------------------------


class FakeYT:
    def __init__(self):
        self.calls = []

    def get_song(self, video_id):
        self.calls.append(("get_song", video_id))
        if video_id == "missing":
            return {"videoDetails": {}}
        return {"videoDetails": {"videoId": video_id, "title": "Song", "author": "Band", "lengthSeconds": "185"}}

    def get_watch_playlist(self, videoId, radio=False, limit=25):
        self.calls.append(("watch", videoId, radio, limit))
        return {"tracks": [
            {"videoId": videoId, "title": "seed", "artists": [{"name": "S"}], "duration_seconds": 1},
            {"videoId": "n1", "title": "Next", "artists": [{"name": "N"}], "duration_seconds": 100},
            {"videoId": "up", "title": "Upload", "videoType": music.UPLOAD_VIDEO_TYPE},
        ]}

    def rate_song(self, video_id, rating):
        self.calls.append(("rate", video_id, rating))
        return {"ok": True}


def test_song_normalises_video_details():
    yt = FakeYT()
    t = music.song("abc", yt=yt)
    assert (t.video_id, t.title, t.artist, t.duration, t.duration_seconds) == ("abc", "Song", "Band", "3:05", 185)
    assert music.song("missing", yt=yt) is None


def test_radio_excludes_the_seed_and_uploads():
    yt = FakeYT()
    tracks = music.radio("seed", limit=10, yt=yt)
    assert [t.video_id for t in tracks] == ["n1"]
    assert yt.calls == [("watch", "seed", True, 10)]


def test_like_rates_the_song():
    yt = FakeYT()
    music.like("abc", yt=yt)
    assert yt.calls == [("rate", "abc", "LIKE")]


def test_watch_url():
    assert music.watch_url("abc") == "https://music.youtube.com/watch?v=abc"


def test_api_shim_still_exposes_the_old_names():
    from ytm import api

    assert api.search is music.search and api.Track is music.Track


# -- auth.cookies_file --------------------------------------------------------------------


def test_cookies_file_writes_netscape_format_from_the_header(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"cookie": "SID=abc; __Secure-3PAPISID=xyz"}))
    cookies = tmp_path / "cookies.txt"
    assert auth.cookies_file(auth_path, cookies) == str(cookies)
    lines = cookies.read_text().splitlines()
    assert lines[0].startswith("# Netscape")
    assert ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tabc" in lines
    assert ".youtube.com\tTRUE\t/\tTRUE\t2147483647\t__Secure-3PAPISID\txyz" in lines
    assert stat.S_IMODE(os.stat(cookies).st_mode) == 0o600


def test_cookies_file_is_reused_until_auth_changes(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"cookie": "SID=one"}))
    cookies = tmp_path / "cookies.txt"
    auth.cookies_file(auth_path, cookies)
    first = cookies.read_text()
    auth.cookies_file(auth_path, cookies)
    assert cookies.read_text() == first
    # a newer auth.json regenerates it
    os.utime(cookies, (1, 1))
    auth_path.write_text(json.dumps({"cookie": "SID=two"}))
    auth.cookies_file(auth_path, cookies)
    assert "two" in cookies.read_text()


def test_cookies_file_is_none_without_cookies(tmp_path):
    assert auth.cookies_file(tmp_path / "nope.json", tmp_path / "c.txt") is None
    oauth = tmp_path / "auth.json"
    oauth.write_text(json.dumps({"access_token": "t", "refresh_token": "r", "expires_at": 1, "expires_in": 1, "scope": "s", "token_type": "Bearer"}))
    assert auth.cookies_file(oauth, tmp_path / "c.txt") is None


# -- thumbnails ----------------------------------------------------------------------


def test_pick_thumbnail_prefers_the_smallest_usable_size():
    thumbs = [
        {"url": "tiny", "width": 60, "height": 60},
        {"url": "small", "width": 120, "height": 120},
        {"url": "big", "width": 544, "height": 544},
    ]
    assert music.pick_thumbnail(thumbs) == "small"
    assert music.pick_thumbnail(thumbs[:1]) == "tiny"  # nothing big enough: the largest
    assert music.pick_thumbnail([]) == ""
    assert music.pick_thumbnail(None) == ""


def test_to_track_carries_a_thumbnail_and_old_state_without_one_still_loads(tmp_path):
    t = music.to_track({"videoId": "v", "title": "T", "thumbnails": [{"url": "u", "width": 200}]})
    assert t.thumbnail == "u"
    radio_item = music.to_track({"videoId": "r", "title": "R", "thumbnail": [{"url": "ru", "width": 200}]})
    assert radio_item.thumbnail == "ru"
    # a session file written before the field existed
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"last_search": [], "tracks": {"v": {
        "video_id": "v", "title": "T", "artist": "", "album": "", "duration": "", "duration_seconds": 0}}}))
    assert state.track_for("v", path).thumbnail == ""
