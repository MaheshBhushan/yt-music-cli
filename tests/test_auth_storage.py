"""The active-record store: atomic writes, revisions, tombstones, safety."""

import json
import os
import stat

import pytest

from ytm import auth
from ytm.authentication import storage as storage_mod
from ytm.authentication.errors import AuthInvalidFormat, AuthStorageError
from ytm.authentication.storage import SessionStore, StoredRecord


def headers(**extra):
    base = {
        "cookie": "SID=x; __Secure-3PAPISID=secret",
        "x-goog-authuser": "0",
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }
    base.update(extra)
    return base


def browser_record(**extra):
    return StoredRecord.browser(headers(**extra))


# -- S01: private by construction ------------------------------------------


def test_a_new_record_gets_a_private_directory_and_file(tmp_path):
    path = tmp_path / "cfg" / "session.json"
    store = SessionStore(path)
    store.save(browser_record(), expected_revision=None)
    assert stat.S_IMODE(os.stat(tmp_path / "cfg").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not list((tmp_path / "cfg").glob(".*tmp*"))


def test_the_record_round_trips(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    record = store.save(browser_record(), expected_revision=None)
    loaded = store.load()
    assert loaded.revision == record.revision
    assert loaded.method == "browser"
    assert loaded.headers["cookie"] == "SID=x; __Secure-3PAPISID=secret"
    assert loaded.verified_at


def test_a_brand_user_round_trips(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    record = StoredRecord.browser(headers(), user="brand-page-id")
    store.save(record, expected_revision=None)
    assert store.load().user == "brand-page-id"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_an_existing_record_keeps_its_mode_on_replace(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    first = store.save(browser_record(), expected_revision=None)
    second = store.save(StoredRecord.browser(headers(cookie="SID=y; __Secure-3PAPISID=next")),
                        expected_revision=first.revision)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load().revision == second.revision


# -- S02/S03: failures never destroy the previous record --------------------


def test_a_failed_replace_preserves_the_previous_record_and_cleans_up(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "session.json")
    first = store.save(browser_record(), expected_revision=None)

    def fail_replace(*args):
        raise OSError("disk full")

    monkeypatch.setattr(storage_mod.os, "replace", fail_replace)
    with pytest.raises(AuthStorageError):
        store.save(StoredRecord.browser(headers(cookie="SID=new; __Secure-3PAPISID=new")),
                   expected_revision=first.revision)
    assert store.load().revision == first.revision
    assert not [p.name for p in store.path.parent.iterdir() if p.name.startswith(".")]


def test_a_symlinked_record_is_refused_on_read_and_write(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(browser_record().to_payload()))
    path = tmp_path / "session.json"
    path.symlink_to(real)
    store = SessionStore(path)
    with pytest.raises(AuthStorageError):
        store.load()
    with pytest.raises(AuthStorageError):
        store.save(browser_record(), expected_revision=None)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="unreadable directory test")
def test_an_unreadable_directory_fails_clearly(tmp_path):
    blocked = tmp_path / "cfg"
    blocked.mkdir()
    blocked.chmod(0)
    try:
        with pytest.raises(AuthStorageError):
            SessionStore(blocked / "session.json").save(browser_record(), expected_revision=None)
    finally:
        blocked.chmod(0o700)


# -- S04/S10: revisions and tombstones --------------------------------------


def test_a_stale_commit_is_rejected_and_cannot_resurrect_a_session(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    first = store.save(browser_record(), expected_revision=None)
    store.logout(expected_revision=first.revision)
    assert store.load().method == "none"
    with pytest.raises(AuthStorageError, match="changed while this login"):
        store.save(browser_record(), expected_revision=first.revision)
    assert store.load().method == "none"


def test_logout_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    first = store.save(browser_record(), expected_revision=None)
    tombstone = store.logout(expected_revision=first.revision)
    again = store.logout(expected_revision=tombstone.revision)
    assert again.method == "none"
    assert again.revision != tombstone.revision


def test_a_tombstone_blocks_a_legacy_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_PATH", tmp_path / "auth.json")
    monkeypatch.setattr(auth, "SESSION_PATH", tmp_path / "session.json")
    auth.AUTH_PATH.write_text(json.dumps(headers()))
    auth.auth_manager().logout()
    with pytest.raises(auth.AuthMissing):
        auth.client()
    assert auth.active_record().method == "none"


# -- malformed records -------------------------------------------------------


def test_an_unreadable_record_is_replaced_by_an_explicit_login(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.path.write_text("{broken")
    record = store.save(browser_record(), expected_revision=None)
    assert store.load().revision == record.revision


def test_a_stale_commit_cannot_overwrite_an_unreadable_record(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    store.path.write_text("{broken")
    with pytest.raises(AuthStorageError, match="changed while this login"):
        store.save(browser_record(), expected_revision="an-old-revision")


def test_a_malformed_record_does_not_fall_through_to_legacy(tmp_path):
    store = SessionStore(tmp_path / "session.json", legacy_path=tmp_path / "auth.json")
    store.path.write_text("{not json")
    (tmp_path / "auth.json").write_text(json.dumps(headers()))
    with pytest.raises(AuthInvalidFormat):
        store.load()


@pytest.mark.parametrize("payload", [
    [],
    {"schema_version": 99, "revision": "r", "method": "browser"},
    {"schema_version": 1, "method": "browser"},
    {"schema_version": 1, "revision": "r", "method": "mystery"},
    {"schema_version": 1, "revision": "r", "method": "browser", "headers": {}},
    {"schema_version": 1, "revision": "r", "method": "browser",
     "headers": {"cookie": "SID=x", "x-goog-authuser": "0"}},
    {"schema_version": 1, "revision": "r", "method": "oauth"},
])
def test_broken_payloads_are_invalid_format(tmp_path, payload):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(AuthInvalidFormat):
        SessionStore(path).load()


def test_unrelated_and_dangerous_headers_never_survive(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    record = StoredRecord.browser({
        "cookie": "SID=x; __Secure-3PAPISID=secret",
        "x-goog-authuser": "0",
        "host": "music.youtube.com",
        "content-length": "12",
        "sec-fetch-mode": "cors",
        "x-request-id": "abc",
        "x-custom": "nope",
    })
    store.save(record, expected_revision=None)
    saved = store.load().headers
    assert set(saved) <= storage_mod.session_mod.ALLOWED_HEADERS
    assert "host" not in saved and "x-custom" not in saved

    with pytest.raises(AuthInvalidFormat):
        StoredRecord.browser({**headers(), "cookie": "SID=x; __Secure-3PAPISID=s\nneaky"})


# -- S14: secrets never leak through repr or status --------------------------


def test_reprs_never_show_secrets(tmp_path):
    record = browser_record()
    assert "secret" not in repr(record)
    assert "SID=x" not in repr(record)
    assert "SAPISIDHASH" not in repr(record)
    session = storage_mod.session_mod.build_session(headers())
    assert "secret" not in repr(session)


# -- the legacy inventory -----------------------------------------------------


def test_managed_inventory_lists_every_credential_copy(tmp_path):
    store = SessionStore(tmp_path / "session.json", legacy_path=tmp_path / "auth.json")
    names = {path.name for path in store.managed_credential_paths()}
    assert names == {
        "session.json", "auth.json", "auth.source.json", "cookies.txt",
        "oauth_client.json", "oauth_desktop_client.json",
    }


def test_ordinary_reads_never_rewrite_the_legacy_file(tmp_path):
    legacy = tmp_path / "auth.json"
    legacy.write_text(json.dumps(headers()))
    before = legacy.stat().st_mtime_ns
    store = SessionStore(tmp_path / "session.json", legacy_path=legacy)
    assert store.load() is None  # legacy files are not records
    assert store.read_legacy()["cookie"].startswith("SID=x")
    assert legacy.stat().st_mtime_ns == before
