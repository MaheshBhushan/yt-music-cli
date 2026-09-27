"""Interactive browser login: observe a real YouTube Music page.

The browser is a real, visible one with a real address bar, launched in an
isolated context that ytm owns; the person signs in through Google's normal
pages. ytm never supplies or reads a password, never intercepts keyboard or
form input, and never serialises the DOM: it only asks the page for the
small logged-in configuration and, when a request is available, reduces an
authenticated Music request's headers to an allowlist.

Playwright is imported on first use, so public commands never pay for it.
"""

from __future__ import annotations

import contextlib
from collections import deque
from urllib.parse import urlsplit
import importlib.util
import time
from dataclasses import dataclass

from ytm.authentication import session as session_mod
from ytm.authentication.errors import (
    BrowserUnavailable,
    LoginCancelled,
    LoginTimedOut,
)
from ytm.authentication.session import MUSIC_ORIGIN

DEFAULT_TIMEOUT = 600
POLL_INTERVAL = 0.5
MUSIC_API_PREFIX = MUSIC_ORIGIN + "/youtubei/v1/"

#: observation only: reads the live page config. No form fields, no DOM.
PROBE_JS = """() => {
  if (location.origin !== "https://music.youtube.com") return null;
  const cfg = window.ytcfg;
  if (!cfg || typeof cfg.get !== "function") return null;
  const index = cfg.get("SESSION_INDEX");
  return {
    loggedIn: cfg.get("LOGGED_IN") === true,
    authUser: index == null ? null : String(index),
    delegatedSessionId: cfg.get("DELEGATED_SESSION_ID") || null,
    visitorData: cfg.get("VISITOR_DATA") || null
  };
}"""


@dataclass(frozen=True)
class PageObservation:
    """The allowlisted values a ready Music page exposed."""

    auth_user: str
    delegated_session_id: str | None = None
    visitor_data: str | None = None


