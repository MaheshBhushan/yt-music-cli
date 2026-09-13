"""Tests for search-result normalisation and typed auth errors."""
import json

import pytest
from ytmusicapi.exceptions import YTMusicServerError

from ytm import api, auth

# Hand-written fixture mirroring the shapes ytmusicapi 1.12.1 actually returns
# for search(filter="songs"), including the degenerate ones.
SEARCH_RESPONSE = [
    {
        "category": "Songs",
        "resultType": "song",
        "videoId": "ZrOKjDZOtkA",
        "title": "Wonderwall",
        "artists": [{"name": "Oasis", "id": "UCmMUZbaYdNH0bEd1PAlAqsA"}],
        "album": {"name": "(What's The Story) Morning Glory?", "id": "MPREb_9nqEki4ZDpp"},
        "duration": "4:19",
        "duration_seconds": 259,
    },
    {
        # no album key at all
        "resultType": "song",
        "videoId": "noalbum123",
        "title": "Untitled Demo",
        "artists": [{"name": "Some Artist", "id": "UC1"}],
        "duration": "2:05",
        "duration_seconds": 125,
    },
    {
        # empty artists list
        "resultType": "song",
        "videoId": "noartist123",
        "title": "Field Recording",
        "artists": [],
        "album": None,
        "duration": "1:00",
        "duration_seconds": 60,
    },
    {
        # null duration
        "resultType": "song",
        "videoId": "nodur123",
        "title": "Mystery Length",
        "artists": [{"name": "Anon"}],
        "album": {"name": "Anon EP"},
        "duration": None,
        "duration_seconds": None,
    },
    {
        # personal upload: out of scope, must be filtered out
        "resultType": "song",
        "videoId": "upload123",
        "title": "My Own Rip",
        "artists": [{"name": "Me"}],
        "videoType": "MUSIC_VIDEO_TYPE_PRIVATELY_OWNED_TRACK",
        "duration": "3:00",
        "duration_seconds": 180,
    },
    {
        # upload from a library-scope search, identified by resultType/entityId
        "resultType": "upload",
        "entityId": "t_po_abc",
        "videoId": "upload456",
        "title": "Another Rip",
        "artists": [{"name": "Me"}],
        "duration": "3:30",
        "duration_seconds": 210,
    },
]


class FakeYTMusic:
    """Stand-in for ytmusicapi.YTMusic; never touches the network."""

    def __init__(self, results=None, error=None):
        self._results = results if results is not None else []
        self._error = error
        self.calls = []

    def search(self, query, filter=None, limit=20):
        self.calls.append({"query": query, "filter": filter, "limit": limit})
        if self._error:
            raise self._error
        return self._results


def test_normalises_fixture_into_tracks():
    tracks = api.to_tracks(SEARCH_RESPONSE)
    assert [t.video_id for t in tracks] == ["ZrOKjDZOtkA", "noalbum123", "noartist123", "nodur123"]
    first = tracks[0]
    assert first.title == "Wonderwall"
    assert first.artist == "Oasis"
    assert first.album == "(What's The Story) Morning Glory?"
    assert first.duration == "4:19"
    assert first.duration_seconds == 259


def test_missing_album_normalises_to_empty_string():
    no_album, null_album = api.to_tracks(SEARCH_RESPONSE)[1:3]
    assert no_album.album == ""
    assert null_album.album == ""


def test_empty_artists_normalises_to_placeholder():
    track = api.to_tracks(SEARCH_RESPONSE)[2]
    assert track.artist == "Unknown Artist"


def test_null_duration_normalises_to_zero():
    track = api.to_tracks(SEARCH_RESPONSE)[3]
    assert track.duration_seconds == 0
    assert track.duration == "0:00"


def test_uploads_filtered_out_of_search_results():
    yt = FakeYTMusic(results=SEARCH_RESPONSE)
    tracks = api.search("wonderwall", yt=yt)
    assert all(t.video_id not in {"upload123", "upload456"} for t in tracks)
    assert "My Own Rip" not in [t.title for t in tracks]
    assert len(tracks) == 4


