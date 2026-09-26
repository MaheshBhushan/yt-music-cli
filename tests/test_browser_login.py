"""Browser login: origin checks, page observation, capture, and the flow."""

import pytest

from ytm.authentication import browser_login, session as session_mod
from ytm.authentication.browser_login import PageObservation, parse_observation
from ytm.authentication.errors import (
    AuthExpired,
    AuthInvalidFormat,
    BrowserUnavailable,
    LoginCancelled,
    LoginTimedOut,
)
from ytm.authentication.manager import AuthManager
from ytm.authentication.session import is_music_url
from ytm.authentication.storage import SessionStore, StoredRecord


def headers(**extra):
    base = {
        "cookie": "SID=x; __Secure-3PAPISID=secret",
        "x-goog-authuser": "0",
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }
    base.update(extra)
    return base


# -- L04: origin checks are exact --------------------------------------------


@pytest.mark.parametrize("url", [
    "https://music.youtube.com",
    "https://music.youtube.com/watch?v=x",
    "https://music.youtube.com:443/browse",
])
def test_music_urls_pass(url):
    assert is_music_url(url)


@pytest.mark.parametrize("url", [
    "https://music.youtube.com.evil.example/",
    "https://music-youtube.com/",
    "https://notmusic.youtube.com/",
    "http://music.youtube.com/",
    "https://music.youtube.com:8443/",
    "https://user@music.youtube.com/",
    "https://accounts.google.com/",
    "javascript:alert(1)",
    "",
    None,
])
def test_lookalikes_and_other_origins_fail(url):
    assert not is_music_url(url)


# -- missing dependencies are reported, not raised ----------------------------


def test_missing_playwright_reports_browser_unavailable(monkeypatch):
    """find_spec on a dotted name imports the parent and raises when the
    parent is missing; the observer must check the top-level package first."""
    def fake_find_spec(name):
        if "." in name:
            raise ModuleNotFoundError(f"No module named '{name.split('.')[0]}'")
        return None

    monkeypatch.setattr(browser_login.importlib.util, "find_spec", fake_find_spec)
    with pytest.raises(BrowserUnavailable, match=r'pip install "ytm\[login\]"'):
        browser_login.PlaywrightBrowser().observe(timeout=1)


def test_a_playwright_without_the_sync_api_is_reported_clearly(monkeypatch):
    import sys
    import types

    monkeypatch.setattr(browser_login.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.delitem(sys.modules, "playwright.sync_api", raising=False)
    with pytest.raises(BrowserUnavailable, match="sync API"):
        browser_login.PlaywrightBrowser().observe(timeout=1)


# -- observation parsing ------------------------------------------------------


@pytest.mark.parametrize("raw", [
    None,
    "not a dict",
    {"loggedIn": False, "authUser": "0"},
    {"loggedIn": "true", "authUser": "0"},
    {"loggedIn": True},
    {"loggedIn": True, "authUser": None},
    {"loggedIn": True, "authUser": 0},          # a number is not the website's string
    {"loggedIn": True, "authUser": "not-numeric"},
])
def test_an_unready_page_keeps_waiting(raw):
    assert parse_observation(raw) is None


def test_a_ready_page_yields_the_allowlisted_values():
    observation = parse_observation({
        "loggedIn": True,
        "authUser": "2",
        "delegatedSessionId": "brand",
        "visitorData": "visitor",
        "extra": "not returned",
    })
    assert observation == PageObservation(auth_user="2", delegated_session_id="brand", visitor_data="visitor")


def test_wait_for_capture_times_out_at_the_deadline():
    now = [0.0]

    def collect():
        return None

    with pytest.raises(LoginTimedOut):
        browser_login.wait_for_capture(
            collect, deadline=5.0, now=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)
        )


def test_wait_for_capture_returns_as_soon_as_there_is_a_session():
    session = session_mod.build_session(headers())
    assert browser_login.wait_for_capture(lambda: session, deadline=1.0, now=lambda: 0.0) is session


# -- browser launch fallback --------------------------------------------------


class RecordingLauncher:
    def __init__(self, ok_channel=None, ok_default=False):
        self.ok_channel = ok_channel
        self.ok_default = ok_default
        self.attempts = []

    def launch(self, **options):
        self.attempts.append(options)
        channel = options.get("channel")
        if channel == self.ok_channel or (channel is None and self.ok_default):
            return "a-browser"
        raise RuntimeError("Executable doesn't exist")


def test_the_bundled_chromium_is_used_first():
    launcher = RecordingLauncher(ok_default=True)
    assert browser_login.PlaywrightBrowser()._launch(launcher, {"headless": False}) == "a-browser"
    assert launcher.attempts == [{"headless": False}]


