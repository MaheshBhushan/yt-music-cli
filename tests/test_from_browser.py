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


def test_error_names_the_reason_per_browser(tmp_path, monkeypatch):
    """Windows Chrome: yt-dlp reads the database but cannot decrypt a single
    cookie (App-Bound Encryption). The error must say so, not "not logged in"."""
    path = tmp_path / "auth.json"

    def fake_extract(name, logger=None):
        if name == "chrome":
            logger.info("Extracted 0 cookies from chrome (312 could not be decrypted)")
            return _jar()
        if name == "edge":
            raise FileNotFoundError('could not find edge cookies database in "C:\\\\Users\\\\m"')
        logger.info("Extracted 40 cookies from firefox")
        return _jar(_FakeCookie("other", "x"))

    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    monkeypatch.setattr(auth, "_AUTODETECT_BROWSERS", ("chrome", "edge", "firefox"))
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser(None, path=path, client_factory=_fake_client_ok)
    message = str(excinfo.value)
    assert "chrome: 312 cookies could not be decrypted" in message
    assert "edge: not installed or no profile found" in message
    assert "firefox: no YouTube login" in message
    assert "sid" not in message.lower()  # never a cookie value


def test_windows_chromium_decrypt_failure_gets_the_app_bound_hint(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth.sys, "platform", "win32")

    def fake_extract(name, logger=None):
        logger.info(f"Extracted 0 cookies from {name} (12 could not be decrypted)")
        return _jar()

    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_ok)
    message = str(excinfo.value)
    assert "App-Bound Encryption" in message
    assert "--from-browser firefox" in message and "--manual" in message and "--oauth" in message


def test_no_windows_hint_on_linux_or_without_decrypt_failures(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, logger=None: _jar())
    monkeypatch.setattr(auth.sys, "platform", "linux")
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_ok)
    assert "App-Bound" not in str(excinfo.value)
    monkeypatch.setattr(auth.sys, "platform", "win32")
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_ok)
    assert "App-Bound" not in str(excinfo.value)  # plain "no login" is not an encryption problem


def test_locked_database_reason(tmp_path, monkeypatch):
    def fake_extract(name, logger=None):
        raise Exception("sqlite3.OperationalError: database is locked")
    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("brave", path=tmp_path / "a.json", client_factory=_fake_client_ok)
    assert "brave: cookie database locked; close the browser and retry" in str(excinfo.value)


def test_quiet_logger_counts_decrypt_failures():
    logger = auth._QuietLogger()
    assert logger.decrypt_failures() == 0
    logger.info("Extracted 3 cookies from chrome (7 could not be decrypted)")
    assert logger.decrypt_failures() == 7
