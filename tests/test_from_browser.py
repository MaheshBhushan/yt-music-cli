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


def _config(authuser="0"):
    return {"auth": {"x-goog-authuser": authuser}}


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
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, profile=None, logger=None: jar)

    auth.from_browser("chrome", path=path, client_factory=_fake_client_ok, config=_config())

    assert path.stat().st_mode & 0o777 == 0o600
    headers = json.loads(path.read_text())
    assert headers["x-goog-authuser"] == "0"
    assert "SID=sid-value" in headers["cookie"]
    assert "__Secure-3PAPISID=papisid-value" in headers["cookie"]


def test_from_browser_uses_configured_authuser(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    jar = _jar(
        _FakeCookie("SID", "sid-value"),
        _FakeCookie("__Secure-3PAPISID", "papisid-value"),
    )
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, profile=None, logger=None: jar)

    auth.from_browser("chrome", path=path, client_factory=_fake_client_ok, config=_config("2"))

    headers = json.loads(path.read_text())
    assert headers["x-goog-authuser"] == "2"


def test_from_browser_autodetect_skips_browser_with_no_youtube_cookies(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    logged_in_jar = _jar(_FakeCookie("__Secure-3PAPISID", "papisid-value"))

    def fake_extract(name, profile=None, logger=None):
        if name == "chrome":
            return _jar()  # no cookies at all: not logged in
        if name == "chromium":
            return logged_in_jar
        raise AssertionError(f"should not try {name}")

    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    monkeypatch.setattr(auth, "_AUTODETECT_BROWSERS", ("chrome", "chromium"))

    auth.from_browser(None, path=path, client_factory=_fake_client_ok, config=_config())

    headers = json.loads(path.read_text())
    assert "__Secure-3PAPISID=papisid-value" in headers["cookie"]


def test_from_browser_no_session_anywhere_raises_actionable_error(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, profile=None, logger=None: _jar())
    monkeypatch.setattr(auth, "_AUTODETECT_BROWSERS", ("chrome", "firefox"))

    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser(None, path=path, client_factory=_fake_client_ok, config=_config())

    message = str(excinfo.value)
    assert "chrome" in message
    assert "firefox" in message
    assert "music.youtube.com" in message
    assert not path.exists()


def test_from_browser_validation_failure_does_not_leave_broken_auth_file(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    jar = _jar(_FakeCookie("__Secure-3PAPISID", "papisid-value"))
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, profile=None, logger=None: jar)

    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_fails, config=_config())

    assert not path.exists()
    assert "did not authenticate" in str(excinfo.value)


def test_error_names_the_reason_per_browser(tmp_path, monkeypatch):
    """Windows Chrome: yt-dlp reads the database but cannot decrypt a single
    cookie (App-Bound Encryption). The error must say so, not "not logged in"."""
    path = tmp_path / "auth.json"

    def fake_extract(name, profile=None, logger=None):
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
        auth.from_browser(None, path=path, client_factory=_fake_client_ok, config=_config())
    message = str(excinfo.value)
    assert "chrome: 312 cookies could not be decrypted" in message
    assert "edge: not installed or no profile found" in message
    assert "firefox: no YouTube login" in message
    assert "sid" not in message.lower()  # never a cookie value


def test_windows_chromium_decrypt_failure_gets_the_app_bound_hint(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth.sys, "platform", "win32")

    def fake_extract(name, profile=None, logger=None):
        logger.info(f"Extracted 0 cookies from {name} (12 could not be decrypted)")
        return _jar()

    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_ok, config=_config())
    message = str(excinfo.value)
    assert "App-Bound Encryption" in message
    assert "--from-browser firefox" in message and "--manual" in message and "--oauth" in message


def test_no_windows_hint_on_linux_or_without_decrypt_failures(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "extract_cookies_from_browser", lambda name, profile=None, logger=None: _jar())
    monkeypatch.setattr(auth.sys, "platform", "linux")
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_ok, config=_config())
    assert "App-Bound" not in str(excinfo.value)
    monkeypatch.setattr(auth.sys, "platform", "win32")
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=_fake_client_ok, config=_config())
    assert "App-Bound" not in str(excinfo.value)  # plain "no login" is not an encryption problem


def test_locked_database_reason(tmp_path, monkeypatch):
    def fake_extract(name, profile=None, logger=None):
        raise Exception("sqlite3.OperationalError: database is locked")
    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("brave", path=tmp_path / "a.json", client_factory=_fake_client_ok, config=_config())
    assert "brave: cookie database locked; close the browser and retry" in str(excinfo.value)


def test_quiet_logger_counts_decrypt_failures():
    logger = auth._QuietLogger()
    assert logger.decrypt_failures() == 0
    logger.info("Extracted 3 cookies from chrome (7 could not be decrypted)")
    assert logger.decrypt_failures() == 7


# -- Chromium forks yt-dlp does not know by name (Helium) ---------------------

