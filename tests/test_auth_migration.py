"""Credential discovery across the new record and the legacy auth file.

Ordinary reads never rewrite the legacy file; a new record (including a
tombstone) takes precedence; an explicit sign-in activates its method.
"""

import json

import pytest

from ytm.authentication.manager import AuthManager
from ytm.authentication.storage import SessionStore, StoredRecord


def headers(**extra):
    base = {
        "cookie": "SID=legacy; __Secure-3PAPISID=secret",
        "x-goog-authuser": "1",
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }
    base.update(extra)
    return base


def oauth_token():
    return {
        "scope": "https://www.googleapis.com/auth/youtube",
        "token_type": "Bearer",
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": 9999999999,
        "expires_in": 999999,
    }


class ProbeClient:
    def __init__(self, account=None, error=None):
        self.account = account if account is not None else {"accountName": "Legacy Listener"}
        self.error = error

    def get_account_info(self):
        if self.error is not None:
            raise self.error
        return self.account


def manager(legacy, *, account=None, error=None):
    store = SessionStore(legacy.parent / "session.json", legacy_path=legacy)
    return AuthManager(
        store,
        client_factory=lambda _headers, user=None: ProbeClient(account=account, error=error),
    )


def test_a_legacy_browser_file_is_discovered_and_never_rewritten(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers()))
    before = legacy.read_bytes()
    mgr = manager(legacy)
    status = mgr.status(validate=False)
    assert (status.logged_in, status.method, status.state) == (True, "browser", "not_checked")
    assert legacy.read_bytes() == before
    assert not mgr.store.path.exists()  # no record is created by reading


def test_a_legacy_oauth_file_is_discovered(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(oauth_token()))
    status = manager(legacy).status(validate=False)
    assert (status.logged_in, status.method, status.state) == (True, "oauth", "not_checked")


def test_a_legacy_oauth_record_without_its_client_file_is_invalid_not_logged_out(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(oauth_token()))
    status = manager(legacy).status()
    assert (status.logged_in, status.method, status.state) == (False, "oauth", "invalid")
    assert "ytm auth" in status.detail or "ytm login" in status.detail


def test_a_legacy_oauth_record_is_validated_with_its_client(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(oauth_token()))
    store = SessionStore(legacy.parent / "session.json", legacy_path=legacy)
    mgr = AuthManager(
        store,
        client_factory=lambda _headers, user=None: ProbeClient(),
        oauth_client_factory=lambda record: ProbeClient(account={"accountName": "OAuth Listener"}),
    )
    status = mgr.status()
    assert (status.logged_in, status.method, status.state) == (True, "oauth", "valid")
    assert status.account_name == "OAuth Listener"


def test_a_legacy_record_is_validated_through_the_account_probe(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers()))
    status = manager(legacy, account={"accountName": "Legacy Listener"}).status()
    assert (status.logged_in, status.method, status.state) == (True, "browser", "valid")
    assert status.account_name == "Legacy Listener"


def test_a_broken_legacy_file_reports_invalid_not_logged_out(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text("{not json")
    status = manager(legacy).status(validate=False)
    assert (status.logged_in, status.state) == (False, "invalid")
    assert "ytm login" in status.detail
    assert not manager(legacy).has_credentials()

    legacy.write_text(json.dumps(["not", "an", "object"]))
    assert manager(legacy).status(validate=False).state == "invalid"


def test_a_legacy_file_without_the_signing_cookie_reports_invalid(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps({"cookie": "SID=only"}))
    assert manager(legacy).status(validate=False).state == "invalid"


def test_a_new_browser_record_takes_precedence_over_legacy(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers(cookie="SID=old; __Secure-3PAPISID=old")))
    mgr = manager(legacy)
    mgr.store.save(StoredRecord.browser(headers(cookie="SID=new; __Secure-3PAPISID=new")),
                   expected_revision=None)
    status = mgr.status(validate=False)
    assert status.method == "browser"
    assert mgr.store.effective().headers["cookie"].endswith("PAPISID=new")


