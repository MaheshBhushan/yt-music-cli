"""Authentication module."""
import getpass
import json
import threading
import re
import subprocess
import sys
import tempfile
import os
import time
from pathlib import Path

import requests
import ytmusicapi
from ytm import config as config_mod
from ytmusicapi.auth.oauth.credentials import OAuthCredentials
from ytmusicapi.auth.oauth.exceptions import BadOAuthClient, UnauthorizedOAuthClient
from ytmusicapi.auth.oauth.token import OAuthToken
from ytmusicapi.exceptions import YTMusicError

AUTH_PATH = Path.home() / ".config" / "ytm" / "auth.json"
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
    "YouTube Music authentication is no longer valid (browser cookies expire "
    "when the session is revoked or the cookie ages out). Run 'ytm auth' to "
    "sign in with Google, or 'ytm auth --from-browser' to import fresh cookies."
)
_MISSING_HINT = "No YouTube Music credentials found at {path}. Run 'ytm auth' to set them up."

_SIGNED_OUT_HINT = (
    "YouTube Music is treating these credentials as signed out: the library "
    "and home feed came back empty instead of failing. Browser cookies have "
    "expired or were copied from a signed-out tab. Run 'ytm auth' again."
)

_REFRESH_FAILED_HINT = (
    " ytm tried to re-extract them from {browser} and could not: {reason}"
)

_OAUTH_EXPIRED_HINT = (
    "YouTube Music OAuth authentication is no longer valid (the refresh token "
    "was revoked or rejected). Run 'ytm auth' to sign in again."
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


class AuthError(Exception):
    """Base class for authentication problems."""


class AuthMissing(AuthError):
    """No credentials have been stored yet."""


class AuthExpired(AuthError):
    """Stored credentials are present but no longer accepted by YouTube Music."""


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
    path=AUTH_PATH,
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


def desktop_oauth_setup(client_file, path=AUTH_PATH):
    """Authorize a desktop client using PKCE and a loopback callback."""
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
        text = str(exc)
        if "could not find profile" in text:
            return None, f'profile "{profile}" not found'
        if isinstance(exc, FileNotFoundError) or "could not find" in text:
            if _unreadable_directory(text) is not None:
                return None, _MACOS_UNREADABLE.format(app=_terminal_name())
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
        " Chrome, Edge, Brave, Vivaldi and Opera on Windows protect their cookies "
        "with App-Bound Encryption (Chrome 127 and newer), which other programs "
        "cannot read. Options: plain 'ytm auth' signs in with Google without any "
        "cookies; or log in at https://music.youtube.com in Firefox and run "
        "'ytm auth --from-browser firefox'."
    )


def _is_network_error(exc):
    """True when exc (or anything it was raised from) is a transport failure, not a refusal."""
    while exc is not None:
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def from_browser(browser=None, path=AUTH_PATH, client_factory=None, profile=None, config=None, authuser=None):
    """Extract YouTube cookies from a local browser profile and store credentials at path.

    If browser is None, tries each of _AUTODETECT_BROWSERS in turn and uses the first
    that yields a logged-in YouTube cookie set. `profile` names one browser profile
    directory (Chromium: "Default", "Profile 1"; Firefox: the profile folder name)
    instead of letting the newest one win. `authuser` is the index of the Google
    account when the browser is signed in to several (the x-goog-authuser header);
    it overrides `auth.x-goog-authuser` in config.toml. Validates the extracted credentials with
    a live call before leaving the auth file in place; on failure the file is removed
    and AuthError is raised so a dead auth file is never left behind silently.
    """
    if authuser is None:
        cfg = config if config is not None else config_mod.load()
        authuser = (cfg.get("auth") or config_mod.DEFAULTS["auth"])["x-goog-authuser"]
    authuser = str(authuser)
    if not authuser.isdecimal():
        raise AuthError(
            f"--authuser must be the numeric index of the Google account in the browser "
            f"(0 for the first, 1 for the second, ...), not {authuser!r}."
        )
    candidates = [browser] if browser else list(_AUTODETECT_BROWSERS)
    cookie_header = None
    reasons = {}
    for name in candidates:
        cookie_header, reason = _extract_browser_cookie_header(name, profile=profile)
        if cookie_header:
            break
        reasons[name] = reason
    if cookie_header is None:
        details = "; ".join(f"{name}: {reason}" for name, reason in reasons.items())
        blocked = _macos_access_hint(reasons)
        # being locked out of every browser is not the same as having no
        # login in any of them, and "log in first" is the wrong thing to
        # tell someone who already is
        closing = (
            "."
            if blocked
            else ". Log in at https://music.youtube.com in one of these browsers "
            "first, then run 'ytm auth --from-browser' again."
        )
        raise AuthError(
            "No logged-in YouTube session found. "
            + details
            + closing
            + blocked
            + _windows_chromium_hint(reasons)
        )

    headers = {
        "cookie": cookie_header,
        "x-goog-authuser": authuser,
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
        source_path(path).unlink(missing_ok=True)
        if _is_network_error(exc):
            raise AuthError(
                "Browser cookies were extracted, but the check against YouTube Music "
                "failed to connect, so no auth file was left behind. This is a network "
                "problem, not a login problem: check connectivity (a machine whose IPv6 "
                "route is broken hangs here until the 30 s timeout; try disabling IPv6 "
                f"or setting a proxy). Underlying error: {exc}"
            ) from exc
        raise AuthError(
            "Extracted browser cookies were written but did not authenticate "
            "successfully; no auth file was left behind. Make sure you are logged "
            "in at https://music.youtube.com and try again. "
            f"Underlying error: {exc}"
        ) from exc
    # remember where these came from, so a stale set can be re-extracted
    # without asking (see refresh_from_browser)
    _write_source(path, {"browser": name, "profile": profile, "authuser": authuser})
    return path


def source_path(path=AUTH_PATH):
    """Where the browser an auth file came from is recorded (a sidecar, since
    every key in auth.json itself is sent to YouTube as a request header)."""
    path = Path(path)
    return path.with_name(path.stem + ".source.json")


def _write_source(path, source):
    with open(source_path(path), "w", encoding="utf-8") as file:
        json.dump(source, file)


def browser_source(path=AUTH_PATH):
    """The browser, profile and authuser the auth at `path` was extracted from,
    or None if it was pasted, came from OAuth, or predates the record."""
    try:
        with open(source_path(path), encoding="utf-8") as file:
            source = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(source, dict) or not source.get("browser"):
        return None
    return source


_refresh_lock = threading.Lock()


def refresh_from_browser(path=AUTH_PATH, client_factory=None):
    """Re-extract cookies from the browser the current auth came from.

    Google rotates the session tokens in the browser every day or so, and
    the copy ytm holds is then treated as signed out. When that happens the
    catalogue layer calls this once and retries; the browser still has the
    live session, so the user never has to run 'ytm auth' by hand.

    Raises AuthError when there is no browser to go back to (pasted headers,
    OAuth) or when the extraction itself fails, for example because the
    browser is signed out too.
    """
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
    return client_from_headers(headers, path)


def browser_headers(path=AUTH_PATH):
    """The stored browser request headers, or None when the auth is OAuth.

    Exposed so a caller can add to them (ytmusicapi treats the dict it is
    given as the client's base headers) before building the client.
    """
    headers = load_headers(path)
    return None if OAuthToken.is_oauth(headers) else headers


def client_from_headers(headers, path=AUTH_PATH):
    """A ytmusicapi client for browser headers that are already in hand."""
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
