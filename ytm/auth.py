"""Authentication module."""
from collections import deque
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests
import ytmusicapi
from ytmusicapi.auth.oauth.credentials import OAuthCredentials
from ytmusicapi.auth.oauth.exceptions import BadOAuthClient, UnauthorizedOAuthClient
from ytmusicapi.auth.oauth.token import OAuthToken
from ytmusicapi.exceptions import YTMusicError

from ytm import config as config_mod
from ytm.authentication import session as session_mod
from ytm.authentication import storage as storage_mod
from ytm.authentication.errors import (
    AccountSelectionError as AccountSelectionError,
)
from ytm.authentication.errors import (
    AuthError,
    AuthExpired,
    AuthMissing,
    AuthStorageError,
    LoginCancelled,
    SessionVerificationUnavailable,
)
from ytm.authentication.errors import (
    AuthInvalidFormat as AuthInvalidFormat,
)
from ytm.authentication.errors import (
    BrowserUnavailable as BrowserUnavailable,
)
from ytm.authentication.errors import (
    LoginTimedOut as LoginTimedOut,
)
from ytm.authentication.manager import AuthManager

AUTH_PATH = Path.home() / ".config" / "ytm" / "auth.json"

#: The versioned active-record store new logins commit to. Kept separate
#: from AUTH_PATH so the legacy file stays readable during migration.
SESSION_PATH = storage_mod.default_session_path()

DEFAULT_DESKTOP_CLIENT = Path(__file__).with_name("oauth_default_client.json")

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
    "YouTube Music authentication is no longer valid. Run 'ytm login' to sign in again."
)
_MISSING_HINT = (
    "This command requires your YouTube Music account.\nRun 'ytm login' to sign in."
)

_SIGNED_OUT_HINT = (
    "YouTube Music is treating these credentials as signed out: the account "
    "service answered as if nobody is logged in. Run 'ytm login' again."
)

_REFRESH_FAILED_HINT = (
    " ytm tried to re-extract them from {browser} and could not: {reason}"
)

_OAUTH_EXPIRED_HINT = (
    "YouTube Music OAuth authentication is no longer valid (the refresh token "
    "was revoked or rejected). Run 'ytm login --method oauth' to sign in again, "
    "or plain 'ytm login' for a browser session."
)

_OAUTH_CLIENT_MISSING_HINT = (
    "OAuth client credentials are missing (expected alongside {path}). "
    "Run 'ytm auth' to set them up again."
)


def extract_cookies_from_browser(*args, **kwargs):
    """yt-dlp's browser cookie extraction, imported on first use.

    Importing yt_dlp pulls in its whole extractor and downloader tree, some
    25 ms, and only `ytm auth` and the automatic cookie refresh ever reach
    it -- while every search, every lyrics lookup and every autoplay radio
    spawn paid for it just by importing this module.
    """
    from yt_dlp.cookies import extract_cookies_from_browser as extract

    return extract(*args, **kwargs)


class _QuietLogger:
    """A yt-dlp logger that never prints anything (cookies must never be logged).

    It does remember yt-dlp's own status lines -- "Extracted 0 cookies from
    chrome (312 could not be decrypted)", "could not find ..." -- so a failed
    extraction can say *why* instead of a blanket "not logged in". Those
    messages stay in bounded memory only: third-party output is not trusted
    to be secret-free. Diagnostics persist only classified reason codes.
    """

    def __init__(self):
        self.messages = deque(maxlen=32)

    def debug(self, message):
        self.messages.append(str(message))

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


#: macOS keeps one app out of another's data until the asking app has Full
#: Disk Access. Chrome's profile is then present but unreadable, and yt-dlp
#: reports the cookie database as missing -- which reads as "Chrome is not
#: installed" and sends people looking for the wrong problem entirely.
_MACOS_UNREADABLE = (
    "installed, but macOS will not let {app} read it without Full Disk Access"
)

_MACOS_ACCESS_HINT = (
    " On macOS a program may not read another app's data until it has Full "
    "Disk Access: open System Settings > Privacy & Security > Full Disk "
    "Access, switch {app} on (add it with + if it is not listed), quit {app} "
    "completely and reopen it, then run 'ytm auth --from-browser' again. Or "
    "skip the browser: plain 'ytm auth' signs in with Google and needs none of this."
)

#: TERM_PROGRAM values worth showing by their proper name
_TERMINALS = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "WarpTerminal": "Warp",
    "ghostty": "Ghostty",
    "vscode": "Visual Studio Code",
    "Hyper": "Hyper",
    "WezTerm": "WezTerm",
    "tabby": "Tabby",
    "rio": "Rio",
    "kitty": "kitty",
}


