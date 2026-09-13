import pytest

from ytm import auth, music, update

REAL_LATEST_VERSION = update.latest_version


@pytest.fixture(autouse=True)
def no_network_update_check(monkeypatch, tmp_path):
    """Tests must never reach PyPI or touch the real update cache."""
    monkeypatch.setattr(update, "CHECK_PATH", tmp_path / "update-check.json")
    monkeypatch.setattr(update, "latest_version", lambda timeout=3.0, opener=None: None)


@pytest.fixture
def real_latest_version():
    return REAL_LATEST_VERSION


@pytest.fixture(autouse=True)
def no_real_auth(monkeypatch, tmp_path):
    """Tests never read the developer's own ~/.config/ytm/auth.json: a stale
    one there would make the catalogue layer try a real browser refresh."""
    monkeypatch.setattr(auth, "AUTH_PATH", tmp_path / "auth" / "auth.json")
    monkeypatch.setattr(auth, "COOKIES_PATH", tmp_path / "auth" / "cookies.txt")


@pytest.fixture(autouse=True)
def fresh_catalogue_client(monkeypatch, tmp_path):
    """Each test starts with no cached ytmusicapi client and its own visitor
    id file, so one test's fake client can never serve the next one and the
    developer's real ~/.local/state/ytm/visitor.json is never touched."""
    monkeypatch.setattr(music, "VISITOR_PATH", tmp_path / "visitor.json")
    music.reset_client()
    yield
    music.reset_client()
