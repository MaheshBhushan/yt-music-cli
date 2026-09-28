"""Windows application-data paths and explicit, non-destructive migration.

Existing files remain usable at legacy locations until migrated. New Windows
state/cache use subdirectories so state/session.json never collides with the
active authentication record. Legacy credentials are intentionally handled by
the auth manager; this migration never reads or copies secrets.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
from contextlib import ExitStack, contextmanager
from pathlib import Path

import platformdirs


class MigrationError(RuntimeError):
    pass


def legacy_root(kind, *, home=None, environ=None):
    home = Path.home() if home is None else Path(home)
    env = os.environ if environ is None else environ
    if kind == 'config':
        return home / '.config' / 'ytm'
    default = home / ('.local/state' if kind == 'state' else '.cache')
    override = env.get('XDG_STATE_HOME' if kind == 'state' else 'XDG_CACHE_HOME')
    return Path(override or default) / 'ytm'


def native_root(kind):
    root = Path(platformdirs.user_config_path('ytm', appauthor=False))
    return root if kind == 'config' else root / kind


def application_path(kind, name='', *, platform=None):
    old = legacy_root(kind) / name
    if (platform or sys.platform) != 'win32':
        return old
    # Existing XDG overrides remain authoritative on Windows too.
    override = {'state': 'XDG_STATE_HOME', 'cache': 'XDG_CACHE_HOME'}.get(kind)
    if override and os.environ.get(override):
        return old
    target = native_root(kind) / name
    return old if not target.exists() and old.exists() else target


def _digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').digest()


def _validate(path, kind):
    try:
        if kind == 'config':
            with path.open('rb') as file:
                tomllib.load(file)
        elif path.suffix == '.json':
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('expected object')
            if path.name == 'playlists.json':
                entries = data.get('playlists')
                if not isinstance(entries, list) or any(
                    not isinstance(p, dict) or not isinstance(p.get('playlist_id'), str)
                    or not isinstance(p.get('tracks', []), list) for p in entries
                ):
                    raise ValueError('invalid playlists')
    except (ValueError, OSError) as exc:
        raise MigrationError(f'Cannot migrate unreadable or invalid file: {path.name}. Original kept.') from exc


def migration_plan(*, home=None, environ=None, roots=None):
    """Inventory only; conflicts are explicit and no destination is replaced."""
    roots = roots or {k: native_root(k) for k in ('config', 'state', 'cache')}
    names = {'config': ['config.toml'],
             'state': ['session.json', 'state.json', 'playlists.json', 'visitor.json', 'update-check.json']}
    items = []
    env = os.environ if environ is None else environ
    for kind in ('config', 'state', 'cache'):
        override = {'state': 'XDG_STATE_HOME', 'cache': 'XDG_CACHE_HOME'}.get(kind)
        if override and env.get(override):
            continue  # do not override an explicit storage policy
        source_root = legacy_root(kind, home=home, environ=env)
        target_root = Path(roots[kind])
        if source_root.resolve() == target_root.resolve():
            continue
        candidates = [source_root / n for n in names.get(kind, [])]
        if kind == 'cache' and (source_root / 'tracks').is_dir():
            candidates = sorted((source_root / 'tracks').iterdir())
        for source in candidates:
            if source.name.startswith('.') or (source.is_dir() and not source.is_symlink()):
                continue
            if not source.exists() and not source.is_symlink():
                continue
            target = target_root / source.relative_to(source_root)
            unsafe = any(p.is_symlink() for p in (source, *source.parents, target, *target.parents))
            if unsafe or not source.is_file():
                status = 'unsafe'
            else:
                _validate(source, kind)
                status = ('identical' if target.is_file() and _digest(source) == _digest(target)
                          else 'conflict') if target.exists() else 'copy'
            items.append({'kind': kind, 'source': str(source), 'target': str(target), 'status': status})
    return items


@contextmanager
def _file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same stable sidecar naming as state/playlist writers. Call only on safe paths.
    with path.with_name(path.name + '.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def migrate(*, apply=False, home=None, environ=None, roots=None):
    """Copy managed data; retain legacy backups and never migrate auth tokens.

    Per-file commits are atomic and no-clobber. A disk failure may leave a
    partial migration; a retry recognizes already-identical destinations.
    Close other YTM instances first: older processes retain resolved paths.
    """
    kwargs = dict(home=home, environ=environ, roots=roots)
    items = migration_plan(**kwargs)
    if not apply:
        return items
    if any(i['status'] in ('conflict', 'unsafe') for i in items):
        raise MigrationError('Migration has conflicting or unsafe paths. Nothing copied; originals kept.')
    lock_paths = sorted({Path(i[key]) for i in items for key in ('source', 'target')}, key=str)
    if any(p.with_name(p.name + '.lock').is_symlink() for p in lock_paths):
        raise MigrationError('Migration lock must not be a symlink.')
    with ExitStack() as stack:
        for path in lock_paths:
            stack.enter_context(_file_lock(path))
        # Recheck conflicts/content under locks, before publishing anything.
        updated = migration_plan(**kwargs)
        if {(i['source'], i['target']) for i in updated} != {(i['source'], i['target']) for i in items}:
            raise MigrationError('Migration inventory changed; retry with all YTM instances closed.')
        items = updated
        if any(i['status'] in ('conflict', 'unsafe') for i in items):
            raise MigrationError('Files changed before migration. Nothing copied; retry after closing YTM.')
        for item in items:
            if item['status'] != 'copy':
                continue
            source, target = Path(item['source']), Path(item['target'])
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.ytm-migrate-', dir=target.parent)
            try:
                before = _digest(source)
                with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
                    shutil.copyfileobj(inp, out)
                    out.flush(); os.fsync(out.fileno())
                if _digest(Path(temporary)) != before or _digest(source) != before:
                    raise MigrationError('Source changed during migration; originals kept. Retry with YTM closed.')
                # Hard-link publication is atomic and fails if target appeared.
                # NTFS supports this; unsupported filesystems fail without clobber.
                os.link(temporary, target)
                item['status'] = 'copied'
            except OSError as exc:
                raise MigrationError('Migration could not publish a file. Originals kept; completed copies are safe to retry.') from exc
            finally:
                Path(temporary).unlink(missing_ok=True)
    return items
