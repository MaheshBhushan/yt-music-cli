"""Stale browser cookies are re-extracted and the call retried, by itself."""

import json

import pytest

from ytm import auth, music
from ytm.auth import AuthError, AuthExpired
from tests.test_from_browser import _FakeCookie, _fake_client_ok, _fake_client_fails, _jar


def _extracted(monkeypatch, calls=None):
    jar = _jar(_FakeCookie("SID", "s"), _FakeCookie("__Secure-3PAPISID", "p"))

    def extract(name, profile=None, logger=None):
        if calls is not None:
            calls.append((name, profile))
        return jar

    monkeypatch.setattr(auth, "extract_cookies_from_browser", extract)


def test_from_browser_records_where_the_cookies_came_from(tmp_path, monkeypatch):
    _extracted(monkeypatch)
    path = tmp_path / "auth.json"
    auth.from_browser("chrome", path=path, client_factory=_fake_client_ok, profile="Profile 2", authuser="1")
    assert json.loads(auth.source_path(path).read_text()) == {
        "browser": "chrome", "profile": "Profile 2", "authuser": "1",
    }
    assert auth.browser_source(path)["browser"] == "chrome"


def test_autodetect_records_the_browser_that_had_the_session(tmp_path, monkeypatch):
    empty = _jar()
    good = _jar(_FakeCookie("__Secure-3PAPISID", "p"))
    monkeypatch.setattr(auth, "_AUTODETECT_BROWSERS", ("chrome", "firefox"))
    monkeypatch.setattr(
        auth, "extract_cookies_from_browser",
        lambda name, profile=None, logger=None: good if name == "firefox" else empty,
    )
    path = tmp_path / "auth.json"
    auth.from_browser(None, path=path, client_factory=_fake_client_ok, config={"auth": {"x-goog-authuser": "0"}})
    assert auth.browser_source(path)["browser"] == "firefox"


def test_failed_extraction_leaves_no_source_record_either(tmp_path, monkeypatch):
    _extracted(monkeypatch)
    path = tmp_path / "auth.json"
    with pytest.raises(AuthError):
        auth.from_browser("chrome", path=path, client_factory=_fake_client_fails, authuser="0")
    assert not auth.source_path(path).exists()
    assert auth.browser_source(path) is None


def test_pasted_or_oauth_auth_has_no_browser_source(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{}")
    assert auth.browser_source(path) is None
    with pytest.raises(AuthError, match="not extracted from a browser"):
        auth.refresh_from_browser(path, client_factory=_fake_client_ok)


def test_refresh_reuses_the_recorded_browser_profile_and_authuser(tmp_path, monkeypatch):
    calls = []
    _extracted(monkeypatch, calls)
    path = tmp_path / "auth.json"
    auth.from_browser("chrome", path=path, client_factory=_fake_client_ok, profile="Profile 2", authuser="1")
    auth.refresh_from_browser(path, client_factory=_fake_client_ok)
    assert calls == [("chrome", "Profile 2"), ("chrome", "Profile 2")]
    assert json.loads(path.read_text())["x-goog-authuser"] == "1"


# -- the catalogue layer: stale -> refresh -> retry --------------------------------


class Stale:
    """A client whose library is empty until the auth file is refreshed."""

    def __init__(self, fresh):
        self.fresh = fresh

    def get_library_playlists(self, limit=25):
        return [{"playlistId": "LM", "title": "Liked Music"}] if self.fresh() else []


def _browser_auth(monkeypatch, tmp_path, refresh_ok=True):
    """Auth at the test AUTH_PATH that came from a browser; `refreshed` flips
    when refresh_from_browser runs."""
    path = auth.AUTH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookie": "SID=old"}))
    auth._write_source(path, {"browser": "helium", "profile": None, "authuser": "0"})
    state = {"refreshed": False}

    def refresh(path=auth.AUTH_PATH, client_factory=None):
        if not refresh_ok:
            raise AuthError("No logged-in YouTube session found. helium: no YouTube cookies")
        state["refreshed"] = True

    monkeypatch.setattr(auth, "refresh_from_browser", refresh)
    monkeypatch.setattr(music, "client", lambda: Stale(lambda: state["refreshed"]))
    return state


def test_signed_out_library_refreshes_the_cookies_and_retries(monkeypatch, tmp_path):
    state = _browser_auth(monkeypatch, tmp_path)
    playlists = music.library_playlists()
    assert [p.title for p in playlists] == ["Liked Music"]
    assert state["refreshed"]


def test_when_the_browser_is_signed_out_too_the_error_says_both(monkeypatch, tmp_path):
    _browser_auth(monkeypatch, tmp_path, refresh_ok=False)
    with pytest.raises(AuthExpired) as excinfo:
        music.library_playlists()
    message = str(excinfo.value)
    assert "signed out" in message
    assert "re-extract them from helium" in message
    assert "No logged-in YouTube session found" in message


def test_pasted_headers_are_not_refreshed(monkeypatch, tmp_path):
    path = auth.AUTH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookie": "SID=old"}))
    monkeypatch.setattr(auth, "refresh_from_browser", lambda *a, **k: pytest.fail("must not refresh"))
    monkeypatch.setattr(music, "client", lambda: Stale(lambda: False))
    with pytest.raises(AuthExpired, match="signed out") as excinfo:
        music.library_playlists()
    assert "re-extract" not in str(excinfo.value)


def test_a_caller_supplied_client_is_never_refreshed(monkeypatch, tmp_path):
    _browser_auth(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "refresh_from_browser", lambda *a, **k: pytest.fail("must not refresh"))
    with pytest.raises(AuthExpired):
        music.library_playlists(yt=Stale(lambda: False))


def test_expired_headers_on_search_are_refreshed_too(monkeypatch, tmp_path):
    from ytmusicapi.exceptions import YTMusicError

    state = _browser_auth(monkeypatch, tmp_path)

    class Client:
        def search(self, query, filter=None, limit=20):
            if not state["refreshed"]:
                raise YTMusicError("Server returned HTTP 401: Unauthorized")
            return [{"videoId": "v1", "title": "T", "artists": [{"name": "A"}], "duration": "1:00"}]

    monkeypatch.setattr(music, "client", lambda: Client())
    assert [t.video_id for t in music.search("q")] == ["v1"]
