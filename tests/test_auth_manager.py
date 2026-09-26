"""AuthManager: validate before commit, then serve the selected identity."""

import requests
import pytest
from ytmusicapi.exceptions import YTMusicServerError

from ytm.authentication import session as session_mod
from ytm.authentication.errors import (
    AccountSelectionError,
    AuthExpired,
    AuthInvalidFormat,
    AuthMissing,
    AuthStorageError,
    SessionVerificationUnavailable,
)
from ytm.authentication.manager import AuthManager
from ytm.authentication.storage import SessionStore


def headers(**extra):
    base = {
        "cookie": "SID=x; __Secure-3PAPISID=secret",
        "x-goog-authuser": "0",
        "authorization": "SAPISIDHASH 0_0",
        "origin": "https://music.youtube.com",
    }
    base.update(extra)
    return base


def candidate(user=None, **extra):
    return session_mod.build_session(headers(**extra), user=user)


_UNSET = object()


class FakeClient:
    def __init__(self, headers=None, user=None, account=_UNSET, error=None):
        self.headers = dict(headers or {})
        self.user = user
        self.account = {"accountName": "Example Listener"} if account is _UNSET else account
        self.error = error
        self.calls = []

    def get_account_info(self):
        self.calls.append("get_account_info")
        if self.error is not None:
            raise self.error
        return self.account


def save_record(manager, **extra):
    """A verified record without running the (possibly failing) probe."""
    session = candidate(**extra)
    verified = session_mod.VerifiedSession(session=session, account_name="Example Listener")
    return manager.save_verified(verified, expected_revision=None)


def manager_for(tmp_path, *, probe=None, client_factory=None, anonymous=None):
    store = SessionStore(tmp_path / "session.json")
    return AuthManager(
        store,
        client_factory=client_factory or (lambda h, user=None: FakeClient(h, user)),
        probe=probe or (lambda client: client.get_account_info()),
        anonymous_client_factory=anonymous or (lambda: FakeClient()),
    )


# -- validate before commit ---------------------------------------------------


def test_a_valid_candidate_becomes_a_verified_session_and_writes_nothing(tmp_path):
    manager = manager_for(tmp_path)
    verified = manager.validate_candidate(candidate())
    assert verified.account_name == "Example Listener"
    assert verified.verified_at
    assert "secret" not in repr(verified)
    assert not manager.store.path.exists()


def test_the_probe_sees_the_captured_headers_and_brand_user(tmp_path):
    seen = {}

    def factory(headers, user=None):
        seen["headers"] = dict(headers)
        seen["user"] = user
        return FakeClient(headers, user)

    manager = manager_for(tmp_path, client_factory=factory)
    manager.validate_candidate(candidate(user="brand-page-id"))
    assert seen["headers"]["cookie"].endswith("__Secure-3PAPISID=secret")
    assert seen["headers"]["x-goog-authuser"] == "0"
    assert seen["user"] == "brand-page-id"


@pytest.mark.parametrize("account", [None, {}, {"accountName": ""}])
def test_an_account_that_cannot_be_read_is_rejected(tmp_path, account):
    manager = manager_for(tmp_path, client_factory=lambda h, user=None: FakeClient(h, user, account=account))
    with pytest.raises(AccountSelectionError):
        manager.validate_candidate(candidate())


def test_a_rejected_session_is_expired_not_a_new_login(tmp_path):
    error = YTMusicServerError("Server returned HTTP 401: Unauthorized.\ninvalid credentials")
    manager = manager_for(tmp_path, client_factory=lambda h, user=None: FakeClient(h, user, error=error))
    with pytest.raises(AuthExpired):
        manager.validate_candidate(candidate())


def test_a_network_failure_means_unknown_not_expired(tmp_path):
    manager = manager_for(
        tmp_path,
        client_factory=lambda h, user=None: FakeClient(h, user, error=requests.ConnectionError("no route")),
    )
    with pytest.raises(SessionVerificationUnavailable, match="connectivity"):
        manager.validate_candidate(candidate())


def test_an_unreadable_provider_response_is_verification_unavailable(tmp_path):
    manager = manager_for(
        tmp_path,
        client_factory=lambda h, user=None: FakeClient(
            h, user, error=KeyError("Unable to find 'accountName' using path [...] on {huge dump}")
        ),
    )
    with pytest.raises(SessionVerificationUnavailable) as excinfo:
        manager.validate_candidate(candidate())
    assert "huge dump" not in str(excinfo.value)