def test_fork_settings_per_platform(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    mac = auth._fork_settings("helium")
    assert mac["dir"].endswith("Library/Application Support/net.imput.helium")
    assert mac["keychain"] == ("Helium Storage Key", "Helium")

    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert auth._fork_settings("helium") == {"dir": "/xdg/net.imput.helium", "keyring": "Chromium"}

    monkeypatch.setattr(auth.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    assert auth._fork_settings("helium")["dir"] == r"C:\Users\me\AppData\Local\imput\Helium\User Data"

    assert auth._fork_settings("netscape") is None


def _chromium_cookie_db(path, rows):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('version', '20')")
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
        "path TEXT, expires_utc INTEGER, is_secure INTEGER)"
    )
    for host, name, value in rows:
        conn.execute("INSERT INTO cookies VALUES (?, ?, ?, X'', '/', 0, 1)", (host, name, value))
    conn.commit()
    conn.close()


def test_fork_cookies_are_read_from_the_fork_profile_dir(tmp_path, monkeypatch):
    profile = tmp_path / "net.imput.helium"
    _chromium_cookie_db(profile / "Default" / "Cookies", [
        (".youtube.com", "__Secure-3PAPISID", "papisid"),
        (".youtube.com", "SID", "sid"),
        (".example.com", "other", "x"),
    ])
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(auth, "_fork_settings", lambda name: {"dir": str(profile), "keyring": "Chromium"})

    header, reason = auth._extract_browser_cookie_header("helium")
    assert reason is None
    assert "__Secure-3PAPISID=papisid" in header and "SID=sid" in header and "other" not in header


def test_fork_without_a_profile_reports_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_fork_settings", lambda name: {"dir": str(tmp_path / "missing")})
    assert auth._extract_browser_cookie_header("helium") == (None, "not installed or no profile found")


def test_keychain_password_asks_security_for_heliums_item(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = b"secret\n"

    monkeypatch.setattr(auth.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Result())
    assert auth._keychain_password("Helium Storage Key", "Helium") == b"secret"
    assert calls == [["security", "find-generic-password", "-w", "-a", "Helium", "-s", "Helium Storage Key"]]


def test_helium_is_tried_by_autodetect_after_the_mainstream_browsers():
    assert "helium" in auth._AUTODETECT_BROWSERS
    assert auth._AUTODETECT_BROWSERS.index("helium") > auth._AUTODETECT_BROWSERS.index("chrome")


def test_fork_prefers_the_profile_with_a_login_over_the_newest_database(tmp_path, monkeypatch):
    """Two Helium profiles: the one flushed last on quit has no YouTube login,
    the older one does. yt-dlp's newest-file rule picked the wrong one (#27)."""
    import os

    profile = tmp_path / "net.imput.helium"
    logged_in = profile / "Default" / "Network" / "Cookies"
    other = profile / "Profile 1" / "Network" / "Cookies"
    system = profile / "System Profile" / "Network" / "Cookies"
    _chromium_cookie_db(logged_in, [(".youtube.com", "__Secure-3PAPISID", "papisid")])
    _chromium_cookie_db(other, [(".example.com", "other", "x")])
    _chromium_cookie_db(system, [(".youtube.com", "__Secure-3PAPISID", "system-ghost")])
    os.utime(logged_in, (1_000, 1_000))
    os.utime(other, (2_000, 2_000))
    os.utime(system, (3_000, 3_000))
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(auth, "_fork_settings", lambda name: {"dir": str(profile), "keyring": "Chromium"})

    header, reason = auth._extract_browser_cookie_header("helium")
    assert reason is None
    assert "__Secure-3PAPISID=papisid" in header


def test_fork_profile_option_selects_one_profile_directory(tmp_path, monkeypatch):
    profile = tmp_path / "net.imput.helium"
    _chromium_cookie_db(profile / "Default" / "Cookies", [(".youtube.com", "__Secure-3PAPISID", "a")])
    _chromium_cookie_db(profile / "Profile 1" / "Cookies", [(".youtube.com", "__Secure-3PAPISID", "b")])
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(auth, "_fork_settings", lambda name: {"dir": str(profile), "keyring": "Chromium"})

    header, _ = auth._extract_browser_cookie_header("helium", profile="Profile 1")
    assert "__Secure-3PAPISID=b" in header
    assert auth._extract_browser_cookie_header("helium", profile="Profile 7") == (
        None, 'profile "Profile 7" not found'
    )


def test_profile_is_passed_through_to_yt_dlp_for_known_browsers(tmp_path, monkeypatch):
    seen = {}

    def fake_extract(name, profile=None, logger=None):
        seen[name] = profile
        return _jar(_FakeCookie("__Secure-3PAPISID", "v"))

    monkeypatch.setattr(auth, "extract_cookies_from_browser", fake_extract)
    auth.from_browser("firefox", path=tmp_path / "auth.json", client_factory=_fake_client_ok, profile="abc.default")
    assert seen == {"firefox": "abc.default"}


def test_validation_network_failure_is_reported_as_network_not_login(tmp_path, monkeypatch):
    import requests

    path = tmp_path / "auth.json"
    monkeypatch.setattr(
        auth, "extract_cookies_from_browser",
        lambda name, profile=None, logger=None: _jar(_FakeCookie("__Secure-3PAPISID", "v")),
    )

    def client_hangs(path):
        raise requests.exceptions.ConnectTimeout("HTTPSConnectionPool(host='music.youtube.com')")

    with pytest.raises(auth.AuthError) as excinfo:
        auth.from_browser("chrome", path=path, client_factory=client_hangs)
    assert not path.exists()
    message = str(excinfo.value)
    assert "network problem" in message and "did not authenticate" not in message
