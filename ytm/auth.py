"""Authentication module."""
import getpass
import json
import re
import subprocess
import sys
import tempfile
import os
import time
from pathlib import Path

import ytmusicapi
from yt_dlp.cookies import extract_cookies_from_browser
from ytmusicapi.auth.oauth.credentials import OAuthCredentials
from ytmusicapi.auth.oauth.exceptions import BadOAuthClient, UnauthorizedOAuthClient
from ytmusicapi.auth.oauth.token import OAuthToken
from ytmusicapi.exceptions import YTMusicError

AUTH_PATH = Path.home() / ".config" / "ytm" / "auth.json"

# Order in which --from-browser auto-detection tries local browser profiles.
# On Windows, Firefox goes first: Chromium browsers there (Chrome 127+, and
# Edge/Brave/Vivaldi/Opera on the same engine) protect cookies with
# App-Bound Encryption, which no outside program can undo, so they never
# yield a usable session and only cost time.
_CHROMIUM_BROWSERS = ("chrome", "chromium", "edge", "brave", "vivaldi", "opera", "helium")

#: Chromium forks yt-dlp does not know by name. Per platform: where the
#: profile lives and how the cookie key is labelled in the OS keystore.
#: Helium: github.com/imputnet/helium-{macos,linux,windows} branding patches.
_CHROMIUM_FORKS = {
    "helium": {
        "darwin": {
            "dir": "~/Library/Application Support/net.imput.helium",
            "keychain": ("Helium Storage Key", "Helium"),  # (service, account)
        },
        "linux": {"dir": "~/.config/net.imput.helium", "keyring": "Chromium"},
        "win32": {"dir": r"%LOCALAPPDATA%\imput\Helium\User Data"},
    },
}
_AUTODETECT_BROWSERS = (
    ("firefox", *_CHROMIUM_BROWSERS) if sys.platform == "win32" else (*_CHROMIUM_BROWSERS, "firefox")
)

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

_SIGNED_OUT_HINT = (
    "YouTube Music is treating these credentials as signed out: the library "
    "and home feed came back empty instead of failing. Browser cookies have "
    "expired or were copied from a signed-out tab. Run 'ytm auth' again."
)

_OAUTH_EXPIRED_HINT = (
    "YouTube Music OAuth authentication is no longer valid (the refresh token "
    "was revoked or rejected). Run 'ytm auth --oauth' to re-authenticate."
)

_OAUTH_CLIENT_MISSING_HINT = (
    "OAuth client credentials are missing (expected alongside {path}). "
    "Run 'ytm auth --oauth' to set them up again."
)


class _QuietLogger:
    """A yt-dlp logger that never prints anything (cookies must never be logged).

    It does remember yt-dlp's own status lines -- "Extracted 0 cookies from
    chrome (312 could not be decrypted)", "could not find ..." -- so a failed
    extraction can say *why* instead of a blanket "not logged in". Those
    lines carry counts and paths, never cookie values.
    """

    def __init__(self):
        self.messages = []

    def debug(self, message):
        pass

    def info(self, message):
        self.messages.append(str(message))

    def warning(self, message, only_once=False):
        self.messages.append(str(message))

    def error(self, message):
        self.messages.append(str(message))

    def decrypt_failures(self):
        """How many cookies yt-dlp could not decrypt, per its summary line."""
        for message in self.messages:
            match = re.search(r"\((\d+) could not be decrypted\)", message)
            if match:
                return int(match.group(1))
        return 0


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


def _oauth_client_path(path):
    """Where the OAuth app's client_id/client_secret are stored, alongside path.

    Kept separate from the token file because ytmusicapi rewrites the token
    file on every refresh with only token fields (see RefreshingToken.store_token),
    which would silently drop client_id/client_secret if they lived in the same file.
    """
    return path.parent / "oauth_client.json"


