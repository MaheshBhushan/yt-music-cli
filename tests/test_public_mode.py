"""Public operations stay anonymous.

Search, song metadata, radio, lyrics and public playlist reads must never
read stored credentials, build an OAuth client, reimport browser cookies,
or import Playwright -- even when a malformed or expired auth file is
sitting at the credential path. The forbidden functions raise here, so a
test fails on entering the account branch, not merely on a wrong result.
"""

import json
import sys

import pytest

from ytm import auth, music


class FakeAnonymous:
    """Stand-in for ytmusicapi.YTMusic() as the public paths use it."""

    built = []

    def __init__(self, *args, **kwargs):
        FakeAnonymous.built.append((args, kwargs))

    def search(self, query, filter=None, limit=20):
        return [
            {"videoId": "v1", "title": "Song", "artists": [{"name": "Artist"}], "duration_seconds": 100},
        ]

    def get_song(self, video_id):
        return {"videoDetails": {"videoId": video_id, "title": "Song", "author": "Band", "lengthSeconds": "100"}}

    def get_watch_playlist(self, videoId=None, radio=False, limit=25):
        if radio:
            return {
                "tracks": [
                    {"videoId": videoId, "title": "seed"},
                    {"videoId": "r1", "title": "Next", "artists": [{"name": "X"}], "duration_seconds": 100},
                ]
            }
        return {"lyrics": "browse-1"}

    def get_lyrics(self, browse_id, timestamps=False):
        if timestamps:
            return {
                "hasTimestamps": True,
                "lyrics": [{"text": "la", "start_time": 0, "end_time": 1000}],
                "source": "src",
            }
        return {"lyrics": "la la", "source": "src"}

    def get_playlist(self, playlist_id, limit=100):
        return {
            "id": playlist_id,
            "title": "Public",
            "trackCount": "2",
            "tracks": [{"videoId": "t1", "title": "T1", "artists": [{"name": "A"}]}],
        }


@pytest.fixture
def anonymous(monkeypatch):
    """Every ytmusicapi client a public path builds is the fake above."""
    FakeAnonymous.built = []
    monkeypatch.setattr(music.ytmusicapi, "YTMusic", FakeAnonymous)
    monkeypatch.setattr(music, "_CATALOGUE_CLIENT", None)
    monkeypatch.setattr(music, "_LYRICS_CLIENT", None)
    yield FakeAnonymous
    music.reset_client()


def _forbid(monkeypatch, module, *names):
    def fail(*args, **kwargs):
        pytest.fail("a public operation touched account authentication")

    for name in names:
        monkeypatch.setattr(module, name, fail)


@pytest.fixture
def no_account_touch(monkeypatch):
    """Any account lookup, refresh, or OAuth construction is a failure."""
    _forbid(
        monkeypatch,
        music,
        "shared_client",
        "client",
        "_seeded_headers",
        "_account_get_playlist",
        "_account_playlist_count",
    )
    _forbid(
        monkeypatch,
        auth,
        "load_headers",
        "browser_headers",
        "client",
        "refresh_from_browser",
        "oauth_setup",
        "_oauth_client",
        "cookies_file",
    )


def _write_auth(text):
    auth.AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth.AUTH_PATH.write_text(text)


def _public_calls():
    """One call of each public operation, as (name, callable) pairs."""
    return [
        ("search", lambda: music.search("q")),
        ("song", lambda: music.song("v1")),
        ("radio", lambda: music.radio("v1")),
        ("plain lyrics", lambda: music.get_lyrics("v1")),
        ("timed lyrics", lambda: music.get_lyrics("v1", timestamps=True)),
        ("public playlist", lambda: music.get_playlist("PLpublic")),
        ("public playlist count", lambda: music.playlist_count("PLpublic")),
    ]


# -- P01, P06, P07, P08, P09, P10: every public path is anonymous ----------


def test_public_operations_build_only_argument_free_anonymous_clients(anonymous, no_account_touch):
    for name, call in _public_calls():
        call()  # must not raise
        assert anonymous.built, f"{name} built no client at all"
    assert all(args == () and kwargs == {} for args, kwargs in anonymous.built), (
        "a public client was constructed with credentials"
    )


def test_search_song_and_radio_use_the_anonymous_client(anonymous, no_account_touch):
    assert [t.video_id for t in music.search("q")] == ["v1"]
    assert music.song("v1").title == "Song"
    assert [t.video_id for t in music.radio("v1")] == ["r1"]
    assert len(anonymous.built) == 1  # one shared anonymous client, no auth


def test_plain_lyrics_are_anonymous(anonymous, no_account_touch):
    assert music.get_lyrics("v1") == ("la la", "src")


