"""Windows data migration: no personal directories or credentials are read."""
import json

import pytest
from ytm import paths


def locations(tmp_path):
    home = tmp_path / 'User With Spaces é'
    root = home / 'AppData' / 'Local' / 'ytm'
    roots = {'config': root, 'state': root / 'state', 'cache': root / 'cache'}
    return home, roots


def test_migration_preview_apply_retry_and_auth_preservation(tmp_path):
    home, roots = locations(tmp_path)
    cfg = home / '.config' / 'ytm'
    cfg.mkdir(parents=True)
    (cfg / 'config.toml').write_text('[audio]\nvolume=40\n')
    (cfg / 'auth.json').write_text('DO NOT READ OR COPY')
    old = home / '.local/state/ytm'
    old.mkdir(parents=True)
    library = old / 'playlists.json'
    library.write_text(json.dumps({'playlists': []}))
    roots['config'].mkdir(parents=True)
    tombstone = roots['config'] / 'session.json'
    tombstone.write_text('TOMBSTONE MUST STAY')
    kwargs = dict(home=home, environ={}, roots=roots)
    plan = paths.migrate(**kwargs)
    assert len(plan) == 2 and all(i['status']=='copy' for i in plan)
    assert not (roots['state'] / 'playlists.json').exists()
    assert all(i['status']=='copied' for i in paths.migrate(apply=True, **kwargs))
    assert library.read_bytes() == (roots['state']/'playlists.json').read_bytes()
    assert tombstone.read_text() == 'TOMBSTONE MUST STAY'
    assert not (roots['config']/'auth.json').exists()
    assert all(i['status']=='identical' for i in paths.migrate(apply=True, **kwargs))


def test_conflict_preflight_keeps_every_original(tmp_path):
    home, roots = locations(tmp_path)
    old = home / '.config/ytm'
    old.mkdir(parents=True)
    (old/'config.toml').write_text('[audio]\nvolume=40')
    roots['config'].mkdir(parents=True)
    (roots['config']/'config.toml').write_text('[audio]\nvolume=50')
    with pytest.raises(paths.MigrationError, match='conflicting'):
        paths.migrate(apply=True, home=home, environ={}, roots=roots)
    assert '40' in (old/'config.toml').read_text()
    assert '50' in (roots['config']/'config.toml').read_text()


def test_corrupt_state_is_not_migrated(tmp_path):
    home, roots = locations(tmp_path)
    old = home / '.local/state/ytm'
    old.mkdir(parents=True)
    (old/'playlists.json').write_text('{bad')
    with pytest.raises(paths.MigrationError, match='invalid'):
        paths.migrate(apply=True, home=home, environ={}, roots=roots)
    assert not roots['state'].exists()


def test_windows_resolution_uses_old_until_copy_then_native(tmp_path, monkeypatch):
    home, roots = locations(tmp_path)
    old = home/'.config/ytm'
    old.mkdir(parents=True)
    (old/'config.toml').write_text('[audio]\nvolume=40')
    monkeypatch.setattr(paths, 'legacy_root', lambda kind: old)
    monkeypatch.setattr(paths, 'native_root', lambda kind: roots[kind])
    assert paths.application_path('config','config.toml',platform='win32') == old/'config.toml'
    roots['config'].mkdir(parents=True)
    (roots['config']/'config.toml').write_text('[audio]\nvolume=40')
    assert paths.application_path('config','config.toml',platform='win32') == roots['config']/'config.toml'


def test_xdg_override_is_never_migrated(tmp_path):
    home, roots = locations(tmp_path)
    override=tmp_path/'override'
    (override/'ytm').mkdir(parents=True)
    (override/'ytm/session.json').write_text('{}')
    assert paths.migrate(apply=True,home=home,roots=roots,environ={'XDG_STATE_HOME':str(override)}) == []


def test_publish_failure_preserves_source_and_removes_staging(tmp_path,monkeypatch):
    home,roots=locations(tmp_path)
    old=home/'.config/ytm'
    old.mkdir(parents=True)
    (old/'config.toml').write_text('[audio]\nvolume=40')
    def fail(*args): raise OSError('test disk error')
    monkeypatch.setattr(paths.os,'link',fail)
    with pytest.raises(paths.MigrationError, match='publish'):
        paths.migrate(apply=True,home=home,environ={},roots=roots)
    assert (old/'config.toml').exists()
    assert not (roots['config']/'config.toml').exists()
    assert not list(roots['config'].glob('.ytm-migrate-*'))
