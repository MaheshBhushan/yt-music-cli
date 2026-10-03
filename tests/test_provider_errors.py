"""Expiration vs. permission vs. provider failures.

Only HTTP 401 means "sign in again". A 403 can be a permission outcome for
a perfectly valid session, 400 is a request/provider incompatibility, 429 a
rate limit, 5xx the provider failing, and a network error leaves validity
unknown. None of those may show re-login advice, and a write is never
replayed automatically.
"""

import io
import json

import pytest
import requests
from ytmusicapi.exceptions import YTMusicServerError

from ytm import auth, cli, music
from ytm.auth import AuthExpired
from ytm.authentication.errors import SessionVerificationUnavailable
from ytm.music import BadRequest, PermissionDenied, ProviderError, ProviderUnavailable, RateLimited


def server_error(status, body=""):
    return YTMusicServerError(f"Server returned HTTP {status}: something failed.\n{body}")


# -- the classifier itself ----------------------------------------------------


def test_http_status_reads_only_the_librarys_own_prefix():
    assert auth.http_status(server_error(401)) == 401
    # digits in a response body are not a status code
    assert auth.http_status(YTMusicServerError(
        'Server returned HTTP 400: Bad Request.\n{"status": 500, "code": "401"}'
    )) == 400
    assert auth.http_status(YTMusicServerError("no status here")) is None
    assert auth.http_status(ValueError("HTTP 401")) is None


def test_is_expiry_is_401_only():
    assert auth.is_expiry(server_error(401))
    assert not auth.is_expiry(server_error(403))
    assert not auth.is_expiry(server_error(400))
    assert not auth.is_expiry(server_error(500))


# -- account operations -------------------------------------------------------


class FailingRead:
    def __init__(self, error):
        self.error = error

    def get_liked_songs(self, limit=100):
        raise self.error


@pytest.mark.parametrize("status,expected", [
    (401, AuthExpired),
    (403, PermissionDenied),
    (400, BadRequest),
    (429, RateLimited),
    (500, ProviderUnavailable),
    (503, ProviderUnavailable),
])
def test_account_errors_are_classified_by_status(status, expected):
    with pytest.raises(expected) as excinfo:
        music.liked_songs(yt=FailingRead(server_error(status)))
    message = str(excinfo.value)
    if expected is AuthExpired:
        assert "ytm login" in message
    else:
        assert "ytm login" not in message
        assert "HTTP %d" % status in message


def test_a_permission_denied_never_reads_as_expiry():
    with pytest.raises(PermissionDenied) as excinfo:
        music.liked_songs(yt=FailingRead(server_error(403)))
    assert not isinstance(excinfo.value, AuthExpired)
    assert "permission problem" in str(excinfo.value).lower()


# -- public operations --------------------------------------------------------


def test_a_public_playlist_401_is_a_provider_error_not_expiry():
    class YT:
        def get_playlist(self, playlist_id, limit=100):
            raise server_error(401, "SECRET RESPONSE BODY")

    with pytest.raises(ProviderError) as excinfo:
        music.get_playlist("PLpublic", yt=YT())
    assert not isinstance(excinfo.value, AuthExpired)
    assert "SECRET" not in str(excinfo.value)
    assert "ytm login" not in str(excinfo.value)


def test_public_lyrics_keep_the_provider_error_for_the_caller_to_classify():
    class YT:
        def get_watch_playlist(self, videoId=None):
            raise server_error(401, "SECRET RESPONSE BODY")

    with pytest.raises(YTMusicServerError):
        music.get_lyrics("v", yt=YT())


# -- writes are never replayed ------------------------------------------------


def test_a_rejected_write_is_not_refreshed_or_replayed(monkeypatch, tmp_path):
    from ytm import music as music_mod

    path = auth.AUTH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookie": "SID=old"}))
    auth._write_source(path, {"browser": "helium", "profile": None, "authuser": "0"})
    refreshes = []
    monkeypatch.setattr(auth, "refresh_from_browser", lambda *a, **k: refreshes.append(1))

    class Client:
        calls = 0

        def rate_song(self, video_id, rating):
            self.calls += 1
            raise server_error(401)

    client = Client()
    monkeypatch.setattr(music_mod, "shared_client", lambda: client)
    with pytest.raises(AuthExpired):
        music_mod.like("v")
    assert client.calls == 1
    assert refreshes == []