def test_a_tombstone_outranks_a_legacy_file_for_status_and_clients(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers()))
    mgr = manager(legacy)
    mgr.logout()
    status = mgr.status(validate=False)
    assert (status.logged_in, status.state) == (False, "logged_out")
    with pytest.raises(Exception):
        mgr.get_client(require_auth=True)


# -- S06/S07: one method must not destroy the other --------------------------


def test_a_successful_browser_commit_preserves_legacy_oauth_files(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(oauth_token()))
    client_file = tmp_path / "oauth_client.json"
    client_file.write_text(json.dumps({"client_id": "id", "client_secret": "secret"}))
    before = legacy.read_bytes()

    mgr = manager(legacy)
    mgr.store.save(StoredRecord.browser(headers()), expected_revision=None)

    assert legacy.read_bytes() == before
    assert json.loads(client_file.read_text())["client_id"] == "id"
    assert mgr.status(validate=False).method == "browser"


def test_an_explicit_oauth_activation_beats_an_older_browser_record(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(oauth_token()))
    mgr = manager(legacy)
    mgr.store.save(StoredRecord.browser(headers()), expected_revision=None)

    mgr.store.save(StoredRecord.oauth(legacy), expected_revision=mgr.store.load().revision)

    assert mgr.status(validate=False).method == "oauth"
    assert mgr.store.effective().token_path == str(legacy)


def test_logout_removes_every_managed_copy_but_keeps_the_tombstone(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers()))
    (tmp_path / "auth.source.json").write_text("{}")
    (tmp_path / "cookies.txt").write_text("cookies")
    (tmp_path / "oauth_client.json").write_text("{}")
    (tmp_path / "oauth_desktop_client.json").write_text("{}")

    mgr = manager(legacy)
    result = mgr.logout()

    assert result.revision
    assert result.failed == ()
    assert mgr.store.path.exists() and mgr.store.load().method == "none"
    remaining = [p.name for p in tmp_path.iterdir()]
    assert remaining == ["session.json"] or set(remaining) <= {"session.json", "session.json.lock"}


def test_logout_reports_a_file_it_could_not_remove(tmp_path, monkeypatch):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers()))
    mgr = manager(legacy)

    def fail_unlink(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.unlink", fail_unlink)
    result = mgr.logout()
    assert result.failed and result.failed[0][0].endswith("auth.json")
    assert mgr.store.load().method == "none"  # the local revoke still happened


def test_repeated_logout_is_idempotent_and_touches_nothing_else(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers()))
    (tmp_path / "playlists.json").write_text("[]")  # local data, not a credential
    mgr = manager(legacy)
    first = mgr.logout()
    second = mgr.logout()
    assert second.revision != first.revision
    assert second.failed == ()
    assert (tmp_path / "playlists.json").read_text() == "[]"
    assert mgr.status().state == "logged_out"


# -- production defaults: the legacy file belongs to the same store -----------


def test_the_default_store_and_manager_see_the_legacy_file():
    """The manager must not report "logged out" while the music layer uses a
    legacy auth.json: one credential, one answer."""
    from ytm import auth

    auth.AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth.AUTH_PATH.write_text(json.dumps(headers()))

    store = auth.session_store()
    assert store.legacy_path == auth.AUTH_PATH
    record = store.effective()
    assert record is not None and record.method == "browser"
    assert store.load() is None  # reading the legacy file creates no record

    status = auth.auth_manager().status(validate=False)
    assert (status.logged_in, status.method, status.state) == (True, "browser", "not_checked")


def test_the_default_logout_removes_the_legacy_copy():
    from ytm import auth

    auth.AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth.AUTH_PATH.write_text(json.dumps(headers()))
    (auth.AUTH_PATH.parent / "auth.source.json").write_text("{}")
    (auth.AUTH_PATH.parent / "cookies.txt").write_text("cookies")

    auth.auth_manager().logout()

    assert not auth.AUTH_PATH.exists()
    assert not (auth.AUTH_PATH.parent / "auth.source.json").exists()
    assert not (auth.AUTH_PATH.parent / "cookies.txt").exists()
    assert auth.active_record().method == "none"