def parse_observation(raw):
    """A probe result as PageObservation, or None when the page is not ready.

    Missing ytcfg, a signed-out page, or a non-numeric account index all
    mean "keep waiting", never "use a default".
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("loggedIn") is not True:
        return None
    auth_user = raw.get("authUser")
    if not isinstance(auth_user, str) or not auth_user.isascii() or not auth_user.isdecimal():
        return None
    delegated = raw.get("delegatedSessionId")
    visitor = raw.get("visitorData")
    return PageObservation(
        auth_user=auth_user,
        delegated_session_id=delegated if isinstance(delegated, str) and delegated else None,
        visitor_data=visitor if isinstance(visitor, str) and visitor else None,
    )


def wait_for_capture(
    collect, *, deadline, now=time.monotonic, sleep=time.sleep, poll_interval=POLL_INTERVAL
):
    """Poll `collect` until it returns a BrowserSession.

    `collect` returns None while the page is not ready; a closed browser is
    reported through LoginCancelled by the caller's own error mapping.
    """
    while True:
        if now() >= deadline:
            raise LoginTimedOut(
                "Timed out waiting for YouTube Music sign-in; existing credentials were kept."
            )
        session = collect()
        if session is not None:
            if now() >= deadline:
                raise LoginTimedOut("Timed out waiting for YouTube Music sign-in.")
            return session
        sleep(min(poll_interval, max(0, deadline - now())))


class PlaywrightBrowser:
    """The real observer, on headed Playwright in a context ytm owns.

    Never launches against the user's ordinary browser profile and never
    exposes a debugging port; the context is ephemeral and closed in
    ``finally`` (closing it is not a server-side Google logout).
    """

    def __init__(self, *, channel=None, engine="chromium", poll_interval=POLL_INTERVAL):
        if channel in ("firefox", "webkit", "chromium"):
            engine, channel = channel, None
        if channel == "edge":
            channel = "msedge"
        if engine not in ("chromium", "firefox", "webkit"):
            raise BrowserUnavailable("Unsupported Playwright browser engine.")
        if channel and channel not in ("chrome", "chrome-beta", "chrome-dev", "chrome-canary", "msedge", "msedge-beta", "msedge-dev", "msedge-canary"):
            raise BrowserUnavailable("Unsupported Playwright channel; use chrome, edge, chromium, firefox or webkit.")
        self.channel = channel
        self.engine = engine
        self.poll_interval = poll_interval

    def observe(self, *, timeout):
        # find_spec on a dotted name imports the parent, which raises when
        # playwright itself is missing; check the top-level package first.
        if importlib.util.find_spec("playwright") is None:
            raise BrowserUnavailable(
                "Interactive login needs the Playwright package, which is not installed. "
                "Install it with 'pip install \"ytm[login]\"' (or 'pip install playwright'), "
                "then run 'ytm login --install-browser' for the browser binary; or import "
                "an existing session with 'ytm login --from-browser NAME'."
            )
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "The Playwright package is installed but its sync API is missing; "
                "reinstall it with 'pip install -U playwright'."
            ) from exc

        deadline = time.monotonic() + timeout
        with sync_playwright() as playwright:
            launcher = getattr(playwright, self.engine)
            browser = self._launch(launcher, {"headless": False, "timeout": max(1, (deadline - time.monotonic()) * 1000)})
            try:
                return self._observe_context(browser, deadline=deadline)
            except PlaywrightError as exc:
                if time.monotonic() >= deadline:
                    raise LoginTimedOut("Timed out waiting for the login browser; existing credentials were kept.") from exc
                if "closed" in str(exc).lower():
                    raise LoginCancelled(
                        "The login browser was closed; existing credentials were kept."
                    ) from exc
                raise BrowserUnavailable(
                    "The login browser stopped unexpectedly; existing credentials were kept."
                ) from exc
            finally:
                with contextlib.suppress(Exception):
                    browser.close()

    def _launch(self, launcher, options):
        """Launch the requested channel, else bundled Chromium, else Chrome/Edge.

        An explicit ``--browser`` is a choice, so a failure there is final.
        Without one, an installed Chrome (then Edge) is tried before asking
        the user to download Playwright's Chromium: it is the same engine,
        already on the machine, and the context stays Playwright-managed.
        """
        if self.channel:
            attempts = [{"channel": self.channel}]
        elif self.engine == "chromium":
            attempts = [{}, {"channel": "chrome"}, {"channel": "msedge"}]
        else:
            attempts = [{}]
        last_error = None
        deadline = time.monotonic() + options["timeout"] / 1000 if "timeout" in options else None
        for extra in attempts:
            try:
                current = {**options, **extra}
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    current["timeout"] = remaining * 1000
                return launcher.launch(**current)
            except Exception as exc:
                last_error = exc
        raise BrowserUnavailable(
            "Could not launch a browser for login (tried "
            + (self.channel or ("bundled Chromium, Google Chrome and Microsoft Edge" if self.engine == "chromium" else self.engine))
            + "). Install one with 'ytm login --install-browser', or import an "
            "existing session with 'ytm login --from-browser NAME'."
        ) from last_error

    def _observe_context(self, browser, *, deadline):
        context = browser.new_context(accept_downloads=False)
        try:
            context.set_default_timeout(min(5000, max(1, (deadline - time.monotonic()) * 1000)))
            page = context.new_page()
            captured = deque(maxlen=8)

            def remember(request):
                if not session_mod.is_music_url(request.url):
                    return
                if request.method != "POST" or urlsplit(request.url).path not in (
                    "/youtubei/v1/browse", "/youtubei/v1/account/account_menu"
                ):
                    return  # never inspect a Google credential submission
                try:
                    headers = session_mod.normalize_headers(request.all_headers())
                except Exception:
                    return
                if "SAPISIDHASH" in headers.get("authorization", ""):
                    captured.append(headers)

            context.on("request", remember)  # includes popups in the owned context
            page.goto(MUSIC_ORIGIN, wait_until="domcontentloaded",
                      timeout=max(1, min(30000, (deadline - time.monotonic()) * 1000)))

            def collect():
                if not browser.is_connected() or not context.pages:
                    raise LoginCancelled("The login browser was closed; existing credentials were kept.")
                for candidate in context.pages:
                    observation = self._probe(candidate)
                    if observation is None:
                        continue
                    result = self._candidate(context, observation, captured)
                    # An account switch during cookie/header collection invalidates the snapshot.
                    if result is not None and observation == self._probe(candidate):
                        return result
                return None

            def pump(seconds):
                # time.sleep starves Playwright's synchronous event dispatcher.
                if context.pages:
                    context.pages[0].wait_for_timeout(seconds * 1000)

            return wait_for_capture(collect, deadline=deadline, sleep=pump,
                                    poll_interval=self.poll_interval)
        finally:
            with contextlib.suppress(Exception):
                context.close()

    def _probe(self, page):
        if not session_mod.is_music_url(page.url):
            return None
        try:
            return parse_observation(page.evaluate(PROBE_JS))
        except Exception:
            return None  # navigating or the context is going away: not ready

    def _candidate(self, context, observation, captured):
        current = session_mod.cookies_to_header(context.cookies(MUSIC_API_PREFIX + "browse"))
        base = {}
        for raw in reversed(captured):
            headers = session_mod.normalize_headers(raw)
            if headers.get("x-goog-authuser") != observation.auth_user:
                continue
            if (headers.get("x-goog-pageid") or None) != observation.delegated_session_id:
                continue
            if current and session_mod.cookie_value(headers.get("cookie"), session_mod.SIGNING_COOKIE) != session_mod.cookie_value(current, session_mod.SIGNING_COOKIE):
                continue
            base = headers
            break
        if current:
            base["cookie"] = current
        if not base.get("cookie"):
            return None
        base["x-goog-authuser"] = observation.auth_user
        base["origin"] = MUSIC_ORIGIN
        if observation.visitor_data:
            base["x-goog-visitor-id"] = observation.visitor_data
        if observation.delegated_session_id:
            base["x-goog-pageid"] = observation.delegated_session_id
        else:
            base.pop("x-goog-pageid", None)
        # Cookies can lag the signed-in page during redirects.
        if not session_mod.cookie_value(base["cookie"], session_mod.SIGNING_COOKIE):
            if not any(session_mod.cookie_value(base["cookie"], name) for name in ("SAPISID", "__Secure-1PAPISID")):
                return None
        return session_mod.build_session(base, user=observation.delegated_session_id,
                                         source="interactive_browser")


def interactive_login(manager, *, browser=None, timeout=DEFAULT_TIMEOUT, confirm=None):
    """Observe a browser session, verify it, and commit it if confirmed.

    The expected revision is taken before the browser opens, so a logout or
    another login that happens meanwhile makes the commit fail rather than
    resurrect a session. Nothing is written before validation; a failure
    leaves the previous credentials exactly as they were.
    """
    expected_revision = manager.expected_revision()
    runner = browser if browser is not None else PlaywrightBrowser()
    session = runner.observe(timeout=timeout)
    from ytm.authentication import diagnostics
    diagnostics.event("validating")
    verified = manager.validate_candidate(session)
    diagnostics.event("validated")
    if confirm is not None and not confirm(verified):
        raise LoginCancelled("Login cancelled; the previous credentials were kept.")
    return manager.save_verified(verified, expected_revision=expected_revision)