def _write_json_0600(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as file:
        json.dump(data, file)
    os.chmod(path, 0o600)


def _resolve_oauth_client(client_id, client_secret):
    """Resolve client_id/client_secret: explicit args > env vars > interactive prompt."""
    client_id = client_id or os.environ.get("YTM_OAUTH_CLIENT_ID")
    client_secret = client_secret or os.environ.get("YTM_OAUTH_CLIENT_SECRET")
    if not client_id:
        client_id = input("Google Cloud OAuth client ID: ").strip()
    if not client_secret:
        client_secret = getpass.getpass("Google Cloud OAuth client secret: ").strip()
    if not client_id or not client_secret:
        raise AuthError(
            "An OAuth client_id and client_secret are required. Create a 'TVs and "
            "Limited Input devices' OAuth client in Google Cloud Console and pass "
            "them via --client-id/--client-secret, YTM_OAUTH_CLIENT_ID/"
            "YTM_OAUTH_CLIENT_SECRET, or the interactive prompt."
        )
    return client_id, client_secret


def _load_oauth_client(path):
    """Return the stored (client_id, client_secret) for the OAuth token at path."""
    client_path = _oauth_client_path(path)
    try:
        with open(client_path, encoding="utf-8") as file:
            data = json.load(file)
        return data["client_id"], data["client_secret"]
    except (OSError, ValueError, KeyError) as exc:
        raise AuthMissing(_OAUTH_CLIENT_MISSING_HINT.format(path=path)) from exc


def oauth_setup(
    client_id=None,
    client_secret=None,
    path=AUTH_PATH,
    credentials_factory=None,
    sleep=time.sleep,
):
    """Run the OAuth device-code flow and store the resulting refreshable token at path.

    Prints a verification URL and short user code for the user to enter on another
    device, then polls token_from_code at the interval YouTube's response specifies
    until authorised (or the device code expires). The client_id/client_secret are
    persisted separately (see _oauth_client_path) since they are needed again for
    every future token refresh.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    client_id, client_secret = _resolve_oauth_client(client_id, client_secret)
    _write_json_0600(_oauth_client_path(path), {"client_id": client_id, "client_secret": client_secret})

    make_credentials = credentials_factory or OAuthCredentials
    credentials = make_credentials(client_id, client_secret)
    try:
        code = credentials.get_code()
    except Exception as exc:
        raise AuthError(f"Could not start the OAuth device flow: {exc}") from exc

    print(f"Go to {code['verification_url']} and enter the code: {code['user_code']}")
    print("Waiting for you to authorise this device...")

    interval = code.get("interval", 5)
    deadline = time.time() + code.get("expires_in", 1800)
    raw = None
    while True:
        sleep(interval)
        raw = credentials.token_from_code(code["device_code"])
        if "access_token" in raw:
            break
        error = raw.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise AuthError(f"OAuth device authorisation failed: {raw}")
        if time.time() > deadline:
            raise AuthError(
                "OAuth device code expired before authorisation completed; "
                "run 'ytm auth --oauth' again."
            )

    token = {
        "scope": raw["scope"],
        "token_type": raw["token_type"],
        "access_token": raw["access_token"],
        "refresh_token": raw["refresh_token"],
        "expires_in": raw["expires_in"],
        "expires_at": int(time.time()) + raw["expires_in"],
    }
    _write_json_0600(path, token)
    return path


def _cookie_header_from_jar(jar):
    """Build a Cookie header value from a http.cookiejar-style jar of youtube.com cookies.

    Returns None if the jar has no usable logged-in YouTube session (no __Secure-3PAPISID).
    """
    pairs = [(cookie.name, cookie.value) for cookie in jar if "youtube.com" in cookie.domain]
    if not any(name == "__Secure-3PAPISID" for name, _ in pairs):
        return None
    return "; ".join(f"{name}={value}" for name, value in pairs)


def _fork_settings(browser_name):
    """Profile dir and keystore label for a Chromium fork on this platform,
    or None when the fork is unknown here."""
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    settings = _CHROMIUM_FORKS.get(browser_name, {}).get(platform)
    if settings is None:
        return None
    directory = settings["dir"]
    if platform == "linux":
        directory = directory.replace("~/.config", os.environ.get("XDG_CONFIG_HOME", "~/.config"), 1)
    # %VAR% is Windows syntax; os.path.expandvars only honours it on Windows
    directory = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), directory)
    return {**settings, "dir": os.path.expanduser(directory)}


def _keychain_password(service, account):
    """The cookie key macOS keeps for a browser, from the login Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-a", account, "-s", service],
        capture_output=True, check=False,
    )
    return result.stdout.rstrip(b"\n") if result.returncode == 0 else None


