"""Tests for `ytm auth --oauth` (T23): device-code flow and dual auth-kind support."""
import json

import pytest

from ytm import auth
from ytm.auth import AuthExpired, AuthMissing


class _FakeCredentials:
    """Stub OAuthCredentials that never touches the network."""

    calls = []

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._polls = 0

    def get_code(self):
        return {
            "verification_url": "https://www.google.com/device",
            "user_code": "ABC-DEF-GHI",
            "device_code": "device-xyz",
            "interval": 0,
            "expires_in": 1800,
        }

    def token_from_code(self, device_code):
        self._polls += 1
        if self._polls < 2:
            return {"error": "authorization_pending"}
        return {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "scope": "https://www.googleapis.com/auth/youtube",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def refresh_token(self, refresh_token):
        raise auth.UnauthorizedOAuthClient("revoked")


class _FailingRefreshCredentials(_FakeCredentials):
    def refresh_token(self, refresh_token):
        raise auth.UnauthorizedOAuthClient("Token refresh error. Most likely client/token mismatch.")


def _oauth_token_dict(expired=True):
    return {
        "scope": "https://www.googleapis.com/auth/youtube",
        "token_type": "Bearer",
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expires_at": 0 if expired else 9999999999,
        "expires_in": 0 if expired else 999999,
    }


def test_oauth_setup_prints_verification_url_and_user_code(tmp_path, capsys, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    monkeypatch.setenv("YTM_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YTM_OAUTH_CLIENT_SECRET", "csecret")

    result = auth.oauth_setup(path=path, credentials_factory=_FakeCredentials, sleep=lambda s: None)

    out = capsys.readouterr().out
    assert "https://www.google.com/device" in out
    assert "ABC-DEF-GHI" in out
    assert result == path


def test_oauth_setup_stores_token_at_mode_0600(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    monkeypatch.setenv("YTM_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YTM_OAUTH_CLIENT_SECRET", "csecret")

    auth.oauth_setup(path=path, credentials_factory=_FakeCredentials, sleep=lambda s: None)

    assert path.stat().st_mode & 0o777 == 0o600
    token = json.loads(path.read_text())
    assert token["access_token"] == "at-1"
    assert token["refresh_token"] == "rt-1"

    client_path = auth._oauth_client_path(path)
    assert client_path.stat().st_mode & 0o777 == 0o600
    stored = json.loads(client_path.read_text())
    assert stored == {"client_id": "cid", "client_secret": "csecret"}


def test_oauth_setup_credential_precedence_flags_over_env_over_prompt(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    monkeypatch.setenv("YTM_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("YTM_OAUTH_CLIENT_SECRET", "env-secret")

    def _fail_input(prompt=""):
        raise AssertionError("should not prompt when flags are given")

    monkeypatch.setattr("builtins.input", _fail_input)

    auth.oauth_setup(
        client_id="flag-id",
        client_secret="flag-secret",
        path=path,
        credentials_factory=_FakeCredentials,
        sleep=lambda s: None,
    )
    stored = json.loads(auth._oauth_client_path(path).read_text())
    assert stored == {"client_id": "flag-id", "client_secret": "flag-secret"}


def test_oauth_setup_falls_back_to_env_then_prompt(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    monkeypatch.delenv("YTM_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("YTM_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "prompted-id")
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": "prompted-secret")

    auth.oauth_setup(path=path, credentials_factory=_FakeCredentials, sleep=lambda s: None)

    stored = json.loads(auth._oauth_client_path(path).read_text())
    assert stored == {"client_id": "prompted-id", "client_secret": "prompted-secret"}


def test_client_builds_ytmusic_with_oauth_credentials_for_oauth_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_oauth_token_dict(expired=False)))
    auth._write_json_0600(auth._oauth_client_path(path), {"client_id": "cid", "client_secret": "csec"})

    captured = {}

    class _FakeYTMusic:
        def __init__(self, auth_arg, oauth_credentials=None):
            captured["auth_arg"] = auth_arg
            captured["oauth_credentials"] = oauth_credentials
            self._token = type("T", (), {"access_token": "at"})()

    monkeypatch.setattr(auth.ytmusicapi, "YTMusic", _FakeYTMusic)

    ytm = auth.client(path=path, credentials_factory=_FakeCredentials)

    assert captured["auth_arg"] == str(path)
    assert isinstance(captured["oauth_credentials"], _FakeCredentials)
    assert isinstance(ytm, _FakeYTMusic)


def test_client_builds_ytmusic_without_oauth_credentials_for_browser_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    path.parent.mkdir(parents=True)
    headers = {"cookie": "SID=x; __Secure-3PAPISID=y", "authorization": "SAPISIDHASH 0_0"}
    path.write_text(json.dumps(headers))

    captured = {}

    def _fake_ytmusic(passed_headers):
        captured["headers"] = passed_headers
        return "a-client"

    monkeypatch.setattr(auth.ytmusicapi, "YTMusic", _fake_ytmusic)

    result = auth.client(path=path)

    assert captured["headers"] == headers
    assert "oauth_credentials" not in captured
    assert result == "a-client"


def test_client_surfaces_auth_expired_for_revoked_oauth_refresh_token(tmp_path, monkeypatch):
    path = tmp_path / "config" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_oauth_token_dict(expired=True)))
    auth._write_json_0600(auth._oauth_client_path(path), {"client_id": "cid", "client_secret": "csec"})

    class _RealishYTMusic:
        """Mimics ytmusicapi's lazy-refresh-on-access behaviour for a revoked token."""

        def __init__(self, auth_arg, oauth_credentials=None):
            self._creds = oauth_credentials

        class _Token:
            def __init__(self, creds):
                self._creds = creds

            @property
            def access_token(self):
                self._creds.refresh_token("rt-old")

        @property
        def _token(self):
            return self._Token(self._creds)

    monkeypatch.setattr(auth.ytmusicapi, "YTMusic", _RealishYTMusic)

    with pytest.raises(AuthExpired, match="ytm auth --oauth"):
        auth.client(path=path, credentials_factory=_FailingRefreshCredentials)


def test_load_cookies_returns_none_for_oauth_file(tmp_path):
    path = tmp_path / "config" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_oauth_token_dict(expired=False)))

    assert auth.load_cookies(path=path) is None


def test_load_cookies_still_works_for_browser_file(tmp_path):
    path = tmp_path / "config" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cookie": "SID=abc"}))

    assert auth.load_cookies(path=path) == "SID=abc"


def test_oauth_client_missing_raises_auth_missing(tmp_path):
    path = tmp_path / "config" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_oauth_token_dict(expired=False)))
    # no oauth_client.json written

    with pytest.raises(AuthMissing, match="ytm auth --oauth"):
        auth.client(path=path)