def _terminal_name(environ=None):
    """What to call the app that needs Full Disk Access, so the instruction
    names the window the user is actually looking at."""
    environ = os.environ if environ is None else environ
    program = environ.get("TERM_PROGRAM")
    if program:
        return _TERMINALS.get(program, program)
    # kitty and Alacritty set no TERM_PROGRAM
    if environ.get("KITTY_WINDOW_ID") or environ.get("TERM", "").startswith("xterm-kitty"):
        return "kitty"
    if environ.get("ALACRITTY_WINDOW_ID") or environ.get("ALACRITTY_SOCKET"):
        return "Alacritty"
    if environ.get("WEZTERM_PANE"):
        return "WezTerm"
    return "your terminal"


def _unreadable_directory(text, isdir=None, readable=None):
    """The directory a yt-dlp "could not find" message names, when it is
    there but shut to us. None when it is genuinely absent.

    yt-dlp cannot tell the two apart: it walks the profile directory, the
    walk yields nothing because the OS denied it, and it reports the
    database as missing.
    """
    isdir = os.path.isdir if isdir is None else isdir
    readable = (lambda path: os.access(path, os.R_OK)) if readable is None else readable
    match = re.search(r'["\'](.+?)["\']', text)
    if match is None:
        return None
    path = match.group(1)
    # yt-dlp names the profiles directory for Firefox and the browser's own
    # directory for the Chromium family; a parent that is shut counts too
    for candidate in (path, os.path.dirname(path)):
        if candidate and isdir(candidate) and not readable(candidate):
            return candidate
    return None


def session_store(legacy_path=None):
    """The active-record store; ``legacy_path`` defaults to the current AUTH_PATH.

    ``None`` means "the canonical legacy location", not "no legacy file": the
    manager, status and logout must see the same credential the music layer
    does, or an existing ``auth.json`` is used by one code path while another
    reports "logged out" and leaves it behind on cleanup.
    """
    return storage_mod.SessionStore(
        SESSION_PATH,
        legacy_path=AUTH_PATH if legacy_path is None else legacy_path,
    )


def active_record(legacy_path=None):
    """The new-format active record, or None (legacy files are not records).

    Malformed records raise AuthInvalidFormat rather than falling through to
    another credential source.
    """
    return session_store(legacy_path).load()