def test_an_installed_chrome_is_used_before_asking_for_a_download():
    launcher = RecordingLauncher(ok_channel="chrome")
    assert browser_login.PlaywrightBrowser()._launch(launcher, {"headless": False}) == "a-browser"
    assert launcher.attempts == [
        {"headless": False},
        {"headless": False, "channel": "chrome"},
    ]


def test_edge_is_the_last_installed_browser_tried():
    launcher = RecordingLauncher(ok_channel="msedge")
    assert browser_login.PlaywrightBrowser()._launch(launcher, {"headless": False}) == "a-browser"
    assert [attempt.get("channel") for attempt in launcher.attempts] == [None, "chrome", "msedge"]


def test_an_explicit_channel_is_never_replaced():
    launcher = RecordingLauncher(ok_channel="chrome")
    with pytest.raises(BrowserUnavailable, match="ytm login --install-browser"):
        browser_login.PlaywrightBrowser(channel="chrome-dev")._launch(launcher, {"headless": False})
    assert launcher.attempts == [{"headless": False, "channel": "chrome-dev"}]


# -- the Playwright observer's capture logic ---------------------------------


class FakePage:
    def __init__(self, url, probe):
        self.url = url
        self._probe = probe

    def evaluate(self, script):
        if isinstance(self._probe, Exception):
            raise self._probe
        return self._probe


class FakeContext:
    def __init__(self, cookies=None):
        self._cookies = cookies or []
        self.pages = []

    def cookies(self, urls):
        return list(self._cookies)


def test_the_probe_skips_foreign_pages_and_dead_contexts():
    browser = browser_login.PlaywrightBrowser()
    assert browser._probe(FakePage("https://accounts.google.com/", {"loggedIn": True})) is None
    assert browser._probe(FakePage("https://music.youtube.com/", RuntimeError("navigation"))) is None
    ready = browser._probe(FakePage("https://music.youtube.com/", {
        "loggedIn": True, "authUser": "0",
    }))
    assert ready == PageObservation(auth_user="0")


def test_a_captured_music_request_header_wins_and_is_reduced():
    browser = browser_login.PlaywrightBrowser()
    context = FakeContext(cookies=[])
    captured = [
        {"cookie": "stale-cookie", "x-goog-authuser": "9"},  # older request
        {
            "cookie": "SID=1; __Secure-3PAPISID=secret",
            "authorization": "SAPISIDHASH 123_abc",
            "x-goog-authuser": "2",
            "x-goog-pageid": "brand",
            "host": "music.youtube.com",
            "content-length": "42",
            "sec-fetch-site": "same-origin",
            "x-client-data": "junk",
        },
    ]
    observation = PageObservation(auth_user="2", delegated_session_id="brand", visitor_data="vis")
    session = browser._candidate(context, observation, captured)
    assert session.headers["cookie"].endswith("__Secure-3PAPISID=secret")
    assert session.headers["authorization"] == "SAPISIDHASH 123_abc"
    assert session.headers["x-goog-authuser"] == "2"      # observed index wins
    assert session.headers["origin"] == session_mod.MUSIC_ORIGIN
    assert session.headers["x-goog-visitor-id"] == "vis"
    assert session.user == "brand"
    assert set(session.headers) <= session_mod.ALLOWED_HEADERS
    assert "host" not in session.headers and "x-client-data" not in session.headers


def test_a_cookie_header_falls_back_to_the_context_cookie_api():
    browser = browser_login.PlaywrightBrowser()
    context = FakeContext(cookies=[
        {"name": "SID", "value": "1"},
        {"name": "__Secure-3PAPISID", "value": "secret"},
        {"name": "VISITOR_INFO1_LIVE", "value": "v"},
    ])
    session = browser._candidate(context, PageObservation(auth_user="0"), captured=[])
    assert session.headers["cookie"] == "SID=1; __Secure-3PAPISID=secret; VISITOR_INFO1_LIVE=v"
    assert session.headers["authorization"] == session_mod.AUTHORIZATION_MARKER  # cookie-only capture


def test_the_cookie_api_fallback_refuses_a_session_without_the_signing_cookie():
    browser = browser_login.PlaywrightBrowser()
    context = FakeContext(cookies=[{"name": "SID", "value": "1"}])
    assert browser._candidate(context, PageObservation(auth_user="0"), captured=[]) is None


def test_the_cookie_api_fallback_refuses_ambiguous_duplicate_names():
    browser = browser_login.PlaywrightBrowser()
    context = FakeContext(cookies=[
        {"name": "__Secure-3PAPISID", "value": "one"},
        {"name": "__Secure-3PAPISID", "value": "two"},
    ])
    with pytest.raises(AuthInvalidFormat, match="ambiguous cookies"):
        browser._candidate(context, PageObservation(auth_user="0"), captured=[])


