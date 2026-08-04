"""Authentication module."""
import json
import os
from pathlib import Path

import ytmusicapi
from yt_dlp.cookies import extract_cookies_from_browser
from ytmusicapi.exceptions import YTMusicError

AUTH_PATH = Path.home() / ".config" / "ytm" / "auth.json"

# Order in which --from-browser auto-detection tries local browser profiles.
_AUTODETECT_BROWSERS = ("chrome", "chromium", "edge", "brave", "vivaldi", "opera", "firefox")

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_EXPIRED_HINT = (
    "YouTube Music authentication is no longer valid (browser headers expire "
    "when the session is revoked or the cookie ages out). Run 'ytm auth' to "
    "paste fresh request headers."
)
_MISSING_HINT = "No YouTube Music credentials found at {path}. Run 'ytm auth' to set them up."


class _QuietLogger:
    """A yt-dlp logger that never prints anything (cookies must never be logged)."""

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message, only_once=False):
        pass

    def error(self, message):
        pass


class AuthError(Exception):
    """Base class for authentication problems."""


class AuthMissing(AuthError):
    """No credentials have been stored yet."""


class AuthExpired(AuthError):
    """Stored credentials are present but no longer accepted by YouTube Music."""


def setup(path=AUTH_PATH):
    """Run the interactive browser-header setup and store credentials at path.

    ytmusicapi 1.12.1's OAuth path needs a user-provisioned Google Cloud
    client id and secret, so browser headers are the only credentials a human
    can supply interactively. They are also the cookies stream resolution needs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    print("Copy the request headers of an authenticated POST request from")
    print("https://music.youtube.com (devtools > Network > filter '/browse' >")
    print("right click the browse request > Copy > Copy request headers).")
    try:
        headers = ytmusicapi.setup()
    except YTMusicError as exc:
        raise AuthError(str(exc)) from exc
    # os.open with mode 0600 so the file is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as file:
        file.write(headers)
    os.chmod(path, 0o600)
    return path


def _cookie_header_from_jar(jar):
    """Build a Cookie header value from a http.cookiejar-style jar of youtube.com cookies.

    Returns None if the jar has no usable logged-in YouTube session (no __Secure-3PAPISID).
    """
    pairs = [(cookie.name, cookie.value) for cookie in jar if "youtube.com" in cookie.domain]
    if not any(name == "__Secure-3PAPISID" for name, _ in pairs):
        return None
    return "; ".join(f"{name}={value}" for name, value in pairs)


def _extract_browser_cookie_header(browser_name):
    """Return a Cookie header value extracted from browser_name's profile, or None."""
    try:
        jar = extract_cookies_from_browser(browser_name, logger=_QuietLogger())
    except Exception:
        return None
    return _cookie_header_from_jar(jar)


def from_browser(browser=None, path=AUTH_PATH, client_factory=None):
    """Extract YouTube cookies from a local browser profile and store credentials at path.

    If browser is None, tries each of _AUTODETECT_BROWSERS in turn and uses the first
    that yields a logged-in YouTube cookie set. Validates the extracted credentials with
    a live call before leaving the auth file in place; on failure the file is removed
    and AuthError is raised so a dead auth file is never left behind silently.
    """
    candidates = [browser] if browser else list(_AUTODETECT_BROWSERS)
    cookie_header = None
    for name in candidates:
        cookie_header = _extract_browser_cookie_header(name)
        if cookie_header:
            break
    if cookie_header is None:
        raise AuthError(
            "No logged-in YouTube session found in "
            + ", ".join(candidates)
            + ". Log in at https://music.youtube.com in one of these browsers first, "
            "then run 'ytm auth --from-browser' again."
        )

    headers = {
        "cookie": cookie_header,
        "x-goog-authuser": "0",
        "user-agent": _USER_AGENT,
        # Placeholder so ytmusicapi recognises this as browser auth; it is
        # regenerated from the cookie's SAPISID on every request.
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as file:
        json.dump(headers, file)
    os.chmod(path, 0o600)

    make_client = client_factory or client
    try:
        make_client(path).search("test", limit=1)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise AuthError(
            "Extracted browser cookies were written but did not authenticate "
            "successfully; no auth file was left behind. Make sure you are logged "
            "in at https://music.youtube.com and try again. "
            f"Underlying error: {exc}"
        ) from exc
    return path


def load_headers(path=AUTH_PATH):
    """Return the stored request headers.

    Raises AuthMissing if no usable credentials have been stored.
    """
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError) as exc:
        raise AuthMissing(_MISSING_HINT.format(path=path)) from exc


def load_cookies(path=AUTH_PATH):
    """Return the stored Cookie header value, for reuse by stream resolution."""
    headers = load_headers(path)
    for key, value in headers.items():
        if key.lower() == "cookie":
            return value
    raise AuthMissing(_MISSING_HINT.format(path=path))


def client(path=AUTH_PATH):
    """Return an authenticated ytmusicapi client."""
    headers = load_headers(path)
    try:
        return ytmusicapi.YTMusic(headers)
    except YTMusicError as exc:
        raise AuthExpired(_EXPIRED_HINT) from exc


def is_expiry(exc):
    """Whether a ytmusicapi error indicates credentials are no longer accepted."""
    return any(code in str(exc) for code in ("HTTP 401", "HTTP 403"))