def test_a_read_still_recovers_once_through_a_browser_reimport(monkeypatch, tmp_path):
    """The retry policy is operation-aware: reads may be replayed, writes not."""

    path = auth.AUTH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookie": "SID=old"}))
    auth._write_source(path, {"browser": "helium", "profile": None, "authuser": "0"})
    state = {"refreshed": False}
    monkeypatch.setattr(
        auth, "refresh_from_browser",
        lambda *a, **k: state.__setitem__("refreshed", True),
    )

    class Recovering:
        def get_library_playlists(self, limit=25):
            if state["refreshed"]:
                return [{"playlistId": "LM", "title": "Liked Music"}]
            return []

        def get_account_info(self):
            if not state["refreshed"]:
                raise server_error(401)
            return {"accountName": "Test"}

    clients = iter([Recovering(), Recovering()])
    monkeypatch.setattr(music, "shared_client", lambda: next(clients))
    assert [p.title for p in music.library_playlists()] == ["Liked Music"]
    assert state["refreshed"] is True


# -- empty collections --------------------------------------------------------


def test_empty_liked_and_library_songs_are_valid_results():
    class YT:
        def get_liked_songs(self, limit=100):
            return {"tracks": []}

        def get_library_songs(self, limit=25, **kwargs):
            return []

        def get_account_info(self):
            return {"accountName": "New Listener"}

    assert music.liked_songs(yt=YT()) == []
    assert music.library_songs(yt=YT()) == []


def test_an_unreadable_account_response_is_unknown_not_expired():
    class Stale:
        def get_liked_songs(self, limit=100):
            raise KeyError("Unable to find 'twoColumnBrowseResultsRenderer' on {'SECRET': 'dump'}")

        def get_account_info(self):
            raise KeyError("Unable to find 'header' on {'SECRET': 'dump'}")

    with pytest.raises(SessionVerificationUnavailable) as excinfo:
        music.liked_songs(yt=Stale())
    assert "SECRET" not in str(excinfo.value)


def test_a_keyerror_with_a_live_account_is_a_brief_provider_error():
    class Odd:
        def get_liked_songs(self, limit=100):
            raise KeyError("Unable to find 'twoColumnBrowseResultsRenderer' on {'SECRET': 'dump'}")

        def get_account_info(self):
            return {"accountName": "Test Listener"}

    with pytest.raises(ProviderError, match="unexpected response for your liked songs") as excinfo:
        music.liked_songs(yt=Odd())
    assert "SECRET" not in str(excinfo.value)


def test_an_empty_library_songs_listing_probes_the_account():
    class Stale:
        def get_library_songs(self, limit=25, **kwargs):
            return []

        def get_account_info(self):
            raise KeyError("Unable to find 'header' on {'SECRET': 'dump'}")

    with pytest.raises(SessionVerificationUnavailable) as excinfo:
        music.library_songs(yt=Stale())
    assert "SECRET" not in str(excinfo.value)


def test_an_empty_library_with_a_real_account_is_a_valid_empty_list():
    class YT:
        def get_library_playlists(self, limit=25):
            return []

        def get_account_info(self):
            return {"accountName": "New Listener"}

    assert music.library_playlists(yt=YT()) == []


def test_an_empty_library_probe_that_cannot_reach_youtube_is_unknown():
    class YT:
        def get_library_playlists(self, limit=25):
            return []

        def get_account_info(self):
            raise requests.ConnectionError("no route")

    with pytest.raises(SessionVerificationUnavailable):
        music.library_playlists(yt=YT())


def test_an_empty_library_probe_that_is_signed_out_is_expiry():
    class YT:
        def get_library_playlists(self, limit=25):
            return []

        def get_account_info(self):
            raise server_error(401)

    with pytest.raises(AuthExpired, match="signed out"):
        music.library_playlists(yt=YT())