def test_a_mangled_captured_cookie_falls_back_to_the_context_api():
    browser = browser_login.PlaywrightBrowser()
    context = FakeContext(cookies=[{"name": "__Secure-3PAPISID", "value": "secret"}])
    captured = [{"cookie": "SID=1; __Secure-3PAPISID=sec\nret"}]
    session = browser._candidate(context, PageObservation(auth_user="0"), captured)
    assert session.headers["cookie"] == "__Secure-3PAPISID=secret"


# -- the login flow -----------------------------------------------------------


class FakeBrowser:
    def __init__(self, session=None, error=None):
        self.session = session
        self.error = error

    def observe(self, *, timeout):
        assert timeout == 600
        if self.error is not None:
            raise self.error
        return self.session


class ProbeClient:
    def __init__(self, account=None, error=None):
        self.account = account
        self.error = error

    def get_account_info(self):
        if self.error is not None:
            raise self.error
        return self.account if self.account is not None else {"accountName": "Example Listener"}


def manager_for(tmp_path, *, account=None, error=None):
    return AuthManager(
        SessionStore(tmp_path / "session.json"),
        client_factory=lambda headers, user=None: ProbeClient(account=account, error=error),
    )


def test_a_confirmed_browser_login_commits_the_verified_session(tmp_path):
    confirmed = []
    manager = manager_for(tmp_path)
    record = browser_login.interactive_login(
        manager,
        browser=FakeBrowser(session_mod.build_session(headers())),
        confirm=lambda verified: confirmed.append(verified.account_name) or True,
    )
    assert record.method == "browser"
    assert confirmed == ["Example Listener"]
    assert manager.store.load().revision == record.revision


def test_a_rejected_account_selection_commits_nothing(tmp_path):
    manager = manager_for(tmp_path)
    with pytest.raises(LoginCancelled):
        browser_login.interactive_login(
            manager,
            browser=FakeBrowser(session_mod.build_session(headers())),
            confirm=lambda verified: False,
        )
    assert not manager.store.path.exists()


def test_a_timed_out_browser_login_leaves_old_credentials_alone(tmp_path):
    manager = manager_for(tmp_path)
    first = manager.store.save(StoredRecord.browser(headers()), expected_revision=None)
    with pytest.raises(LoginTimedOut):
        browser_login.interactive_login(
            manager, browser=FakeBrowser(error=LoginTimedOut("timeout")), confirm=None
        )
    assert manager.store.load().revision == first.revision


def test_a_login_repairs_an_unreadable_record(tmp_path):
    manager = manager_for(tmp_path)
    manager.store.path.parent.mkdir(parents=True, exist_ok=True)
    manager.store.path.write_text("{broken")
    record = browser_login.interactive_login(
        manager, browser=FakeBrowser(session_mod.build_session(headers())), confirm=None
    )
    assert manager.store.load().revision == record.revision


def test_logout_repairs_an_unreadable_record(tmp_path):
    manager = manager_for(tmp_path)
    manager.store.path.parent.mkdir(parents=True, exist_ok=True)
    manager.store.path.write_text("{broken")
    result = manager.logout()
    assert result.revision
    assert manager.store.load().method == "none"


def test_a_probe_rejection_preserves_the_old_session_byte_for_byte(tmp_path):
    from ytmusicapi.exceptions import YTMusicServerError

    manager = manager_for(tmp_path, error=YTMusicServerError("Server returned HTTP 401: no"))
    old = manager.store.save(
        StoredRecord.browser(headers(cookie="SID=old; __Secure-3PAPISID=old")),
        expected_revision=None,
    )
    before = manager.store.path.read_bytes()
    with pytest.raises(AuthExpired):
        browser_login.interactive_login(
            manager,
            browser=FakeBrowser(session_mod.build_session(headers(cookie="SID=new; __Secure-3PAPISID=new"))),
            confirm=None,
        )
    assert manager.store.path.read_bytes() == before
    assert manager.store.load().revision == old.revision


def test_a_login_finishing_after_a_logout_cannot_resurrect_a_session(tmp_path):
    from ytm.authentication.errors import AuthStorageError

    manager = manager_for(tmp_path)
    manager.store.save(StoredRecord.browser(headers()), expected_revision=None)
    session = session_mod.build_session(headers(cookie="SID=new; __Secure-3PAPISID=new"))

    class LogsOutMidLogin:
        def observe(self, *, timeout):
            # the race: a logout lands after the expected revision was taken
            record = manager.store.load()
            manager.store.logout(expected_revision=record.revision if record else None)
            return session

    with pytest.raises(AuthStorageError, match="changed while this login"):
        browser_login.interactive_login(manager, browser=LogsOutMidLogin(), confirm=None)
    assert manager.store.load().method == "none"
