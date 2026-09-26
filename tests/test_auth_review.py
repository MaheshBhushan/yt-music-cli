"""Regressions found while reviewing the browser-session redesign."""

import pytest
import requests
from ytmusicapi.exceptions import YTMusicServerError

from ytm import auth
from ytm.authentication import browser_login
from ytm.authentication.manager import AuthManager
from ytm.authentication.session import build_session
from ytm.authentication.storage import SessionStore, StoredRecord


def headers(value='fake'):
    return {'cookie': f'__Secure-3PAPISID={value}', 'x-goog-authuser': '0'}


def test_public_factory_never_reads_storage():
    class Forbidden:
        def effective(self):
            pytest.fail('public clients must never read credentials')
    sentinel = object()
    manager = AuthManager(Forbidden(), anonymous_client_factory=lambda: sentinel)
    assert manager.get_client() is sentinel


@pytest.mark.parametrize('account', [None, {}, {'accountName': ''}, {'accountName': 123}])
def test_status_does_not_validate_an_empty_or_malformed_account(tmp_path, account):
    store = SessionStore(tmp_path / 'session.json')
    store.save(StoredRecord.browser(headers()), expected_revision=None)
    manager = AuthManager(store, client_factory=lambda *a: object(), probe=lambda c: account)
    assert manager.status().state == 'unknown'


@pytest.mark.parametrize('error', [
    requests.ConnectionError('SECRET'),
    YTMusicServerError('Server returned HTTP 403: SECRET'),
    KeyError('SECRET'),
])
def test_status_handles_construction_failures_without_secrets(tmp_path, error):
    store = SessionStore(tmp_path / 'session.json')
    store.save(StoredRecord.browser(headers()), expected_revision=None)
    def factory(*args):
        raise error
    status = AuthManager(store, client_factory=factory).status()
    assert status.state == 'unknown'
    assert 'SECRET' not in status.detail


def test_oauth_candidate_failure_preserves_previous_files(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, 'SESSION_PATH', tmp_path / 'session.json')
    path = tmp_path / 'auth.json'
    path.write_text('OLD TOKEN')
    client = tmp_path / 'oauth_client.json'
    client.write_text('OLD CLIENT')
    def setup(**kwargs):
        target = kwargs['path']
        target.write_text('NEW TOKEN')
        (target.parent / 'oauth_client.json').write_text('NEW CLIENT')
        return target
    monkeypatch.setattr(auth, 'oauth_setup', setup)
    def rejected(*a, **kw):
        raise auth.AuthExpired('rejected')
    monkeypatch.setattr(auth, '_oauth_client', rejected)
    with pytest.raises(auth.AuthExpired):
        auth.oauth_login(path=path)
    assert path.read_text() == 'OLD TOKEN'
    assert client.read_text() == 'OLD CLIENT'


def test_expired_deadline_cannot_capture_a_candidate():
    with pytest.raises(auth.LoginTimedOut):
        browser_login.wait_for_capture(lambda: build_session(headers()), deadline=1, now=lambda: 2)


def test_capture_ignores_headers_from_other_account():
    class Context:
        def cookies(self, url):
            assert url.startswith('https://music.youtube.com/youtubei/v1/')
            return [{'name': '__Secure-3PAPISID', 'value': 'current'}]
    old = {**headers('old'), 'x-goog-authuser': '0', 'x-goog-pageid': 'old-brand'}
    observed = browser_login.PageObservation('2', visitor_data='current-visitor')
    result = browser_login.PlaywrightBrowser()._candidate(Context(), observed, [old])
    assert result.headers['cookie'] == '__Secure-3PAPISID=current'
    assert result.headers['x-goog-authuser'] == '2'
    assert 'x-goog-pageid' not in result.headers


def test_import_snapshot_precedes_extraction(monkeypatch):
    calls = []
    class Manager:
        def expected_revision(self):
            calls.append('snapshot')
            return 'r'
        def validate_candidate(self, candidate):
            return candidate
        def save_verified(self, value, **kwargs):
            return value
    monkeypatch.setattr(auth, 'auth_manager', lambda *a: Manager())
    monkeypatch.setattr(auth, '_find_browser_cookie_header',
                        lambda *a, **kw: (calls.append('extract') or '__Secure-3PAPISID=fake', 'chrome', {}))
    auth.import_from_browser('chrome', authuser='0')
    assert calls == ['snapshot', 'extract']


