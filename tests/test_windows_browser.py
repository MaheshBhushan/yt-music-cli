"""W4: Windows browser discovery and honest failure routing (simulated).

Real Windows registry, executable and cookie behavior needs a Windows host,
so these tests pin the routing and error messages with fake registry data
and a simulated platform identifier. They do not certify a real sign-in;
the live matrix stays a manual, consenting-user step.
"""

import sys
from types import SimpleNamespace

import pytest

from ytm import auth
from ytm.authentication import browser_profiles as profiles
from ytm.authentication.errors import BrowserUnavailable


class FakeKey:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_winreg(progid=None, error=None):
    class Winreg:
        HKEY_CURRENT_USER = object()

        @staticmethod
        def OpenKey(root, path):
            if error is not None:
                raise error
            return FakeKey(progid)

        @staticmethod
        def QueryValueEx(key, name):
            return key._value, 1

    return Winreg()


def windows(monkeypatch):
    monkeypatch.setattr(profiles.sys, "platform", "win32")
    monkeypatch.setattr(profiles.shutil, "which", lambda name: None)


def test_windows_https_association_selects_the_default_browser(monkeypatch):
    windows(monkeypatch)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg("ChromeHTML"))
    assert profiles.default_browser() == "chrome"


def test_windows_missing_association_is_actionable(monkeypatch):
    windows(monkeypatch)
    monkeypatch.setitem(
        sys.modules, "winreg", fake_winreg(error=FileNotFoundError("no UserChoice key"))
    )
    with pytest.raises(BrowserUnavailable, match="Choose 'ytm login --browser"):
        profiles.default_browser()


def test_windows_unknown_association_never_falls_back_to_another_store(monkeypatch):
    windows(monkeypatch)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg("SafariHTML"))
    with pytest.raises(BrowserUnavailable):
        profiles.default_browser()


def test_explicit_browser_overrides_the_os_default(monkeypatch):
    monkeypatch.setattr(
        profiles, "default_browser", lambda: pytest.fail("must not consult the OS default")
    )
    monkeypatch.setattr(profiles, "browser_executable", lambda name: f"/fake/{name}")
    monkeypatch.setattr(profiles, "firefox_profile", lambda profile=None: "/fake/profile")
    launched = []
    monkeypatch.setattr(
        profiles.subprocess, "Popen", lambda command, **kwargs: launched.append(command)
    )
    monkeypatch.setattr(
        auth, "_find_browser_cookie_header",
        lambda browser, profile=None: ("__Secure-3PAPISID=FAKE", browser, {}),
    )
    candidate = profiles.NativeBrowser(browser="firefox", ready=lambda: None).observe(timeout=30)
    assert launched and launched[0][0] == "/fake/firefox"
    assert candidate.headers["cookie"] == "__Secure-3PAPISID=FAKE"


@pytest.mark.parametrize("env", ["LOCALAPPDATA", "PROGRAMFILES"])
def test_windows_executable_discovery_per_user_and_system(tmp_path, monkeypatch, env):
    windows(monkeypatch)
    for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "install" / "Google/Chrome/Application"
    root.mkdir(parents=True)
    executable = root / "chrome.exe"
    executable.write_text("")
    monkeypatch.setenv(env, str(tmp_path / "install"))
    assert profiles.browser_executable("chrome") == str(executable)


def test_windows_executable_paths_with_spaces_and_unicode(tmp_path, monkeypatch):
    windows(monkeypatch)
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    root = tmp_path / "Öffentliche Dateien" / "Google/Chrome/Application"
    root.mkdir(parents=True)
    executable = root / "chrome.exe"
    executable.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Öffentliche Dateien"))
    assert profiles.browser_executable("chrome") == str(executable)


def test_windows_missing_executable_is_actionable(tmp_path, monkeypatch):
    windows(monkeypatch)
    for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        monkeypatch.setenv(name, str(tmp_path / "empty"))
    with pytest.raises(BrowserUnavailable, match="Install it normally"):
        profiles.browser_executable("brave")


def test_windows_firefox_profiles_use_appdata(tmp_path, monkeypatch):
    windows(monkeypatch)
    root = tmp_path / "Mozilla/Firefox"
    profile = root / "work.profile"
    profile.mkdir(parents=True)
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=work\nPath=work.profile\nIsRelative=1\nDefault=1\n"
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert profiles.firefox_profile() == str(profile)
    assert profiles.firefox_profile("work") == str(profile)


def test_opera_rejects_explicit_profile_selection():
    with pytest.raises(BrowserUnavailable, match="Opera profile selection is not supported"):
        profiles.launch_arguments("opera", "Profile 1")


def test_launch_success_alone_is_not_authentication(monkeypatch):
    monkeypatch.setattr(profiles, "default_browser", lambda: "chrome")
    monkeypatch.setattr(profiles, "browser_executable", lambda name: "/fake/chrome")
    monkeypatch.setattr(
        profiles.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace()
    )

    def failing(browser, profile=None):
        raise BrowserUnavailable("no YouTube login found in that profile")

    monkeypatch.setattr(auth, "_find_browser_cookie_header", failing)
    with pytest.raises(BrowserUnavailable, match="no YouTube login"):
        profiles.NativeBrowser(ready=lambda: None).observe(timeout=30)