def test_a_candidate_missing_the_signing_cookie_is_invalid_format(tmp_path):
    manager = manager_for(tmp_path)
    raw = session_mod.BrowserSession(headers={"cookie": "SID=x", "x-goog-authuser": "0"})
    with pytest.raises(AuthInvalidFormat):
        manager.validate_candidate(raw)


# -- commit -------------------------------------------------------------------


def test_save_verified_commits_the_session(tmp_path):
    manager = manager_for(tmp_path)
    verified = manager.validate_candidate(candidate(user="brand"))
    record = manager.save_verified(verified, expected_revision=None)
    assert record.method == "browser"
    assert record.user == "brand"
    assert manager.store.load().headers["cookie"].endswith("__Secure-3PAPISID=secret")


def test_a_commit_after_a_logout_is_rejected(tmp_path):
    manager = manager_for(tmp_path)
    first = manager.save_verified(manager.validate_candidate(candidate()), expected_revision=None)
    manager.logout()
    with pytest.raises(AuthStorageError):
        manager.save_verified(manager.validate_candidate(candidate()), expected_revision=first.revision)
    assert manager.store.load().method == "none"


# -- clients ------------------------------------------------------------------


def test_a_new_process_builds_a_browser_client_from_the_saved_record(tmp_path):
    original = manager_for(tmp_path)
    original.save_verified(original.validate_candidate(candidate(user="brand")), expected_revision=None)

    seen = {}

    def factory(headers, user=None):
        seen["headers"] = dict(headers)
        seen["user"] = user
        return FakeClient(headers, user)

    fresh = manager_for(tmp_path, client_factory=factory)  # a separate manager, same store
    client = fresh.get_client(require_auth=True)
    assert isinstance(client, FakeClient)
    assert seen["headers"]["cookie"].endswith("__Secure-3PAPISID=secret")
    assert seen["headers"]["origin"] == session_mod.MUSIC_ORIGIN
    assert seen["user"] == "brand"


def test_an_mutated_client_header_copy_does_not_touch_the_record(tmp_path):
    manager = manager_for(tmp_path)
    manager.save_verified(manager.validate_candidate(candidate()), expected_revision=None)
    client = manager.get_client(require_auth=True)
    client.headers["cookie"] = "tampered"
    assert manager.store.load().headers["cookie"].endswith("__Secure-3PAPISID=secret")


def test_missing_credentials_raise_for_account_calls_and_nothing_for_guests(tmp_path):
    manager = manager_for(tmp_path)
    with pytest.raises(AuthMissing, match="ytm login"):
        manager.get_client(require_auth=True)
    assert isinstance(manager.get_client(), FakeClient)


def test_a_tombstone_is_missing_not_a_legacy_fallback(tmp_path):
    manager = manager_for(tmp_path)
    manager.logout()
    assert not manager.has_credentials()
    with pytest.raises(AuthMissing):
        manager.get_client(require_auth=True)


def test_has_credentials_is_local_presence_only(tmp_path):
    manager = manager_for(tmp_path)
    assert not manager.has_credentials()
    manager.save_verified(manager.validate_candidate(candidate()), expected_revision=None)
    assert manager.has_credentials()


# -- status -------------------------------------------------------------------


def test_status_never_claims_valid_without_a_probe(tmp_path):
    manager = manager_for(tmp_path)
    manager.save_verified(manager.validate_candidate(candidate()), expected_revision=None)
    status = manager.status(validate=False)
    assert status.logged_in and status.state == "not_checked"
    assert status.account_name is None


def test_status_valid_after_a_probe(tmp_path):
    manager = manager_for(tmp_path)
    manager.save_verified(manager.validate_candidate(candidate()), expected_revision=None)
    status = manager.status()
    assert (status.logged_in, status.method, status.state) == (True, "browser", "valid")
    assert status.account_name == "Example Listener"


def test_status_expired_when_the_probe_is_rejected(tmp_path):
    error = YTMusicServerError("Server returned HTTP 401: Unauthorized.")
    manager = manager_for(
        tmp_path, client_factory=lambda h, user=None: FakeClient(h, user, error=error)
    )
    save_record(manager)
    status = manager.status()
    assert (status.logged_in, status.state) == (False, "expired")


def test_status_unknown_when_the_provider_cannot_be_reached(tmp_path):
    error = requests.exceptions.ConnectionError("no route")
    manager = manager_for(tmp_path, client_factory=lambda h, user=None: FakeClient(h, user, error=error))
    save_record(manager)
    status = manager.status()
    assert (status.logged_in, status.state) == (True, "unknown")


def test_status_logged_out_without_credentials(tmp_path):
    status = manager_for(tmp_path).status()
    assert (status.logged_in, status.method, status.state) == (False, None, "logged_out")
    assert "ytm login" in status.detail
