"""Authentication diagnostics containing only allowlisted metadata.

Never persist exception messages, browser output, profiles, URLs, cookies,
headers, account names or tokens. Classify raw errors in memory instead.
"""
import contextvars
import functools
import json
import os
import platform
import sys
import time
import uuid
from importlib.metadata import PackageNotFoundError, version

from ytm.paths import application_path

_CURRENT = contextvars.ContextVar('ytm_auth_diagnostics', default=None)
_CODES = frozenset({
    'started', 'succeeded', 'failed', 'profile_missing', 'browser_missing',
    'database_copy_failed', 'database_locked', 'permission_denied',
    'app_bound_encryption', 'decryption_failed', 'extraction_failed',
    'AuthMissing', 'AuthExpired', 'AuthInvalidFormat', 'AuthStorageError',
    'BrowserUnavailable', 'LoginCancelled', 'LoginTimedOut',
    'AccountSelectionError', 'SessionVerificationUnavailable',
    'no_youtube_session', 'cookies_available', 'validating', 'validated', 'stored',
})
_BROWSERS = frozenset({'chrome','chromium','edge','msedge','firefox','safari','brave','vivaldi','opera','helium'})


def event(code, *, browser=None, profile_selected=False, errno=None, winerror=None):
    path = _CURRENT.get()
    if path is None or code not in _CODES:
        return
    data = {'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'code': code}
    if browser in _BROWSERS:
        data['browser'] = browser
    data['explicit_profile'] = bool(profile_selected)
    for name, value in (('errno', errno), ('winerror', winerror)):
        if type(value) is int:
            data[name] = value
    try:
        with path.open('a', encoding='utf-8') as file:
            file.write(json.dumps(data) + '\n')
    except OSError:
        pass


def start():
    directory = application_path('state', 'logs')
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f'auth-{time.time_ns()}-{uuid.uuid4().hex[:8]}.jsonl'
        with path.open('x', encoding='utf-8') as file:
            if os.name != 'nt':
                os.chmod(path, 0o600)
            versions = {}
            for name in ('ytm', 'yt-dlp', 'ytmusicapi'):
                try:
                    versions[name] = version(name)
                except PackageNotFoundError:
                    versions[name] = 'unavailable'
            file.write(json.dumps({'code':'environment', 'os':sys.platform,
                                   'python':platform.python_version(), 'versions':versions})+'\n')
        # Each file contains only a bounded number of small classified events.
        for old in sorted(directory.glob('auth-*.jsonl'), reverse=True)[10:]:
            if not old.is_symlink() and time.time() - old.stat().st_mtime > 600:
                old.unlink(missing_ok=True)
        return path
    except OSError:
        return None


def traced(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        path = start()
        token = _CURRENT.set(path)
        event('started')
        try:
            result = function(*args, **kwargs)
            event('succeeded')
            return result
        except BaseException as exc:
            event('failed')
            # Known exception types identify validation/storage/network stages
            # without persisting arbitrary exception text or class names.
            from ytm.authentication import errors
            for name in ('AuthMissing', 'AuthExpired', 'AuthInvalidFormat', 'AuthStorageError',
                         'BrowserUnavailable', 'LoginCancelled', 'LoginTimedOut',
                         'AccountSelectionError', 'SessionVerificationUnavailable'):
                if isinstance(exc, getattr(errors, name)):
                    event(name)
                    break
            if path is not None:
                print(f'Authentication diagnostic log: {path}', file=sys.stderr)
            else:
                print('Authentication diagnostic log could not be created.', file=sys.stderr)
            raise
        finally:
            _CURRENT.reset(token)
    return run