def test_legacy_import_validates_before_replacing(tmp_path, monkeypatch):
    path = tmp_path / 'auth.json'
    path.write_text('OLD SESSION')
    monkeypatch.setattr(auth, '_find_browser_cookie_header', lambda *a, **kw: ('__Secure-3PAPISID=FAKE', 'chrome', {}))
    def rejected(candidate):
        assert candidate != path
        assert path.read_text() == 'OLD SESSION'
        raise requests.ConnectionError('SECRET')
    with pytest.raises(auth.AuthError) as caught:
        auth.from_browser('chrome', path=path, authuser='0', client_factory=rejected)
    assert path.read_text() == 'OLD SESSION'
    assert 'SECRET' not in str(caught.value)


def test_cookie_domains_are_matched_exactly():
    from types import SimpleNamespace
    def cookie(domain, value):
        return SimpleNamespace(name='__Secure-3PAPISID', value=value, domain=domain)
    assert auth._cookie_header_from_jar([cookie('youtube.com.evil.example', 'BAD')]) is None
    assert auth._cookie_header_from_jar([cookie('notyoutube.com', 'BAD')]) is None
    assert auth._cookie_header_from_jar([cookie('.youtube.com', 'GOOD')]) == '__Secure-3PAPISID=GOOD'


def test_new_session_keeps_brand_identity_with_old_visitor_cache(monkeypatch, tmp_path):
    from ytm import music
    auth.session_store().save(StoredRecord.browser(headers(), user='brand'), expected_revision=None)
    music._write_visitor_id('OLD-VISITOR')
    assert music._seeded_headers(auth.AUTH_PATH) is None


def test_oauth_success_uses_managed_generation_and_logout_removes_it(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(auth, 'SESSION_PATH', tmp_path / 'session.json')
    path = tmp_path / 'auth.json'
    path.write_text('OLD TOKEN')
    def setup(**kwargs):
        target = kwargs['path']
        target.write_text('NEW TOKEN')
        (target.parent / 'oauth_client.json').write_text('NEW CLIENT')
        return target
    monkeypatch.setattr(auth, 'oauth_setup', setup)
    monkeypatch.setattr(auth, '_oauth_client', lambda *a, **kw: SimpleNamespace(get_account_info=lambda: {'accountName': 'Example'}))
    record = auth.oauth_login(path=path)
    from pathlib import Path
    token = Path(record.token_path)
    assert token != path and token.read_text() == 'NEW TOKEN'
    assert path.read_text() == 'OLD TOKEN'
    assert auth._managed_token_path(record, path) == token
    auth.auth_manager(path).logout()
    assert not token.exists()


def test_tui_discards_account_result_if_logout_happens_during_request(monkeypatch):
    import threading
    from ytm.tui.backend import Backend, BackendError
    backend = Backend.__new__(Backend)
    backend._account_lock = threading.RLock()
    backend._account_revision = None
    stamp = ['old']
    monkeypatch.setattr(auth, 'credential_stamp', lambda: stamp[0])
    def handler(args):
        backend._mixes = ['old account result']
        stamp[0] = 'logged-out'
        return {'private': 'old account result'}
    backend._routes = {'playlist_list': handler}
    with pytest.raises(BackendError, match='account changed'):
        backend.request('playlist_list')
    assert backend._mixes is None


def test_oauth_reuses_active_generation_desktop_client(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(auth, 'SESSION_PATH', tmp_path / 'session.json')
    path = tmp_path / 'auth.json'
    seen = []
    def setup(**kwargs):
        seen.append(kwargs['client_file'])
        target = kwargs['path']
        target.write_text('FAKE TOKEN')
        (target.parent / 'oauth_desktop_client.json').write_text('FAKE CLIENT')
        return target
    monkeypatch.setattr(auth, 'oauth_setup', setup)
    monkeypatch.setattr(auth, '_oauth_client', lambda *a, **kw: SimpleNamespace(get_account_info=lambda: {'accountName': 'Example'}))
    first = auth.oauth_login(path=path)
    auth.oauth_login(path=path)
    from pathlib import Path
    assert seen == [None, Path(first.token_path).parent / 'oauth_desktop_client.json']


def test_invalid_utf8_record_has_safe_format_error(tmp_path):
    store = SessionStore(tmp_path / 'session.json')
    store.path.write_bytes(b'\xff')
    with pytest.raises(auth.AuthInvalidFormat, match='valid UTF-8'):
        store.load()