def test_timed_lyrics_get_their_own_anonymous_client(anonymous, no_account_touch):
    search_client = music.catalogue_client()
    lines, source = music.get_lyrics("v1", timestamps=True)
    assert [line["text"] for line in lines] == ["la"]
    assert source == "src"
    assert music.lyrics_client() is not search_client
    # a second timed call reuses the lyrics client and still leaves search alone
    music.get_lyrics("v1", timestamps=True)
    assert music.catalogue_client() is search_client
    assert len(anonymous.built) == 2


def test_public_playlist_and_count_are_anonymous(anonymous, no_account_touch):
    playlist, tracks = music.get_playlist("PLpublic")
    assert (playlist.playlist_id, playlist.title) == ("PLpublic", "Public")
    assert [t.video_id for t in tracks] == ["t1"]
    assert music.playlist_count("PLpublic") == 2


# -- P02, P03: stored credentials cannot drag a public path into auth ------


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("invalid credentials", id="malformed"),
        pytest.param(
            json.dumps(
                {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expires_at": 1,
                    "expires_in": 1,
                    "scope": "s",
                    "token_type": "Bearer",
                }
            ),
            id="expired-oauth",
        ),
    ],
)
def test_stored_credentials_are_never_read_by_public_operations(anonymous, no_account_touch, stored):
    _write_auth(stored)
    for name, call in _public_calls():
        call()  # the forbidden fixtures would fail on any credential read


def test_public_playback_path_does_not_derive_a_cookie_file(anonymous, no_account_touch, monkeypatch):
    from ytm import cli, config

    cfg = config.load()
    cfg["audio"]["control"] = "player"
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    made = {}

    class FakePlayer:
        def __init__(self, **kwargs):
            made.update(kwargs)

    monkeypatch.setattr(cli, "Player", FakePlayer)
    cli.player(spawn=False)
    # authenticated_streams is off by default, so stream resolution gets no
    # cookie file and nothing reads the credential store to build the player
    assert made["cookies_file"] is None


# -- P11: local playlists stay local ---------------------------------------


def test_local_playlist_crud_builds_no_client(anonymous, no_account_touch, monkeypatch, tmp_path):
    from tests.test_cli_core import FakePlayer
    from ytm import playlists_local
    from ytm.tui.backend import Backend

    monkeypatch.setattr(playlists_local, "DEFAULT_PATH", tmp_path / "pl.json")
    monkeypatch.setattr("ytm.state.STATE_PATH", tmp_path / "session.json")
    fake = FakePlayer()
    backend = Backend(player_factory=lambda spawn=True, timeout=None: fake)
    made = backend.request("playlist_create", {"title": "Offline", "local": True})
    assert made["local"] is True
    backend.request("playlist_add", {
        "playlist_id": made["playlist_id"], "video_ids": ["v1"],
        "tracks": [{"video_id": "v1", "title": "Song"}],
    })
    got = backend.request("playlist_get", {"playlist_id": made["playlist_id"]})
    assert [t["video_id"] for t in got["tracks"]] == ["v1"]
    backend.close()
    assert anonymous.built == [], "a local playlist operation built a network client"


# -- P12: public mode keeps working with no credentials at all -------------


def test_public_operations_with_no_credentials_are_unaffected(anonymous, no_account_touch):
    assert not auth.AUTH_PATH.exists()
    for name, call in _public_calls():
        call()


# -- P05: no browser automation or keyring is imported by public paths -----


def test_public_paths_import_no_browser_automation_or_keyring(anonymous, no_account_touch):
    before = set(sys.modules)
    for name, call in _public_calls():
        call()
    newly_imported = set(sys.modules) - before
    assert not any(name == "playwright" or name.startswith("playwright.") for name in newly_imported)
    assert not any(name == "keyring" or name.startswith("keyring.") for name in newly_imported)


# -- the access policy itself ----------------------------------------------


class FakeAccount:
    def __init__(self):
        self.calls = []

    def get_playlist(self, playlist_id, limit=100):
        self.calls.append(("get_playlist", playlist_id, limit))
        return {"id": playlist_id, "title": "Account", "trackCount": "1", "tracks": []}


def test_account_only_and_library_playlists_use_the_account_client(monkeypatch):
    account = FakeAccount()
    chosen = []
    monkeypatch.setattr(music, "shared_client", lambda: chosen.append("account") or account)
    monkeypatch.setattr(music, "catalogue_client", lambda: chosen.append("anonymous") or FakeAnonymous())

    music.get_playlist("LM")
    music.get_playlist("SE")
    music.get_playlist("RDTMAK5uy_mix")
    music.get_playlist("PLfrom-library", require_auth=True)
    music.playlist_count("LM")
    assert chosen == ["account"] * 5

    music.get_playlist("PLpublic")
    music.playlist_count("PLpublic")
    assert chosen[-2:] == ["anonymous", "anonymous"]
    assert music.account_playlist("LM") and music.account_playlist("RDTMAK5uy_mix")
    assert music.account_playlist("PLx", require_auth=True)
    assert not music.account_playlist("PLpublic")