# -- CLI rendering ------------------------------------------------------------


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_never_prints_a_provider_response_body(monkeypatch):
    def exploding(query, limit=20, yt=None):
        raise server_error(500, "SECRET RESPONSE BODY")

    monkeypatch.setattr(music, "search", exploding)
    code, out, err = run("search", "anything")
    assert code == 1
    assert "HTTP 500" in err
    assert "SECRET" not in err
    assert "Traceback" not in err


def test_cli_prints_a_typed_provider_error_verbatim(monkeypatch):
    def forbidden(query, limit=20, yt=None):
        raise PermissionDenied("YouTube Music refused this operation (HTTP 403).")

    monkeypatch.setattr(music, "search", forbidden)
    code, out, err = run("search", "anything")
    assert code == 1
    assert "HTTP 403" in err and "Traceback" not in err


@pytest.mark.parametrize("account", [None, {}, {"accountName": ""}, {"accountName": 123}])
def test_missing_account_identity_does_not_prove_expiry(account):
    class YT:
        def get_account_info(self):
            return account

    with pytest.raises(SessionVerificationUnavailable) as excinfo:
        music._probe_account(YT())
    assert "ytm login" not in str(excinfo.value)


@pytest.mark.parametrize("error", [KeyError("SECRET"), TypeError("SECRET"), ValueError("SECRET")])
def test_unreadable_probe_never_refreshes_or_replaces_credentials(monkeypatch, error):
    from tests.test_auth_migration import headers
    from ytm.authentication.storage import StoredRecord

    store = auth.session_store()
    store.save(StoredRecord.browser(headers()), expected_revision=None)
    before = store.path.read_bytes()

    class YT:
        def get_library_playlists(self, limit=25):
            return []

        def get_account_info(self):
            raise error

    monkeypatch.setattr(music, "shared_client", lambda: YT())
    monkeypatch.setattr(auth, "refresh_from_browser", lambda *a, **kw: pytest.fail("must not refresh"))
    with pytest.raises(SessionVerificationUnavailable) as excinfo:
        music.library_playlists()
    assert "SECRET" not in str(excinfo.value)
    assert "ytm login" not in str(excinfo.value)
    assert store.path.read_bytes() == before
    assert auth.auth_manager().status(validate=False).logged_in


SIGNED_OUT_MENU = KeyError(
    "Unable to find 'header' using path ['actions', 0, 'openPopupAction', 'popup', "
    "'multiPageMenuRenderer', 'header', 'activeAccountHeaderRenderer', 'accountName'] "
    "on {'SECRET': 'dump'}, exception: 'header'"
)


def test_an_account_menu_without_its_header_is_signed_out():
    class YT:
        def get_account_info(self):
            raise SIGNED_OUT_MENU

    with pytest.raises(AuthExpired) as excinfo:
        music._probe_account(YT())
    assert "SECRET" not in str(excinfo.value)


def test_a_signed_out_browser_record_is_reimported_and_retried(monkeypatch):
    from tests.test_auth_migration import headers
    from ytm.authentication.storage import StoredRecord

    store = auth.session_store()
    store.save(StoredRecord.browser(headers(), source="existing_browser:chrome"), expected_revision=None)
    calls = []

    class Stale:
        def get_library_playlists(self, limit=25):
            return []

        def get_account_info(self):
            raise SIGNED_OUT_MENU

    class Fresh:
        def get_library_playlists(self, limit=25):
            return [{"playlistId": "PL1", "title": "favs"}]

    yts = iter([Stale(), Fresh()])
    monkeypatch.setattr(music, "shared_client", lambda: next(yts))
    monkeypatch.setattr(auth, "refresh_from_browser", lambda *a, **kw: calls.append(auth.browser_source(*a)))
    monkeypatch.setattr(music, "_remember_visitor_id", lambda: None)
    assert [p.title for p in music.library_playlists()] == ["favs"]
    assert calls and calls[0]["browser"] == "chrome"
