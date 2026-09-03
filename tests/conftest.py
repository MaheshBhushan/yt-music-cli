import pytest

from ytm import update

REAL_LATEST_VERSION = update.latest_version


@pytest.fixture(autouse=True)
def no_network_update_check(monkeypatch, tmp_path):
    """Tests must never reach PyPI or touch the real update cache."""
    monkeypatch.setattr(update, "CHECK_PATH", tmp_path / "update-check.json")
    monkeypatch.setattr(update, "latest_version", lambda timeout=3.0, opener=None: None)


@pytest.fixture
def real_latest_version():
    return REAL_LATEST_VERSION