def test_search_uses_songs_filter():
    yt = FakeYTMusic(results=[])
    api.search("kaanave kaanave", yt=yt)
    assert yt.calls[0]["filter"] == "songs"


def test_expired_auth_raises_typed_autherror_not_raw_traceback():
    expired = YTMusicServerError(
        "Server returned HTTP 401: Unauthorized.\nRequest had invalid authentication credentials."
    )
    yt = FakeYTMusic(error=expired)
    with pytest.raises(auth.AuthExpired) as excinfo:
        api.search("anything", yt=yt)
    assert "ytm auth" in str(excinfo.value)


def test_non_auth_server_error_is_not_swallowed():
    yt = FakeYTMusic(error=YTMusicServerError("Server returned HTTP 500: Internal Server Error.\nboom"))
    with pytest.raises(YTMusicServerError):
        api.search("anything", yt=yt)


class FakeLyricsYTMusic:
    """Stand-in for the two calls behind api.get_lyrics."""

    def __init__(self, browse_id="browse-1", lyrics_result=None, error=None):
        self._browse_id = browse_id
        self._lyrics_result = lyrics_result
        self._error = error
        self.watch_calls = []
        self.lyrics_calls = []

    def get_watch_playlist(self, videoId=None):
        self.watch_calls.append(videoId)
        if self._error:
            raise self._error
        data = {"tracks": []}
        if self._browse_id is not None:
            data["lyrics"] = self._browse_id
        return data

    def get_lyrics(self, browseId):
        self.lyrics_calls.append(browseId)
        return self._lyrics_result


def test_get_lyrics_returns_normalised_text_and_source():
    yt = FakeLyricsYTMusic(
        browse_id="browse-1",
        lyrics_result={"lyrics": "some lyrics text", "source": "Musixmatch"},
    )
    lyrics, source = api.get_lyrics("PYgcJpC6WAQ", yt=yt)
    assert lyrics == "some lyrics text"
    assert source == "Musixmatch"
    assert yt.watch_calls == ["PYgcJpC6WAQ"]
    assert yt.lyrics_calls == ["browse-1"]


def test_get_lyrics_missing_browse_id_returns_none_without_second_call():
    yt = FakeLyricsYTMusic(browse_id=None)
    lyrics, source = api.get_lyrics("no-lyrics-track", yt=yt)
    assert lyrics is None
    assert source is None
    assert yt.lyrics_calls == []


def test_get_lyrics_null_result_returns_none():
    yt = FakeLyricsYTMusic(browse_id="browse-1", lyrics_result=None)
    lyrics, source = api.get_lyrics("v1", yt=yt)
    assert lyrics is None
    assert source is None


def test_get_lyrics_expired_auth_raises_typed_autherror():
    expired = YTMusicServerError(
        "Server returned HTTP 401: Unauthorized.\nRequest had invalid authentication credentials."
    )
    yt = FakeLyricsYTMusic(error=expired)
    with pytest.raises(auth.AuthExpired):
        api.get_lyrics("v1", yt=yt)


def test_playlist_normalisation_marks_remote_and_local():
    remote = api.to_playlist({"playlistId": "PL1", "title": "Mix", "count": "12"})
    assert (remote.playlist_id, remote.title, remote.track_count, remote.local) == ("PL1", "Mix", 12, False)
    local = api.to_playlist({"playlistId": "local-1", "title": "Offline"}, local=True)
    assert local.local is True
    assert local.track_count == 0


def test_missing_auth_file_raises_authmissing(tmp_path):
    with pytest.raises(auth.AuthMissing) as excinfo:
        auth.load_headers(tmp_path / "auth.json")
    assert "ytm auth" in str(excinfo.value)


def test_cookies_exposed_for_stream_resolution(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"Cookie": "SID=abc; HSID=def", "authorization": "SAPISIDHASH x"}))
    assert auth.load_cookies(path) == "SID=abc; HSID=def"


def test_setup_writes_file_with_mode_0600(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    monkeypatch.setattr(auth.ytmusicapi, "setup", lambda: json.dumps({"cookie": "SID=abc"}))
    auth.setup(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["cookie"] == "SID=abc"
