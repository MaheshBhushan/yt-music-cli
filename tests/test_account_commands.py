"""`ytm liked`, `ytm library`, `ytm playlists`: account listings that fail cleanly."""

import io
import json

import pytest

from ytm import cli, music
from ytm.auth import AuthMissing
from ytm.music import Playlist, Track


def track(video_id, title="T", artist="A", seconds=200):
    return Track(video_id, title, artist, "Album", f"{seconds // 60}:{seconds % 60:02d}", seconds)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# -- music layer --------------------------------------------------------------


class FakeAccount:
    def __init__(self, liked=None, library=None, playlists=None):
        self.liked = liked if liked is not None else []
        self.library = library if library is not None else []
        self.playlists = playlists if playlists is not None else []

    def get_liked_songs(self, limit=100):
        return {"tracks": self.liked[:limit]}

    def get_library_songs(self, limit=25, **kwargs):
        return self.library[:limit]

    def get_library_playlists(self, limit=25):
        return self.playlists[:limit]

    def get_account_info(self):
        # the bounded probe an empty library listing triggers
        return {"accountName": "Test Listener"}


def test_liked_songs_normalise_and_empty_is_valid():
    yt = FakeAccount(liked=[{"videoId": "v1", "title": "Song", "artists": [{"name": "Band"}]}])
    assert [t.video_id for t in music.liked_songs(yt=yt)] == ["v1"]
    assert music.liked_songs(yt=FakeAccount()) == []


def test_library_songs_normalise_and_empty_is_valid():
    yt = FakeAccount(library=[{"videoId": "v2", "title": "Saved", "artists": [{"name": "Band"}]}])
    assert [t.video_id for t in music.library_songs(yt=yt)] == ["v2"]
    assert music.library_songs(yt=FakeAccount()) == []


# -- cli ----------------------------------------------------------------------


def test_liked_renders_numbered_results(monkeypatch):
    monkeypatch.setattr(
        music, "liked_songs",
        lambda limit=25, yt=None: [track("v1", "First", "Band"), track("v2", "Second", "Band")],
    )
    code, out, err = run("liked")
    assert code == 0
    assert out.splitlines() == [
        "1. First — Band  (3:20)",
        "2. Second — Band  (3:20)",
    ]


def test_liked_empty_result_is_success_not_an_error(monkeypatch):
    monkeypatch.setattr(music, "liked_songs", lambda limit=25, yt=None: [])
    code, out, err = run("--json", "liked")
    assert code == 0
    assert json.loads(out) == {"tracks": []}
    assert out.strip() == '{"tracks": []}'


def test_library_uses_the_song_listing(monkeypatch):
    seen = []
    monkeypatch.setattr(
        music, "library_songs",
        lambda limit=25, yt=None: (seen.append(limit), [track("v1", "Saved")])[1],
    )
    code, out, err = run("library", "-n", "5")
    assert code == 0
    assert seen == [5]
    assert out.startswith("1. Saved — A")


def test_playlists_lists_remote_playlists(monkeypatch):
    monkeypatch.setattr(
        music, "library_playlists",
        lambda limit=25, yt=None: [Playlist("PL1", "Road Trip", 12), Playlist("LM", "Liked Music", 300)],
    )
    code, out, err = run("playlists")
    assert code == 0
    assert out.splitlines() == ["Road Trip — 12 tracks", "Liked Music — 300 tracks"]


def test_playlists_local_lists_without_any_account(monkeypatch, tmp_path):
    from ytm import playlists_local

    monkeypatch.setattr(playlists_local, "DEFAULT_PATH", tmp_path / "pl.json")
    monkeypatch.setattr(music, "library_playlists", lambda *a, **k: pytest.fail("must not access the account"))
    playlists_local.create("Offline mix")
    code, out, err = run("playlists", "--local")
    assert code == 0
    assert out.strip() == "Offline mix — 0 tracks"


@pytest.mark.parametrize("argv", [["liked"], ["library"], ["playlists"]])
def test_account_listings_without_credentials_say_how_to_login(monkeypatch, argv):
    def no_credentials(*args, **kwargs):
        raise AuthMissing(
            "This command requires your YouTube Music account.\nRun 'ytm login' to sign in."
        )

    monkeypatch.setattr(music, "shared_client", no_credentials)
    code, out, err = run(*argv)
    assert code == 1
    assert "Run 'ytm login' to sign in." in err
    assert "Traceback" not in err
    assert "ytm auth" not in err


def test_an_unreadable_account_response_does_not_request_login(monkeypatch):
    """Parser failures leave validity unknown and never expose response data."""

    class Stale:
        def get_liked_songs(self, limit=100):
            raise KeyError(
                "Unable to find 'twoColumnBrowseResultsRenderer' using path [...] "
                "on {'SECRET RESPONSE BODY': true}"
            )

        def get_account_info(self):
            raise KeyError(
                "Unable to find 'header' using path [...] on {'SECRET RESPONSE BODY': true}"
            )

    monkeypatch.setattr(music, "shared_client", lambda: Stale())
    code, out, err = run("liked")
    assert code == 1
    assert out == ""  # failures render to stderr only
    assert "Stored credentials were kept" in err
    assert "ytm login" not in err
    assert "Traceback" not in err
    assert "SECRET" not in err
