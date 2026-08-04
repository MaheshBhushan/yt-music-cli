"""Tests for `ytm auth --from-browser` (T15): cookie extraction from a local browser."""
import json

import pytest

from ytm import auth


class _FakeCookie:
    def __init__(self, name, value, domain="youtube.com"):
        self.name = name
        self.value = value
        self.domain = domain


def _jar(*cookies):
    return list(cookies)


def _fake_client_ok(path):
    class _Client:
        def search(self, query, limit=1):
            return [{"title": "ok"}]

    return _Client()


def _fake_client_fails(path):
    class _Client:
        def search(self, query, limit=1):
            raise auth.YTMusicError("Server returned HTTP 401")

    return _Client()


def test_cookie_header_built_from_jar_with_authuser():
    jar = _jar(
        _FakeCookie("SID", "sid-value"),
        _FakeCookie("__Secure-3PAPISID", "papisid-value"),
        _FakeCookie("unrelated", "nope", domain="example.com"),
    )
    header = auth._cookie_header_from_jar(jar)
    assert "SID=sid-value" in header
    assert "__Secure-3PAPISID=papisid-value" in header
    assert "unrelated" not in header


def test_from_browser_writes_headers_with_authuser_and_mode_0600(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    jar = _jar(
        _FakeCookie("SID", "sid-value"),
        _FakeCookie("__Secure-3PAPISID", "papisid-value"),
    )
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, logger=None: jar)

    auth.from_browser("chrome", path=path, client_factory=_fake_client_ok)

    assert path.stat().st_mode & 0o777 == 0o600
    headers = json.loads(path.read_text())
    assert headers["x-goog-authuser"] == "0"
    assert "SID=sid-value" in headers["cookie"]
    assert "__Secure-3PAPISID=papisid-value" in headers["cookie"]


def test_from_browser_autodetect_skips_browser_with_no_youtube_cookies(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    logged_in_jar = _jar(_FakeCookie("__Secure-3PAPISID", "papisid-value"))

    def fake_extract(name, logger=None):
        if name == "chrome":
            return _jar()  # no cookies at all: not logged in
        if name == "chromium":
            return logged_in_jar
        raise AssertionError(f"should not try {name}")

    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    monkeypatch.setattr(auth, "_AUTODETECT_BROWSERS", ("chrome", "chromium"))

    auth.from_browser(None, path=path, client_factory=_fake_client_ok)

    headers = json.loads(path.read_text())
    assert "__Secure-3PAPISID=papisid-value" in headers["cookie"]


def test_from_browser_no_session_anywhere_raises_actionable_error(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, logger=None: _jar())
    monkeypatch.setattr(auth, "_AUTODETECT_BROWSERS", ("chrome", "firefox"))

    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser(None, path=path, client_factory=_fake_client_ok)

    message = str(excinfo.value)
    assert "chrome" in message
    assert "firefox" in message
    assert "music.youtube.com" in message
    assert not path.exists()


def test_from_browser_validation_failure_does_not_leave_broken_auth_file(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    jar = _jar(_FakeCookie("__Secure-3PAPISID", "papisid-value"))
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, logger=None: jar)

    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_fails)

    assert not path.exists()
    assert "did not authenticate" in str(excinfo.value)
