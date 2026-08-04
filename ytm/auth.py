"""Authentication module."""
import json
import os
from pathlib import Path

import ytmusicapi
from ytmusicapi.exceptions import YTMusicError

AUTH_PATH = Path.home() / ".config" / "ytm" / "auth.json"

_EXPIRED_HINT = (
    "YouTube Music authentication is no longer valid (browser headers expire "
    "when the session is revoked or the cookie ages out). Run 'ytm auth' to "
    "paste fresh request headers."
)
_MISSING_HINT = "No YouTube Music credentials found at {path}. Run 'ytm auth' to set them up."


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
