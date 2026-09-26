"""Normal-profile login never starts automation or selects another cookie store."""
from types import SimpleNamespace

import pytest

from ytm import auth, cli
from ytm.authentication import browser_profiles as profiles
from ytm.authentication.browser_login import PlaywrightBrowser
from ytm.authentication.errors import BrowserUnavailable, LoginCancelled


@pytest.mark.parametrize('identifier,expected', [
    ('google-chrome.desktop', 'chrome'), ('chromium.desktop', 'chromium'),
    ('MSEdgeHTM', 'edge'), ('FirefoxURL-123', 'firefox'), ('com.brave.Browser', 'brave'),
    ('com.vivaldi.Vivaldi', 'vivaldi'), ('opera.desktop', 'opera'), ('net.imput.helium', 'helium'),
])
def test_https_associations(identifier, expected):
    assert profiles.browser_from_identifier(identifier) == expected


def test_unknown_default_never_falls_back_to_another_cookie_store():
    with pytest.raises(BrowserUnavailable):
        profiles.browser_from_identifier('com.apple.Safari')


@pytest.mark.parametrize('browser', ['chrome', 'chromium', 'edge', 'brave', 'vivaldi', 'helium'])
def test_chromium_profiles_are_launched_without_debugging(browser):
    args, selected = profiles.launch_arguments(browser)
    assert selected == 'Default'
    assert args == ['--profile-directory=Default', 'https://music.youtube.com']
    assert not any('remote-debugging' in a or 'user-data-dir' in a for a in args)
    args, selected = profiles.launch_arguments(browser, 'Profile 1')
    assert args[0] == '--profile-directory=Profile 1' and selected == 'Profile 1'


@pytest.mark.parametrize('profile', ['../somewhere', '/tmp/profile', '..', 'Default\n--flag'])
def test_chromium_profile_paths_are_rejected(profile):
    with pytest.raises(BrowserUnavailable):
        profiles.launch_arguments('chrome', profile)


def test_firefox_resolves_installed_default_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.sys, 'platform', 'linux')
    monkeypatch.setattr(profiles.Path, 'home', lambda: tmp_path)
    root = tmp_path / '.mozilla/firefox'
    (root / 'first.default').mkdir(parents=True)
    (root / 'second.default').mkdir()
    (root / 'profiles.ini').write_text('[Profile0]\nName=old\nPath=first.default\nIsRelative=1\nDefault=1\n'
                                     '[Profile1]\nName=work\nPath=second.default\nIsRelative=1\n'
                                     '[InstallABC]\nDefault=second.default\n')
    assert profiles.firefox_profile() == str(root / 'second.default')
    assert profiles.firefox_profile('old') == str(root / 'first.default')
    assert profiles.launch_arguments('firefox', 'work')[0] == ['-profile', str(root / 'second.default'), profiles.MUSIC_ORIGIN]


def test_native_login_uses_only_the_opened_profile(monkeypatch):
    launched, extracted = [], []
    monkeypatch.setattr(profiles, 'default_browser', lambda: 'chrome')
    monkeypatch.setattr(profiles, 'browser_executable', lambda name: '/fake/chrome')
    monkeypatch.setattr(profiles.subprocess, 'Popen', lambda command, **kw: launched.append(command))
    def extract(browser, profile=None):
        extracted.append((browser, profile))
        return '__Secure-3PAPISID=FAKE', browser, {}
    monkeypatch.setattr(auth, '_find_browser_cookie_header', extract)
    runner = profiles.NativeBrowser(profile='Profile 1', authuser='2', ready=lambda: None)
    candidate = runner.observe(timeout=30)
    assert launched == [['/fake/chrome', '--profile-directory=Profile 1', profiles.MUSIC_ORIGIN]]
    assert extracted == [('chrome', 'Profile 1')]
    assert candidate.headers['x-goog-authuser'] == '2'


def test_native_login_missing_terminal_fails_before_launch(monkeypatch):
    monkeypatch.setattr(profiles.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(profiles, 'default_browser', lambda: pytest.fail('must not discover or launch'))
    with pytest.raises(LoginCancelled):
        profiles.NativeBrowser().observe(timeout=30)


@pytest.mark.parametrize('name,engine,channel', [
    ('firefox', 'firefox', None), ('webkit', 'webkit', None), ('chromium', 'chromium', None),
    ('chrome', 'chromium', 'chrome'), ('edge', 'chromium', 'msedge'),
])
def test_playwright_engine_selection(name, engine, channel):
    browser = PlaywrightBrowser(channel=name)
    assert (browser.engine, browser.channel) == (engine, channel)


@pytest.mark.parametrize('engine', ['firefox', 'webkit'])
def test_nonchromium_engine_never_receives_chrome_channels(engine):
    calls = []
    class Launcher:
        def launch(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError('not installed')
    with pytest.raises(BrowserUnavailable):
        PlaywrightBrowser(engine=engine)._launch(Launcher(), {'headless': False})
    assert calls == [{'headless': False}]


@pytest.mark.parametrize('argv', [
    ['login', '--install-browser', '--method', 'oauth'],
    ['login', '--install-browser', '--from-browser', 'chrome'],
    ['login', '--install-browser', '--browser', 'brave'],
    ['login', '--timeout', 'nan'], ['login', '--timeout', 'inf'],
    ['login', '--method', 'playwright', '--browser', 'brave'],
])
def test_invalid_combinations_never_launch(argv):
    args = cli.build_parser().parse_args(argv)
    assert cli._login_usage_error(args)
