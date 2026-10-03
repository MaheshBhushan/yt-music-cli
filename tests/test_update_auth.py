"""Updates and fresh processes retain credentials and explicit logout records."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_auth_migration import headers, oauth_token
from ytm import auth, update
from ytm.authentication.storage import StoredRecord


@pytest.mark.parametrize('kind', ['pip', 'pipx', 'uv', 'editable'])
@pytest.mark.parametrize('method', ['browser', 'oauth', 'legacy', 'none'])
def test_update_and_fresh_process_preserve_auth(monkeypatch, kind, method):
    store = auth.session_store()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    if method == 'legacy':
        store.legacy_path.write_text(json.dumps(headers()))
    elif method == 'browser':
        store.save(StoredRecord.browser(headers()), expected_revision=None)
    elif method == 'oauth':
        token = store.path.parent / 'oauth.json'
        token.write_text(json.dumps(oauth_token()))
        auth._write_json_0600(auth._oauth_client_path(token), {
            'client_id': 'test-client', 'client_secret': 'test-secret',
        })
        store.save(StoredRecord.oauth(token), expected_revision=None)
    else:
        store.legacy_path.write_text(json.dumps(headers()))
        store.save(StoredRecord.tombstone(), expected_revision=None)

    before = {p: p.read_bytes() for p in store.path.parent.rglob('*.json')}
    monkeypatch.setattr(update.sys, 'platform', 'linux')
    monkeypatch.setattr(update, '_has_module', lambda name: True)
    commands = []

    def installer(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='updated', stderr='')

    ok, _ = update.upgrade(kind=kind, run=installer, target='99.0.0', verify=lambda: '99.0.0')
    assert ok is (kind != 'editable')
    assert bool(commands) is (kind != 'editable')
    # A fresh interpreter reads the same on-disk credential without network or
    # module caches. Installer execution is stubbed; no real package is changed.
    result = subprocess.run([
        sys.executable, '-c', '''
import sys
from ytm.authentication.manager import AuthManager
from ytm.authentication.storage import SessionStore
manager = AuthManager(SessionStore(sys.argv[1], legacy_path=sys.argv[2]))
status = manager.status(validate=False)
print(status.logged_in, status.method, status.state)
''', str(store.path), str(store.legacy_path),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True, timeout=10)
    expected = 'False None logged_out' if method == 'none' else (
        f'True {"browser" if method == "legacy" else method} not_checked'
    )
    assert result.stdout.strip() == expected
    assert {p: p.read_bytes() for p in store.path.parent.rglob('*.json')} == before