def _extract_fork_cookies(browser_name, logger):
    """Read a Chromium fork's cookie database the way yt-dlp reads Chrome's.

    yt-dlp only accepts the browser names it knows, so forks reuse its
    Chromium machinery (`yt_dlp.cookies`) with our own profile directory and
    keystore label. Everything else -- database copy, decryption, cookie
    construction -- is yt-dlp's.
    """
    from yt_dlp import cookies as ytc

    settings = _fork_settings(browser_name)
    if settings is None:
        raise FileNotFoundError(f"{browser_name} is not supported on this platform")
    browser_dir = settings["dir"]
    database = ytc._newest(ytc._find_files(browser_dir, "Cookies", logger))
    if database is None:
        raise FileNotFoundError(f'could not find {browser_name} cookies database in "{browser_dir}"')
    with tempfile.TemporaryDirectory(prefix="ytm") as tmpdir:
        cursor = ytc._open_database_copy(database, tmpdir)
        try:
            meta_version = int(cursor.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()[0])
            if sys.platform == "darwin" and "keychain" in settings:
                # yt-dlp derives the Keychain item from "<name> Safe Storage";
                # Helium renamed it, so fetch the password ourselves
                decryptor = ytc.MacChromeCookieDecryptor("Chromium", logger, meta_version=meta_version)
                password = _keychain_password(*settings["keychain"])
                decryptor._v10_key = None if password is None else decryptor.derive_key(password)
            else:
                decryptor = ytc.get_cookie_decryptor(
                    browser_dir, settings.get("keyring", "Chromium"), logger, meta_version=meta_version
                )
            cursor.connection.text_factory = bytes
            columns = ytc._get_column_names(cursor, "cookies")
            secure = "is_secure" if "is_secure" in columns else "secure"
            cursor.execute(
                f"SELECT host_key, name, value, encrypted_value, path, expires_utc, {secure} FROM cookies"
            )
            jar = ytc.YoutubeDLCookieJar()
            failed = 0
            for row in cursor.fetchall():
                _, cookie = ytc._process_chrome_cookie(decryptor, *row)
                if cookie is None:
                    failed += 1
                    continue
                jar.set_cookie(cookie)
        finally:
            cursor.connection.close()
    suffix = f" ({failed} could not be decrypted)" if failed else ""
    logger.info(f"Extracted {len(jar)} cookies from {browser_name}{suffix}")
    return jar


def _extract_browser_cookie_header(browser_name):
    """Return (cookie header or None, one-line reason when None).

    The reason is what the user needs to fix it: the browser was not found,
    its cookies could not be decrypted (App-Bound Encryption on Windows), or
    it simply has no YouTube login.
    """
    logger = _QuietLogger()
    try:
        if browser_name in _CHROMIUM_FORKS:
            jar = _extract_fork_cookies(browser_name, logger)
        else:
            jar = extract_cookies_from_browser(browser_name, logger=logger)
    except Exception as exc:
        text = str(exc)
        if isinstance(exc, FileNotFoundError) or "could not find" in text:
            return None, "not installed or no profile found"
        if "locked" in text.lower():
            return None, "cookie database locked; close the browser and retry"
        return None, text.splitlines()[0] if text else type(exc).__name__
    header = _cookie_header_from_jar(jar)
    if header:
        return header, None
    failed = logger.decrypt_failures()
    if failed:
        return None, f"{failed} cookies could not be decrypted"
    return None, "no YouTube login"


def _windows_chromium_hint(reasons):
    """Extra guidance when Chromium cookies failed to decrypt on Windows."""
    if sys.platform != "win32":
        return ""
    if not any(
        name in _CHROMIUM_BROWSERS and "decrypted" in (reason or "")
        for name, reason in reasons.items()
    ):
        return ""
    return (
        " Chrome, Edge, Brave, Vivaldi and Opera on Windows protect their cookies "
        "with App-Bound Encryption (Chrome 127 and newer), which other programs "
        "cannot read. Options: log in at https://music.youtube.com in Firefox and "
        "run 'ytm auth --from-browser firefox'; or 'ytm auth --manual' and paste "
        "the request headers from the browser's DevTools; or 'ytm auth --oauth'."
    )


