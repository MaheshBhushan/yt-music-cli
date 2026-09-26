"""Open normal browser profiles without attaching automation to them.

The user signs in on the website. After they return to the terminal, read
only the chosen profile through the existing yt-dlp cookie adapter. Never
start a remote debugging server, copy a profile, or close the user's browser.
"""
from __future__ import annotations

import configparser
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ytm.authentication.errors import BrowserUnavailable, LoginCancelled, LoginTimedOut
from ytm.authentication.session import MUSIC_ORIGIN, build_session

NATIVE_BROWSERS = ('chrome', 'chromium', 'edge', 'firefox', 'brave', 'vivaldi', 'opera', 'helium')
EXECUTABLES = {
    'chrome': ('google-chrome', 'google-chrome-stable', 'chrome'),
    'chromium': ('chromium', 'chromium-browser'),
    'edge': ('microsoft-edge', 'microsoft-edge-stable', 'msedge'),
    'firefox': ('firefox',), 'brave': ('brave', 'brave-browser'),
    'vivaldi': ('vivaldi', 'vivaldi-stable'), 'opera': ('opera',), 'helium': ('helium',),
}
MAC_APPS = {'chrome': 'Google Chrome', 'chromium': 'Chromium', 'edge': 'Microsoft Edge',
            'firefox': 'Firefox', 'brave': 'Brave Browser', 'vivaldi': 'Vivaldi',
            'opera': 'Opera', 'helium': 'Helium'}
WINDOWS_EXES = {
    'chrome': 'Google/Chrome/Application/chrome.exe', 'edge': 'Microsoft/Edge/Application/msedge.exe',
    'chromium': 'Chromium/Application/chrome.exe', 'firefox': 'Mozilla Firefox/firefox.exe',
    'brave': 'BraveSoftware/Brave-Browser/Application/brave.exe',
    'vivaldi': 'Vivaldi/Application/vivaldi.exe', 'opera': 'Programs/Opera/launcher.exe',
    'helium': 'imput/Helium/Application/helium.exe',
}


def browser_from_identifier(identifier):
    value = str(identifier).lower()
    for marker, name in (('helium', 'helium'), ('brave', 'brave'), ('vivaldi', 'vivaldi'),
                         ('opera', 'opera'), ('firefox', 'firefox'), ('chromium', 'chromium'),
                         ('chrome', 'chrome'), ('edge', 'edge')):
        if marker in value:
            return name
    raise BrowserUnavailable(
        "The default browser cannot be identified or imported. Choose --browser chrome, "
        "edge, firefox, brave, chromium, vivaldi, opera or helium; "
        "or use --method playwright --browser chromium."
    )


