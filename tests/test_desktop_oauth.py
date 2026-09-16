import json
from types import SimpleNamespace

import pytest
from google_auth_oauthlib.flow import InstalledAppFlow

from ytm import auth


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    source = tmp_path / "client.json"
    source.write_text(json.dumps({"installed": {
        "client_id": "test-id", "client_secret": "test-secret",
        "token_uri": "https://untrusted.invalid/token",
    }}))
    token = {"access_token": "access", "refresh_token": "refresh",
             "scope": ["https://www.googleapis.com/auth/youtube"],
             "token_type": "Bearer", "expires_in": 3600}
    captured = {}

    def make(config, **kwargs):
        captured.update(config=config, options=kwargs)
        def run(**kwargs):
            captured["run"] = kwargs
        return SimpleNamespace(run_local_server=run, oauth2session=SimpleNamespace(token=token))

    monkeypatch.setattr(InstalledAppFlow, "from_client_config", make)
    return source, token, captured


def test_desktop_stores_refreshable_token_and_reuses_client(desktop, tmp_path):
    source, _token, captured = desktop
    path = tmp_path / "config" / "auth.json"
    auth.oauth_setup(client_file=source, path=path)
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "refresh"
    assert saved["scope"] == "https://www.googleapis.com/auth/youtube"
    assert captured["options"]["autogenerate_code_verifier"] is True
    assert captured["config"]["installed"]["token_uri"] == "https://oauth2.googleapis.com/token"
    assert captured["run"]["host"] == "127.0.0.1"
    assert captured["run"]["open_browser"] is True
    for name in ("auth.json", "oauth_client.json", "oauth_desktop_client.json"):
        assert (path.parent / name).stat().st_mode & 0o777 == 0o600
    source.unlink()
    auth.oauth_setup(path=path)


def test_default_client_signs_in_without_credential_prompts(desktop, tmp_path, monkeypatch):
    source, _, captured = desktop
    monkeypatch.setattr(auth, "DEFAULT_DESKTOP_CLIENT", source)
    for name in ("YTM_OAUTH_CLIENT_FILE", "YTM_OAUTH_CLIENT_ID", "YTM_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("unexpected client prompt"))
    path = tmp_path / "fresh" / "auth.json"
    auth.oauth_setup(path=path)
    assert path.exists()
    assert captured["config"]["installed"]["client_id"] == "test-id"


def test_explicit_desktop_client_overrides_default(desktop, tmp_path, monkeypatch):
    source, _, captured = desktop
    default = tmp_path / "default.json"
    default.write_text(json.dumps({"installed": {"client_id": "default", "client_secret": "default"}}))
    monkeypatch.setattr(auth, "DEFAULT_DESKTOP_CLIENT", default)
    auth.oauth_setup(client_file=source, path=tmp_path / "fresh" / "auth.json")
    assert captured["config"]["installed"]["client_id"] == "test-id"


@pytest.mark.parametrize("missing", ["refresh_token", "access_token", "scope"])
def test_incomplete_consent_preserves_existing_auth(desktop, tmp_path, missing):
    source, token, _ = desktop
    path = tmp_path / "auth.json"
    path.write_text('{"cookie": "existing"}')
    token.pop(missing)
    with pytest.raises(auth.AuthError):
        auth.oauth_setup(client_file=source, path=path)
    assert json.loads(path.read_text()) == {"cookie": "existing"}
    assert not (tmp_path / "oauth_client.json").exists()


def test_web_client_rejected_without_overwriting_auth(tmp_path):
    source = tmp_path / "client.json"
    source.write_text('{"web": {"client_id": "id"}}')
    with pytest.raises(auth.AuthError, match="desktop"):
        auth.desktop_oauth_setup(source, path=tmp_path / "auth.json")


def test_stored_token_is_exactly_what_ytmusicapi_accepts(desktop, tmp_path):
    from ytmusicapi.auth.oauth.token import OAuthToken, Token

    source, token, _ = desktop
    token.update(id_token="jwt", expires_at=1757800000.25, refresh_token_expires_in=604799)
    token.pop("token_type")
    path = tmp_path / "auth.json"
    auth.oauth_setup(client_file=source, path=path)
    saved = json.loads(path.read_text())
    assert set(saved) == set(Token.members())
    assert saved["token_type"] == "Bearer"
    assert saved["expires_at"] == 1757800000 and isinstance(saved["expires_at"], int)
    loaded = OAuthToken.from_json(path)  # what ytmusicapi <1.11 does with the file
    assert loaded.refresh_token == "refresh"
    assert OAuthToken.is_oauth(saved)


def test_tv_flow_forgets_remembered_desktop_client(desktop, tmp_path, monkeypatch):
    source, _token, _ = desktop
    monkeypatch.setattr(auth, "DEFAULT_DESKTOP_CLIENT", source)
    path = tmp_path / "auth.json"
    auth.oauth_setup(client_file=source, path=path)
    remembered = tmp_path / "oauth_desktop_client.json"
    assert remembered.exists()

    class Creds:
        def __init__(self, client_id, client_secret):
            pass
        def get_code(self):
            return {"verification_url": "u", "user_code": "c", "device_code": "d", "interval": 0}
        def token_from_code(self, code):
            return {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/youtube", "token_type": "Bearer"}

    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    auth.oauth_setup(client_id="tv-id", client_secret="tv-secret", path=path,
                     credentials_factory=Creds, sleep=lambda s: None)
    assert not remembered.exists()
    assert json.loads((tmp_path / "oauth_client.json").read_text())["client_id"] == "tv-id"
