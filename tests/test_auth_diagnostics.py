"""Real log writes with injected failures; no browser or credentials touched."""
import json

import pytest
from ytm import auth
from ytm.authentication import diagnostics
from ytm.authentication.errors import AuthExpired


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, 'application_path', lambda *args: tmp_path)
    return tmp_path


@pytest.mark.parametrize(('message','code','reason'), [
    ('Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271', 'database_copy_failed', 'close the browser completely'),
    ('failed to decrypt with DPAPI', 'decryption_failed', 'could not be decrypted'),
    ('v20 app-bound encryption unsupported', 'app_bound_encryption', 'App-Bound Encryption'),
    ('database is locked', 'database_locked', 'locked'),
    ('unexpected extractor failure', 'extraction_failed', 'extraction failed'),
])
def test_classified_failure_never_logs_raw_secrets(log_dir, monkeypatch, capsys, message, code, reason):
    secret = 'COOKIE_SECRET_DO_NOT_LOG'
    def extract(*args, logger=None, **kwargs):
        logger.warning('authorization: '+secret)
        raise RuntimeError(message+' cookie='+secret)
    monkeypatch.setattr(auth, 'extract_cookies_from_browser', extract)
    @diagnostics.traced
    def run():
        header, why = auth._extract_browser_cookie_header('chrome', profile='PRIVATE_PROFILE')
        assert header is None and reason in why
        raise auth.AuthError('PRIVATE_ACCOUNT '+secret)
    with pytest.raises(auth.AuthError):
        run()
    files = list(log_dir.glob('auth-*.jsonl'))
    assert len(files) == 1
    text = files[0].read_text()
    assert code in text
    assert secret not in text and 'PRIVATE_' not in text
    assert str(files[0]) in capsys.readouterr().err
    for line in text.splitlines(): json.loads(line)


def test_warning_explains_wrapped_exception(log_dir, monkeypatch):
    def extract(*args, logger=None, **kwargs):
        logger.warning('failed to decrypt with DPAPI')
        raise AttributeError('object has no attribute decode')
    monkeypatch.setattr(auth, 'extract_cookies_from_browser', extract)
    assert 'could not be decrypted' in auth._extract_browser_cookie_header('chrome')[1]


def test_validation_error_has_safe_type_and_logging_failure_does_not_mask(log_dir, monkeypatch):
    @diagnostics.traced
    def run(): raise AuthExpired('secret reason')
    with pytest.raises(AuthExpired): run()
    text = next(log_dir.glob('auth-*')).read_text()
    assert 'AuthExpired' in text and 'secret reason' not in text
    monkeypatch.setattr(diagnostics, 'start', lambda: None)
    with pytest.raises(AuthExpired): run()


def test_failed_import_does_not_claim_browser_is_logged_out():
    message = auth._no_browser_session_error({'chrome': 'cookie database locked'})
    assert 'may still be valid' in message
    assert 'Log in at https' not in message