def default_browser():
    """Identify the OS HTTPS association; never scan unrelated cookie stores."""
    try:
        if sys.platform == 'win32':
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice') as key:
                identifier = winreg.QueryValueEx(key, 'ProgId')[0]
        elif sys.platform == 'darwin':
            result = subprocess.run(
                ['defaults', 'export', 'com.apple.LaunchServices/com.apple.launchservices.secure', '-'],
                capture_output=True, check=True, timeout=5,
            )
            handlers = plistlib.loads(result.stdout).get('LSHandlers', [])
            identifier = next(h['LSHandlerRoleAll'] for h in reversed(handlers)
                              if h.get('LSHandlerURLScheme') == 'https')
        else:
            identifier = subprocess.run(['xdg-settings', 'get', 'default-web-browser'],
                                        capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        return browser_from_identifier(identifier)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as exc:
        raise BrowserUnavailable("Could not identify the default browser. Choose 'ytm login --browser chrome' or another supported browser.") from exc


def browser_executable(browser):
    for name in EXECUTABLES[browser]:
        executable = shutil.which(name)
        if executable:
            return executable
    if sys.platform == 'darwin':
        app = MAC_APPS[browser]
        binary = 'firefox' if browser == 'firefox' else app
        for root in (Path('/Applications'), Path.home() / 'Applications'):
            path = root / f'{app}.app/Contents/MacOS/{binary}'
            if path.is_file():
                return str(path)
    if sys.platform == 'win32':
        for env in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
            if os.environ.get(env):
                path = Path(os.environ[env]) / WINDOWS_EXES[browser]
                if path.is_file():
                    return str(path)
    raise BrowserUnavailable(
        f"Could not find {browser}. Install it normally, use --from-browser to import an "
        "already open browser, or use --method playwright."
    )


def firefox_profile(profile=None):
    if sys.platform == 'darwin':
        root = Path.home() / 'Library/Application Support/Firefox'
    elif sys.platform == 'win32':
        root = Path(os.environ.get('APPDATA', '')) / 'Mozilla/Firefox'
    else:
        root = Path.home() / '.mozilla/firefox'
    parser = configparser.ConfigParser()
    try:
        parser.read(root / 'profiles.ini')
        profiles = []
        for section in parser.sections():
            value = parser[section]
            if 'Path' not in value:
                continue
            path = Path(value['Path']) if value.get('IsRelative') == '0' else root / value['Path']
            profiles.append((value, path))
        if profile:
            selected = next(path for info, path in profiles
                            if profile in (info.get('Name'), path.name, str(path)))
        else:
            installed = next((root / parser[s]['Default'] for s in parser.sections()
                              if s.startswith('Install') and parser[s].get('Default')), None)
            selected = installed or next((path for info, path in profiles if info.get('Default') == '1'),
                                         profiles[0][1] if len(profiles) == 1 else None)
        if selected is not None and selected.is_dir():
            return str(selected.resolve())
    except (OSError, ValueError, StopIteration, configparser.Error):
        pass
    raise BrowserUnavailable("Could not resolve Firefox's profile. Open Firefox once, then use --profile with its profile name, or --from-browser firefox.")


def launch_arguments(browser, profile=None):
    if browser == 'firefox':
        selected = firefox_profile(profile)
        return ['-profile', selected, MUSIC_ORIGIN], selected
    if browser == 'opera':
        if profile:
            raise BrowserUnavailable("Opera profile selection is not supported; omit --profile.")
        return [MUSIC_ORIGIN], None
    selected = profile or 'Default'
    if selected in ('.', '..') or any(c in selected for c in ('/', '\\', '\x00', '\n', '\r')):
        raise BrowserUnavailable("Use a browser profile directory name, such as Default or Profile 1.")
    return [f'--profile-directory={selected}', MUSIC_ORIGIN], selected


class NativeBrowser:
    def __init__(self, *, browser=None, profile=None, authuser=None, ready=None):
        self.browser = browser
        self.profile = profile
        self.authuser = authuser
        self.ready = ready or self._ready
        self._needs_terminal = ready is None

    @staticmethod
    def _ready():
        print('Sign in to YouTube Music, then press Enter here to import that profile. '
              'If cookies are locked, close the browser yourself before continuing.', file=sys.stderr)
        try:
            input()
        except EOFError as exc:
            raise LoginCancelled('Browser login needs an interactive terminal. Use --from-browser for an existing session.') from exc

    def observe(self, *, timeout):
        from ytm import auth

        if self._needs_terminal and not sys.stdin.isatty():
            raise LoginCancelled("Normal-browser login needs a terminal. Use --from-browser for a stored session.")
        started = time.monotonic()
        browser = self.browser if self.browser not in (None, 'default') else default_browser()
        if browser == 'msedge':
            browser = 'edge'
        if browser not in NATIVE_BROWSERS:
            raise BrowserUnavailable('That browser is only supported with --method playwright.')
        arguments, profile = launch_arguments(browser, self.profile)
        executable = browser_executable(browser)
        try:
            subprocess.Popen([executable, *arguments], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
        except OSError as exc:
            raise BrowserUnavailable('Could not open the selected browser.') from exc
        self.ready()
        # The terminal prompt is user-controlled; reject a late confirmation.
        if time.monotonic() - started >= timeout:
            raise LoginTimedOut('Login timed out; existing credentials were kept.')
        cookie, _, reasons = auth._find_browser_cookie_header(browser, profile=profile)
        if cookie is None:
            raise BrowserUnavailable(auth._no_browser_session_error(reasons))
        return build_session({'cookie': cookie, 'x-goog-authuser': auth._authuser_index(None, self.authuser)},
                             source=f'existing_browser:{browser}')
