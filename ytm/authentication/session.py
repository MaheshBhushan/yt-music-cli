"""Captured session models and the checks they must pass before storage.

A browser session is a small allowlisted set of request headers, never a
page dump. The only signing secret kept is the website cookie itself:
ytmusicapi derives a fresh ``SAPISIDHASH`` from it per request, and the
stored authorization value (when one was captured) is a marker that the
library replaces before anything is sent.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from ytm.authentication.errors import AuthInvalidFormat

#: the one origin a captured session may come from
MUSIC_ORIGIN = "https://music.youtube.com"
MUSIC_HOST = "music.youtube.com"

#: the signing cookie the pinned ytmusicapi reads (and only this one)
SIGNING_COOKIE = "__Secure-3PAPISID"

#: cookie names that look like a signing secret but are not the one the
#: pinned ytmusicapi will accept
_UNSUPPORTED_SIGNING_COOKIES = ("SAPISID", "__Secure-1PAPISID")

#: exactly the request headers a browser session may keep. Everything else
#: captured along the way (host, content-length, sec-*, request ids, ...) is
#: dropped, not stored and never replayed.
ALLOWED_HEADERS = frozenset({
    "cookie",
    "authorization",
    "origin",
    "x-origin",
    "x-goog-authuser",
    "user-agent",
    "accept",
    "content-type",
    "x-goog-visitor-id",
    "x-goog-pageid",
})

#: a browser-mode marker: ytmusicapi only needs `authorization` to contain
#: SAPISIDHASH to treat the headers as browser auth, then regenerates the
#: real value from the cookie on every request. Stored so cookie-only
#: captures are recognisable; never sent as-is.
AUTHORIZATION_MARKER = "SAPISIDHASH 0_0"

_FORBIDDEN_IN_VALUE = ("\r", "\n", "\x00")


@dataclass(frozen=True)
class BrowserSession:
    """Captured request headers plus the account-selection hints.

    ``headers`` is excluded from ``repr``: the cookie and authorization are
    durable secrets. The build_session helper copies the mapping; callers must
    hand ytmusicapi a fresh copy -- it mutates its header state.
    """

    headers: dict = field(repr=False)
    user: str | None = None
    source: str = "interactive_browser"


@dataclass(frozen=True)
class VerifiedSession:
    """A candidate that answered a real account read, with its display name."""

    session: BrowserSession = field(repr=False)
    account_name: str | None = None
    verified_at: str = ""


def is_music_url(url):
    """Whether `url` is exactly the YouTube Music HTTPS origin.

    Scheme, host and port must match precisely: a lookalike host, an http
    URL, a nonstandard port, or an embedded userinfo all fail.
    """
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == MUSIC_HOST
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
    )


def normalize_headers(raw):
    """`raw` reduced to the header allowlist, lowercased, with junk dropped.

    Non-string keys and values, newline/control characters, and headers that
    are not on the allowlist never survive. Unrelated headers are simply
    ignored; a mangled required header fails validation afterwards with a
    message about that header.
    """
    if not isinstance(raw, dict):
        raise AuthInvalidFormat("Browser headers must be an object.")
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        name = key.strip().lower()
        if name not in ALLOWED_HEADERS:
            continue
        value = value.strip()
        if not value or any(char in value for char in _FORBIDDEN_IN_VALUE):
            continue
        result[name] = value
    return result


def cookie_value(cookie_header, name):
    """The value of cookie `name` in a Cookie header, or None.

    Values are split on the first ``=`` only, so base64-style values that
    contain ``=`` survive intact; the name must match exactly.
    """
    for pair in (cookie_header or "").split(";"):
        key, separator, value = pair.strip().partition("=")
        if separator and key == name:
            return value
    return None


def cookies_to_header(cookies):
    """A Cookie header from browser cookie records for one URL.

    Duplicate names with different values are ambiguous: rather than order
    them arbitrarily, refuse and let the caller prefer a real request
    header (which is the browser's own answer).
    """
    values = {}
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not name or any(c in name for c in "=;\r\n\t") or any(c in value for c in ";\r\n\x00"):
            raise AuthInvalidFormat("The browser returned a malformed cookie.")
        if name in values and values[name] != value:
            raise AuthInvalidFormat(
                "The browser holds ambiguous cookies for YouTube Music; "
                "refusing to guess which session is meant."
            )
        values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def build_session(raw_headers, *, user=None, source="interactive_browser"):
    """Validate raw captured headers into a BrowserSession, or raise.

    Requires the pinned ytmusicapi's signing cookie, an account index, and
    the exact Music origin; adds the browser-mode authorization marker when
    no captured authorization survived. Never invents a signing cookie from
    a different one.
    """
    if user is not None and (not isinstance(user, str) or not user or any(ord(c) < 32 for c in user)):
        raise AuthInvalidFormat("The selected account identity is invalid.")
    headers = normalize_headers(raw_headers)
    cookie_header = headers.get("cookie")
    if not cookie_header:
        raise AuthInvalidFormat("The captured browser session carried no cookies.")
    if not cookie_value(cookie_header, SIGNING_COOKIE):
        for alternative in _UNSUPPORTED_SIGNING_COOKIES:
            if cookie_value(cookie_header, alternative):
                raise AuthInvalidFormat(
                    f"The captured session has {alternative} but not {SIGNING_COOKIE}, "
                    "which this ytmusicapi version requires. Run 'ytm login' again; "
                    "if it keeps happening, report it."
                )
        raise AuthInvalidFormat(
            f"The captured session has no {SIGNING_COOKIE} cookie; "
            "it is not a signed-in YouTube Music session."
        )
    authuser = headers.get("x-goog-authuser")
    if authuser is None or not authuser.isascii() or not authuser.isdecimal():
        raise AuthInvalidFormat(
            "The captured session did not identify a Google account index "
            "(x-goog-authuser); select an account in YouTube Music and retry."
        )
    headers["origin"] = MUSIC_ORIGIN
    headers.pop("x-origin", None)
    if "SAPISIDHASH" not in headers.get("authorization", ""):
        headers["authorization"] = AUTHORIZATION_MARKER
    headers.setdefault("content-type", "application/json")
    headers.setdefault("accept", "*/*")
    return BrowserSession(headers=headers, user=user or None, source=source)


def validate_session(session):
    """Check a session read back from storage; raises AuthInvalidFormat."""
    if not isinstance(session, BrowserSession):
        raise AuthInvalidFormat("The stored session has the wrong shape.")
    return build_session(dict(session.headers), user=session.user, source=session.source)


def safe_text(value, limit=80):
    """`value` reduced to a short, control-character-free display string.

    Account names come from the provider and end up on a terminal; escape
    sequences have no business there.
    """
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char if char.isprintable() else " " for char in value)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]