def from_browser(browser=None, path=AUTH_PATH, client_factory=None):
    """Extract YouTube cookies from a local browser profile and store credentials at path.

    If browser is None, tries each of _AUTODETECT_BROWSERS in turn and uses the first
    that yields a logged-in YouTube cookie set. Validates the extracted credentials with
    a live call before leaving the auth file in place; on failure the file is removed
    and AuthError is raised so a dead auth file is never left behind silently.
    """
    candidates = [browser] if browser else list(_AUTODETECT_BROWSERS)
    cookie_header = None
    reasons = {}
    for name in candidates:
        cookie_header, reason = _extract_browser_cookie_header(name)
        if cookie_header:
            break
        reasons[name] = reason
    if cookie_header is None:
        details = "; ".join(f"{name}: {reason}" for name, reason in reasons.items())
        raise AuthError(
            "No logged-in YouTube session found. "
            + details
            + ". Log in at https://music.youtube.com in one of these browsers first, "
            "then run 'ytm auth --from-browser' again."
            + _windows_chromium_hint(reasons)
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
    """Return the stored Cookie header value, for reuse by stream resolution.

    OAuth auth files have no cookies (there is no browser session to extract
    one from), so this returns None for them rather than raising -- stream
    resolution falls back to cookie-less requests, see ytm/resolve.py.
    """
    headers = load_headers(path)
    if OAuthToken.is_oauth(headers):
        return None
    for key, value in headers.items():
        if key.lower() == "cookie":
            return value
    raise AuthMissing(_MISSING_HINT.format(path=path))


COOKIES_PATH = AUTH_PATH.parent / "cookies.txt"


def cookies_file(path=AUTH_PATH, cookies_path=COOKIES_PATH):
    """A Netscape-format cookie file for yt-dlp, derived from the stored auth.

    yt-dlp (and therefore mpv's ytdl_hook) reads cookies from a file, while
    ytmusicapi keeps them as one Cookie header in auth.json. This writes the
    header out in the file format, refreshing it whenever auth.json is newer,
    so re-authenticating is the only step the user ever takes. Returns None
    when there are no cookies to write (OAuth auth, or not authenticated).
    """
    try:
        header = load_cookies(path)
    except AuthError:
        return None
    if header is None:
        return None
    cookies_path = Path(cookies_path)
    try:
        fresh = cookies_path.stat().st_mtime >= Path(path).stat().st_mtime
    except OSError:
        fresh = False
    if fresh:
        return str(cookies_path)
    lines = ["# Netscape HTTP Cookie File"]
    for pair in header.split(";"):
        name, _, value = pair.strip().partition("=")
        if name:
            lines.append(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}")
    _write_text_0600(cookies_path, "\n".join(lines) + "\n")
    return str(cookies_path)


def _write_text_0600(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as file:
        file.write(text)
    os.chmod(path, 0o600)


def client(path=AUTH_PATH, credentials_factory=None):
    """Return an authenticated ytmusicapi client, for either auth kind stored at path."""
    headers = load_headers(path)
    if OAuthToken.is_oauth(headers):
        return _oauth_client(path, credentials_factory)
    try:
        return ytmusicapi.YTMusic(headers)
    except YTMusicError as exc:
        raise AuthExpired(_EXPIRED_HINT) from exc


def _oauth_client(path, credentials_factory=None):
    """Build a YTMusic client from a stored OAuth token, eagerly refreshing if due."""
    client_id, client_secret = _load_oauth_client(path)
    make_credentials = credentials_factory or OAuthCredentials
    credentials = make_credentials(client_id, client_secret)
    try:
        ytm = ytmusicapi.YTMusic(str(path), oauth_credentials=credentials)
        # Touching access_token triggers RefreshingToken's auto-refresh (and
        # persists it back to path) if the stored token is due to expire, so a
        # revoked/invalid refresh token surfaces here rather than mid-request.
        _ = ytm._token.access_token
    except (YTMusicError, UnauthorizedOAuthClient, BadOAuthClient) as exc:
        raise AuthExpired(_OAUTH_EXPIRED_HINT) from exc
    return ytm


def is_expiry(exc):
    """Whether a ytmusicapi error indicates credentials are no longer accepted."""
    return any(code in str(exc) for code in ("HTTP 401", "HTTP 403"))