def credential_stamp(legacy_path=None):
    """A value that changes whenever the active credential changes.

    The record revision when a new-format record is active, else the legacy
    file's (path, mtime_ns, size) -- the same identity the cached client
    already used.
    """
    path = AUTH_PATH if legacy_path is None else legacy_path
    record = session_store(path).load()
    if record is not None:
        return ("record", str(SESSION_PATH), record.revision, record.method)
    try:
        stat = os.stat(path)
    except OSError:
        return (str(path), None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _browser_client_factory(headers, user=None):
    return client_from_headers(headers, user=user)


def _probe_account(client):
    return client.get_account_info()


def auth_manager(legacy_path=None, *, client_factory=None, probe=None):
    """An AuthManager on the current store; injectable for tests."""
    return AuthManager(
        session_store(legacy_path),
        client_factory=client_factory or _browser_client_factory,
        probe=probe or _probe_account,
        oauth_client_factory=lambda record: _oauth_client(
            _managed_token_path(record, AUTH_PATH if legacy_path is None else legacy_path)
        ),
    )


def _managed_token_path(record, legacy_path):
    """The token file a stored OAuth record may point at.

    Only managed locations count: a path out of an untrusted JSON envelope
    must not make ytm read an arbitrary file.
    """
    token = Path(record.token_path)
    root = Path(SESSION_PATH).parent / "oauth"
    managed_generation = (
        token.name == "auth.json"
        and token.parent.parent == root
        and re.fullmatch(r"[0-9a-f]{32}", token.parent.name) is not None
        and token.resolve() == token.absolute()
    )
    if token != Path(legacy_path) and not managed_generation:
        raise AuthStorageError(
            "The stored OAuth record points outside the managed credential "
            "directory; run 'ytm login' again."
        )
    return token


def _client_from_record(record, legacy_path, credentials_factory=None):
    if record.method == "none":
        raise AuthMissing(_MISSING_HINT)
    if record.method == "browser":
        return client_from_headers(dict(record.headers or {}), legacy_path, user=record.user)
    return _oauth_client(_managed_token_path(record, legacy_path), credentials_factory)


def _oauth_client_path(path):
    """Where the OAuth app's client_id/client_secret are stored, alongside path.

    Kept separate from the token file because ytmusicapi rewrites the token
    file on every refresh with only token fields (see RefreshingToken.store_token),
    which would silently drop client_id/client_secret if they lived in the same file.
    """
    return path.parent / "oauth_client.json"


def _desktop_client_path(path):
    """Where the Google desktop client used by `ytm auth --client-file` is remembered."""
    return path.parent / "oauth_desktop_client.json"


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
    path=None,
    credentials_factory=None,
    sleep=time.sleep,
    client_file=None,
):
    """Run the OAuth device-code flow and store the resulting refreshable token at path.

    Prints a verification URL and short user code for the user to enter on another
    device, then polls token_from_code at the interval YouTube's response specifies
    until authorised (or the device code expires). The client_id/client_secret are
    persisted separately (see _oauth_client_path) since they are needed again for
    every future token refresh.
    """
    path = Path(AUTH_PATH if path is None else path)
    desktop_file = client_file or os.environ.get("YTM_OAUTH_CLIENT_FILE")
    stored_desktop = _desktop_client_path(path)
    tv_client_given = bool(client_id or client_secret or os.environ.get("YTM_OAUTH_CLIENT_ID")
                           or os.environ.get("YTM_OAUTH_CLIENT_SECRET"))
    if not desktop_file and stored_desktop.exists() and not tv_client_given:
        desktop_file = stored_desktop
    if not desktop_file and not tv_client_given and DEFAULT_DESKTOP_CLIENT.is_file():
        desktop_file = DEFAULT_DESKTOP_CLIENT
    if desktop_file:
        return desktop_oauth_setup(desktop_file, path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    client_id, client_secret = _resolve_oauth_client(client_id, client_secret)
    _write_json_0600(_oauth_client_path(path), {"client_id": client_id, "client_secret": client_secret})
    # The remembered desktop client would no longer match oauth_client.json.
    stored_desktop.unlink(missing_ok=True)

    make_credentials = credentials_factory or OAuthCredentials
    credentials = make_credentials(client_id, client_secret)
    try:
        code = credentials.get_code()
    except Exception as exc:
        raise AuthError("Could not start the OAuth device flow; check the client configuration and connection.") from exc

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
            raise AuthError("OAuth device authorisation was rejected; existing credentials were kept.")
        if time.time() > deadline:
            raise AuthError(
                "OAuth device code expired before authorisation completed; "
                "run 'ytm auth' again."
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


def desktop_oauth_setup(client_file, path=None):
    """Authorize a desktop client using PKCE and a loopback callback."""
    path = Path(AUTH_PATH if path is None else path)
    from google_auth_oauthlib.flow import InstalledAppFlow

    try:
        data = json.loads(Path(client_file).read_text(encoding="utf-8"))
        client_config = data["installed"]
        client_id = client_config["client_id"]
        client_secret = client_config["client_secret"]
        if not client_id or not client_secret:
            raise ValueError("empty client credentials")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuthError("Could not read a Google desktop OAuth client JSON.") from exc
    # Use Google's endpoints, never endpoints supplied by an imported file.
    desktop_config = {"installed": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}
    scope = "https://www.googleapis.com/auth/youtube"
    flow = InstalledAppFlow.from_client_config(
        desktop_config, scopes=[scope], autogenerate_code_verifier=True,
    )
    print("Sign in on this computer and approve YouTube access for ytm.", flush=True)
    try:
        flow.run_local_server(
            host="127.0.0.1", port=0, open_browser=True, timeout_seconds=900,
            authorization_prompt_message="Authorize ytm: {url}",
            success_message="Authorization received. You can return to ytm.",
            prompt="consent", access_type="offline",
        )
    except Exception as exc:
        # OAuth exceptions can include the callback URL or token response.
        raise AuthError("Google sign-in failed or timed out. Run 'ytm auth' again.") from exc
    raw = flow.oauth2session.token
    if not raw.get("refresh_token") or not raw.get("access_token"):
        raise AuthError("Google did not return a refreshable token; retry and approve YouTube access.")
    granted = raw.get("scope", [])
    if isinstance(granted, str):
        granted = granted.split()
    if scope not in granted:
        raise AuthError("YouTube access was not granted. Existing credentials were kept.")
    token = _ytmusicapi_token(raw, granted)
    _write_json_0600(_oauth_client_path(path), {"client_id": client_id, "client_secret": client_secret})
    _write_json_0600(_desktop_client_path(path), desktop_config)
    _write_json_0600(path, token)
    return path


def _ytmusicapi_token(raw, granted):
    """Reduce a google-auth-oauthlib token to exactly what ytmusicapi's OAuthToken accepts.

    oauthlib adds keys ytmusicapi does not know (id_token, expires_at as a
    float, refresh_token_expires_in, ...). ytmusicapi before 1.11 passes the
    whole file to the Token dataclass, so an unknown key is a TypeError at
    every client start; is_oauth() also needs every Token field present.
    """
    expires_in = int(raw.get("expires_in") or 3600)
    expires_at = raw.get("expires_at")
    expires_at = int(expires_at) if expires_at else int(time.time()) + expires_in
    token = {
        "scope": " ".join(granted),
        "token_type": raw.get("token_type") or "Bearer",
        "access_token": raw["access_token"],
        "refresh_token": raw["refresh_token"],
        "expires_at": expires_at,
        "expires_in": expires_in,
    }
    assert set(token) == set(OAuthToken.members())
    return token


def _cookie_header_from_jar(jar):
    """Build a Cookie header value from a http.cookiejar-style jar of youtube.com cookies.

    Returns None if the jar has no usable logged-in YouTube session (no __Secure-3PAPISID).
    """
    records = []
    for cookie in jar:
        domain = cookie.domain.lstrip(".").lower()
        expires = getattr(cookie, "expires", None)
        path = getattr(cookie, "path", "/") or "/"
        if domain not in ("youtube.com", "music.youtube.com"):
            continue
        if expires is not None and expires <= time.time():
            continue
        request_path = "/youtubei/v1/browse"
        if not (request_path == path or request_path.startswith(path.rstrip("/") + "/")):
            continue
        records.append({"name": cookie.name, "value": cookie.value})
    header = session_mod.cookies_to_header(records)
    return header if session_mod.cookie_value(header, session_mod.SIGNING_COOKIE) else None


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


#: Chromium profile directories that never hold a user's login.
_NON_USER_PROFILES = ("System Profile", "Guest Profile")


def _fork_databases(browser_dir, profile, logger):
    """Cookie databases under a Chromium fork's directory, newest first.

    Chromium keeps one per profile (Default, Profile 1, ...), plus System and
    Guest profiles that never hold a login. `profile` restricts the list to
    one profile directory name.
    """
    from yt_dlp import cookies as ytc

    databases = []
    for database in ytc._find_files(browser_dir, "Cookies", logger):
        relative = Path(os.path.relpath(database, browser_dir))
        top = relative.parts[0] if len(relative.parts) > 1 else None
        if top in _NON_USER_PROFILES:
            continue
        if profile is not None and top != profile:
            continue
        databases.append(database)
    return sorted(databases, key=lambda path: os.lstat(path).st_mtime, reverse=True)


def _read_fork_database(database, settings, logger):
    """Decrypt one Chromium cookie database into a jar, the way yt-dlp reads Chrome's."""
    from yt_dlp import cookies as ytc

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
                    settings["dir"], settings.get("keyring", "Chromium"), logger, meta_version=meta_version
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
    return jar, failed


def _extract_fork_cookies(browser_name, logger, profile=None):
    """Read a Chromium fork's cookies the way yt-dlp reads Chrome's.

    yt-dlp only accepts the browser names it knows, so forks reuse its
    Chromium machinery (`yt_dlp.cookies`) with our own profile directory and
    keystore label. Everything else -- database copy, decryption, cookie
    construction -- is yt-dlp's.

    Every profile's database is tried, newest first, and the first one holding
    a YouTube login wins; yt-dlp's "newest file" rule alone picks whichever
    profile the browser happened to flush last, which with several profiles is
    often not the logged-in one (#27). When none has a login, the newest is
    returned so the failure can still say how many cookies failed to decrypt.
    """
    settings = _fork_settings(browser_name)
    if settings is None:
        raise FileNotFoundError(f"{browser_name} is not supported on this platform")
    browser_dir = settings["dir"]
    databases = _fork_databases(browser_dir, profile, logger)
    if not databases:
        if profile is not None and os.path.isdir(browser_dir):
            raise FileNotFoundError(f'could not find profile "{profile}" in "{browser_dir}"')
        raise FileNotFoundError(f'could not find {browser_name} cookies database in "{browser_dir}"')
    first = None
    for database in databases:
        jar, failed = _read_fork_database(database, settings, logger)
        if first is None:
            first = (jar, failed)
        if _cookie_header_from_jar(jar):
            first = (jar, failed)
            break
    jar, failed = first
    suffix = f" ({failed} could not be decrypted)" if failed else ""
    logger.info(f"Extracted {len(jar)} cookies from {browser_name}{suffix}")
    return jar


def _extract_browser_cookie_header(browser_name, profile=None):
    """Return (cookie header or None, one-line reason when None).

    The reason is what the user needs to fix it: the browser was not found,
    its cookies could not be decrypted (App-Bound Encryption on Windows), or
    it simply has no YouTube login.
    """
    logger = _QuietLogger()
    try:
        if browser_name in _CHROMIUM_FORKS:
            jar = _extract_fork_cookies(browser_name, logger, profile=profile)
        else:
            jar = extract_cookies_from_browser(browser_name, profile=profile, logger=logger)
    except Exception as exc:
        from ytm.authentication import diagnostics
        # Inspect causes and yt-dlp warnings in memory, but never write their
        # raw text: a third-party exception may include cookies or a URL.
        errors, seen = [], set()
        current = exc
        while current is not None and id(current) not in seen and len(errors) < 8:
            seen.add(id(current))
            errors.append(current)
            current = current.__cause__ or current.__context__
        text = " ".join([*(str(e) for e in errors), *logger.messages]).lower()
        code = "extraction_failed"
        reason = "cookie extraction failed; check browser permissions and profile availability"
        if "app-bound" in text or "app bound" in text or "v20" in text:
            code, reason = "app_bound_encryption", "cookies could not be decrypted (App-Bound Encryption)"
        elif "decrypt" in text or "dpapi" in text:
            code, reason = "decryption_failed", "cookies could not be decrypted"
        elif "could not copy" in text and "cookie" in text:
            code, reason = "database_copy_failed", "could not copy cookie database; close the browser completely and retry under the same Windows user"
        elif "locked" in text or any(getattr(e, 'winerror', None) in (32, 33) for e in errors):
            code, reason = "database_locked", "cookie database locked; close the browser and retry"
        elif any(isinstance(e, PermissionError) for e in errors):
            code, reason = "permission_denied", "permission denied reading browser data; use the same OS user as the browser"
        elif "could not find profile" in text:
            code, reason = "profile_missing", "selected profile not found; check --profile"
        elif any(isinstance(e, FileNotFoundError) for e in errors) or "could not find" in text:
            code, reason = "browser_missing", "not installed or no profile found"
            if _unreadable_directory(str(exc)) is not None:
                reason = _MACOS_UNREADABLE.format(app=_terminal_name())
        underlying = errors[-1]
        diagnostics.event(code, browser=browser_name, profile_selected=profile is not None,
                          errno=getattr(underlying, 'errno', None), winerror=getattr(underlying, 'winerror', None))
        return None, reason
    from ytm.authentication import diagnostics
    header = _cookie_header_from_jar(jar)
    if header:
        diagnostics.event('cookies_available', browser=browser_name, profile_selected=profile is not None)
        return header, None
    failed = logger.decrypt_failures()
    evidence = " ".join(logger.messages).lower()
    if failed or any(term in evidence for term in ('decrypt', 'dpapi', 'app-bound', 'v20')):
        code = 'app_bound_encryption' if any(term in evidence for term in ('app-bound', 'v20')) else 'decryption_failed'
        diagnostics.event(code, browser=browser_name, profile_selected=profile is not None)
        return None, f"{failed} cookies could not be decrypted" if failed else "cookies could not be decrypted"
    diagnostics.event('no_youtube_session', browser=browser_name, profile_selected=profile is not None)
    return None, "no YouTube login"


def _macos_access_hint(reasons):
    """The Full Disk Access instruction, when that is what stood in the way."""
    if not any("Full Disk Access" in reason for reason in reasons.values()):
        return ""
    return _MACOS_ACCESS_HINT.format(app=_terminal_name())


def _windows_chromium_hint(reasons):
    """Extra guidance when Chromium cookies failed to decrypt on Windows."""
    if sys.platform != "win32":
        return ""
    if not any(
        name in _CHROMIUM_BROWSERS and "decrypt" in (reason or "").lower()
        for name, reason in reasons.items()
    ):
        return ""
    return (
        " Chrome 127 and newer on Windows use App-Bound Encryption; other Chromium browsers "
        "may apply similar protections. These cookies are ones that external programs "
        "may not be able to decrypt. Closing Chrome does not remove this protection. "
        "Try 'ytm login --method playwright --browser chrome' (Google may reject automated browsers), "
        "or log in at https://music.youtube.com in Firefox and run "
        "'ytm auth --from-browser firefox'."
    )


def _is_network_error(exc):
    """True when exc (or anything it was raised from) is a transport failure, not a refusal."""
    while exc is not None:
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _find_browser_cookie_header(browser, profile=None):
    """(cookie header, browser name, failures per browser) over one or all browsers."""
    candidates = [browser] if browser else list(_AUTODETECT_BROWSERS)
    reasons = {}
    for name in candidates:
        cookie_header, reason = _extract_browser_cookie_header(name, profile=profile)
        if cookie_header:
            return cookie_header, name, reasons
        reasons[name] = reason
    return None, None, reasons


def _no_browser_session_error(reasons):
    details = "; ".join(f"{name}: {reason}" for name, reason in reasons.items())
    blocked = _macos_access_hint(reasons)
    # being locked out of every browser is not the same as having no
    # login in any of them, and "log in first" is the wrong thing to
    # tell someone who already is
    closing = (
        "."
        if blocked
        else ". Browser sign-in may still be valid; importing it failed. Check the selected profile and the reason above. "
        "For a separate login, try 'ytm login --method playwright --browser chrome'; "
        "or sign into music.youtube.com in Firefox and use 'ytm login --from-browser firefox'."
    )
    if reasons and all(reason in ("not installed or no profile found", "no YouTube login") for reason in reasons.values()):
        closing = ". Log in at https://music.youtube.com in the chosen browser profile, then retry with --from-browser."
    return (
        "Could not import a YouTube Music browser session. "
        + details
        + closing
        + blocked
        + _windows_chromium_hint(reasons)
    )


def _authuser_index(config, authuser):
    if authuser is None:
        cfg = config if config is not None else config_mod.load()
        authuser = (cfg.get("auth") or config_mod.DEFAULTS["auth"])["x-goog-authuser"]
    authuser = str(authuser)
    if not authuser.isdecimal():
        raise AuthError(
            f"--authuser must be the numeric index of the Google account in the browser "
            f"(0 for the first, 1 for the second, ...), not {authuser!r}."
        )
    return authuser


def from_browser(browser=None, path=None, client_factory=None, profile=None, config=None, authuser=None):
    """Extract YouTube cookies from a local browser profile and store credentials at path.

    If browser is None, tries each of _AUTODETECT_BROWSERS in turn and uses the first
    that yields a logged-in YouTube cookie set. `profile` names one browser profile
    directory (Chromium: "Default", "Profile 1"; Firefox: the profile folder name)
    instead of letting the newest one win. `authuser` is the index of the Google
    account when the browser is signed in to several (the x-goog-authuser header);
    it overrides `auth.x-goog-authuser` in config.toml. Validates the extracted credentials with
    an account read before replacing the auth file; a failed candidate is
    discarded without changing existing credentials.

    Compatibility path: new imports should use `import_from_browser`, which
    validates before writing anything and commits the versioned record.
    """
    path = Path(AUTH_PATH if path is None else path)
    store = session_store(path)
    expected = auth_manager(path).expected_revision()
    before = credential_stamp(path)
    authuser = _authuser_index(config, authuser)
    cookie_header, name, reasons = _find_browser_cookie_header(browser, profile=profile)
    if cookie_header is None:
        raise AuthError(_no_browser_session_error(reasons))

    headers = {
        "cookie": cookie_header,
        "x-goog-authuser": authuser,
        "user-agent": _USER_AGENT,
        # Placeholder so ytmusicapi recognises this as browser auth; it is
        # regenerated from the cookie's SAPISID on every request.
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Legacy callers retain the old file format, but candidates are staged.
    with tempfile.TemporaryDirectory(prefix=".ytm-login-", dir=path.parent) as directory:
        candidate_path = Path(directory) / "auth.json"
        _write_json_0600(candidate_path, headers)
        make_client = client_factory or (lambda value: ytmusicapi.YTMusic(str(value)))
        try:
            checked = make_client(candidate_path)
            auth_manager(path)._read_account(checked)
        except Exception as exc:
            if _is_network_error(exc):
                raise AuthError("Browser session verification failed due to a network problem; existing credentials were kept.") from exc
            raise AuthError("Browser cookies did not authenticate successfully; existing credentials were kept. Run 'ytm login'.") from exc
        store._ensure_directory()
        with store._lock():
            if credential_stamp(path) != before or auth_manager(path).expected_revision() != expected:
                raise AuthStorageError("Credentials changed during browser import; nothing was replaced.")
            if store.load() is not None:
                # A deliberate import must replace the selected record, never
                # leave a tombstone or older session silently taking precedence.
                record = storage_mod.StoredRecord.browser(headers, source=f"existing_browser:{name}")
                store.save(record, expected_revision=expected)
                return store.path
            if path.is_symlink():
                raise AuthStorageError("Refusing to replace a symlinked credential file.")
            os.replace(candidate_path, path)
            _write_source(path, {"browser": name, "profile": profile, "authuser": authuser})
    return path


def import_from_browser(browser=None, path=None, profile=None, config=None, authuser=None,
                        confirm=None):
    """Import a logged-in browser session and store it only after validation.

    The new path: extraction happens first, but nothing replaces the stored
    credentials until the candidate has answered a real account read and
    (when ``confirm`` is given) the account has been accepted. A failure or
    a stale commit after a concurrent logout leaves whatever existed
    byte-for-byte intact.
    """
    path = AUTH_PATH if path is None else Path(path)
    manager = auth_manager(path)
    expected = manager.expected_revision()
    authuser = _authuser_index(config, authuser)
    cookie_header, browser_name, reasons = _find_browser_cookie_header(browser, profile=profile)
    if cookie_header is None:
        raise AuthError(_no_browser_session_error(reasons))
    headers = {
        "cookie": cookie_header,
        "x-goog-authuser": authuser,
        "user-agent": _USER_AGENT,
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }
    candidate = session_mod.build_session(
        headers, source=f"existing_browser:{browser_name}"
    )
    from ytm.authentication import diagnostics
    diagnostics.event('validating')
    verified = manager.validate_candidate(candidate)
    diagnostics.event('validated')
    if confirm is not None and not confirm(verified):
        raise LoginCancelled("Login cancelled; the previous credentials were kept.")
    record = manager.save_verified(verified, expected_revision=expected)
    diagnostics.event("stored")
    return record


def activate_oauth(token_path, path=None, credentials_factory=None):
    """Make an already-obtained OAuth token the active credential.

    Compatibility bridge for plain ``ytm auth``: the token was just issued
    by Google, so no account read is repeated, but the client is still built
    once so an unusable token fails before anything is switched. An older
    browser record must not keep taking precedence over an explicit sign-in.
    """
    path = Path(AUTH_PATH if path is None else path)
    _oauth_client(token_path, credentials_factory)
    manager = auth_manager(path)
    return manager.save_oauth(token_path, expected_revision=manager.expected_revision())


def oauth_login(client_id=None, client_secret=None, path=None, client_file=None,
                credentials_factory=None):
    """Acquire and verify OAuth in a new generation; activate only on success."""
    path = AUTH_PATH if path is None else Path(path)
    manager = auth_manager(path)
    expected = manager.expected_revision()
    manager.store._ensure_directory()
    root = manager.store.path.parent / "oauth"
    if root.is_symlink():
        raise AuthStorageError("OAuth storage directory must not be a symlink.")
    root.mkdir(mode=0o700, exist_ok=True)
    staging = root / uuid.uuid4().hex
    staging.mkdir(mode=0o700)
    committed = False
    try:
        remembered = _desktop_client_path(path)
        active = manager.store.load()
        if active is not None and active.method == "oauth":
            managed = _managed_token_path(active, path)
            remembered = _desktop_client_path(managed)
        explicit_tv = client_id or client_secret or os.environ.get("YTM_OAUTH_CLIENT_ID") or os.environ.get("YTM_OAUTH_CLIENT_SECRET")
        if not client_file and not os.environ.get("YTM_OAUTH_CLIENT_FILE") and not explicit_tv and remembered.is_file():
            client_file = remembered
        token_path = oauth_setup(
            client_id=client_id, client_secret=client_secret, path=staging / "auth.json",
            client_file=client_file, credentials_factory=credentials_factory,
        )
        client = _oauth_client(token_path, credentials_factory)
        manager._read_account(client)
        result = manager.save_oauth(token_path, expected_revision=expected)
        committed = True
        return result
    finally:
        if not committed:
            shutil.rmtree(staging, ignore_errors=True)


def source_path(path=None):
    """Where the browser an auth file came from is recorded (a sidecar, since
    every key in auth.json itself is sent to YouTube as a request header)."""
    path = Path(AUTH_PATH if path is None else path)
    path = Path(path)
    return path.with_name(path.stem + ".source.json")


def _write_source(path, source):
    with open(source_path(path), "w", encoding="utf-8") as file:
        json.dump(source, file)


def browser_source(path=None):
    """The browser, profile and authuser the auth at `path` was extracted from,
    or None if it was pasted, came from OAuth, or predates the record."""
    path = Path(AUTH_PATH if path is None else path)
    try:
        with open(source_path(path), encoding="utf-8") as file:
            source = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(source, dict) or not source.get("browser"):
        return None
    return source


_refresh_lock = threading.Lock()


def refresh_from_browser(path=None, client_factory=None):
    """Re-extract cookies from the browser the current auth came from.

    Google may rotate or revoke the session tokens in the browser, and
    the copy ytm holds is then treated as signed out. When that happens the
    catalogue layer calls this once and retries; the browser still has the
    live session, so the user never has to run 'ytm auth' by hand.

    Raises AuthError when there is no browser to go back to (pasted headers,
    OAuth) or when the extraction itself fails, for example because the
    browser is signed out too.
    """
    path = Path(AUTH_PATH if path is None else path)
    source = browser_source(path)
    if source is None:
        raise AuthError("these credentials were not extracted from a browser")
    with _refresh_lock:
        return from_browser(
            source["browser"],
            path=path,
            client_factory=client_factory,
            profile=source.get("profile"),
            authuser=source.get("authuser"),
        )


def load_headers(path=None):
    """Return the stored request headers.

    Raises AuthMissing if no usable credentials have been stored.
    """
    path = Path(AUTH_PATH if path is None else path)
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError) as exc:
        raise AuthMissing(_MISSING_HINT) from exc


def load_cookies(path=None):
    """Return the stored Cookie header value, for reuse by stream resolution.

    OAuth credentials have no cookies (there is no browser session to
    extract one from), so this returns None for them rather than raising --
    stream resolution falls back to cookie-less requests.
    """
    path = Path(AUTH_PATH if path is None else path)
    record = active_record(path)
    if record is not None:
        if record.method == "none":
            raise AuthMissing(_MISSING_HINT)
        if record.method == "oauth":
            return None
        return (record.headers or {}).get("cookie")
    headers = load_headers(path)
    if OAuthToken.is_oauth(headers):
        return None
    for key, value in headers.items():
        if key.lower() == "cookie":
            return value
    raise AuthMissing(_MISSING_HINT)


COOKIES_PATH = AUTH_PATH.parent / "cookies.txt"


def cookies_file(path=None, cookies_path=None):
    """A Netscape-format cookie file for yt-dlp, derived from the stored auth.

    yt-dlp (and therefore mpv's ytdl_hook) reads cookies from a file, while
    ytmusicapi keeps them as one Cookie header in auth.json. This writes the
    header out in the file format, refreshing it whenever the active
    credential is newer, so re-authenticating is the only step the user ever
    takes. Returns None when there are no cookies to write (OAuth auth,
    logged-out tombstone, or not authenticated).

    The export runs under the same store lock login/logout use: it either
    commits before a logout (whose cleanup then removes it) or runs after
    and sees the tombstone, so it can never resurrect a credential file.
    """
    path = Path(AUTH_PATH if path is None else path)
    cookies_path = Path(COOKIES_PATH if cookies_path is None else cookies_path)
    store = session_store(path)
    with store.transaction():
        try:
            record = store.load()
            if record is not None and record.method in ("none", "oauth"):
                return None
            header = load_cookies(path)
        except AuthError:
            return None
        if header is None:
            return None
        source = store.path if record is not None else path
        try:
            fresh = cookies_path.stat().st_mtime >= source.stat().st_mtime
        except OSError:
            fresh = False
        if fresh:
            return str(cookies_path)
        lines = ["# Netscape HTTP Cookie File"]
        for pair in header.split(";"):
            name, _, value = pair.strip().partition("=")
            if name:
                lines.append(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}")
        _write_cookies_atomic(cookies_path, "\n".join(lines) + "\n")
        return str(cookies_path)


def _write_cookies_atomic(path, text):
    """Publish the cookie export atomically, or leave the old one intact.

    A private temporary file in the same directory is fsynced and renamed
    over the destination; a symlinked destination is refused rather than
    followed and truncated, and a failed write removes only its own staging
    file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AuthStorageError("Refusing to write the cookie export through a symlink.")
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise AuthStorageError("Could not write the cookie export.") from exc


def _write_text_0600(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as file:
        file.write(text)
    os.chmod(path, 0o600)


def client(path=None, credentials_factory=None):
    """Return an authenticated ytmusicapi client.

    A new-format record takes precedence over the legacy file, including a
    logged-out tombstone (which raises rather than falling back). Without a
    record, the legacy file keeps working exactly as before.
    """
    path = Path(AUTH_PATH if path is None else path)
    record = active_record(path)
    if record is not None:
        return _client_from_record(record, path, credentials_factory)
    headers = load_headers(path)
    if OAuthToken.is_oauth(headers):
        return _oauth_client(path, credentials_factory)
    return client_from_headers(headers, path)


def browser_headers(path=None):
    """The browser request headers for the active credential, or None.

    Exposed so a caller can add to them (ytmusicapi treats the dict it is
    given as the client's base headers) before building the client.
    """
    path = Path(AUTH_PATH if path is None else path)
    record = active_record(path)
    if record is not None:
        if record.method != "browser":
            return None
        return dict(record.headers or {})
    headers = load_headers(path)
    return None if OAuthToken.is_oauth(headers) else headers


def client_from_headers(headers, path=None, user=None):
    """A ytmusicapi client for browser headers that are already in hand.

    ``user`` is ytmusicapi's supported brand-account hook; it becomes the
    request context's ``onBehalfOfUser``.
    """
    path = Path(AUTH_PATH if path is None else path)
    try:
        if user is None:
            return ytmusicapi.YTMusic(headers)
        return ytmusicapi.YTMusic(headers, user=user)
    except YTMusicError as exc:
        if is_expiry(exc):
            raise AuthExpired(_EXPIRED_HINT) from exc
        raise SessionVerificationUnavailable("Could not prepare the YouTube Music session. Try again later.") from exc


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


#: ytmusicapi's own status prefix; the only place a status is trusted. A
#: body that happens to contain an HTTP-looking number is not a status.
_HTTP_STATUS = re.compile(r"^Server returned HTTP (\d{3})(?:\D|$)")


def http_status(exc):
    """The HTTP status ytmusicapi reported in `exc`, or None."""
    match = _HTTP_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def is_expiry(exc):
    """Whether a ytmusicapi error means the session was rejected.

    Only HTTP 401 says that. A 403 can be a permission outcome for a valid
    session (playlist ownership, private items, audio CDNs), so it must be
    classified by the operation, not treated as expiry.
    """
    return http_status(exc) == 401
