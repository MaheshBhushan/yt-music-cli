"""Windows application-data policy; legacy data stays readable until migration."""

from pathlib import Path

from ytm import cache, config, playlists_local, state
from ytm.authentication import storage
from ytm.volume import SystemVolume


def test_session_path_follows_platformdirs(monkeypatch, tmp_path):
    """The Windows policy (LOCALAPPDATA via platformdirs) is exercised."""
    monkeypatch.setattr(
        storage.platformdirs, "user_config_path",
        lambda appname, appauthor=None: str(tmp_path / "AppData" / "ytm"),
    )
    assert storage.default_session_path() == tmp_path / "AppData" / "ytm" / "session.json"


def test_active_and_legacy_records_are_distinct_locations():
    assert storage.default_session_path() != config.CONFIG_PATH
    assert storage.default_session_path().name == "session.json"
    assert config.CONFIG_PATH.name == "config.toml"


def test_default_paths_never_live_in_the_repository():
    repository = Path(__file__).resolve().parents[1]
    for path in (
        config.CONFIG_PATH,
        state.STATE_PATH,
        playlists_local.DEFAULT_PATH,
        cache.DEFAULT_CACHE_DIR,
    ):
        resolved = Path(path).resolve()
        assert resolved != repository
        assert repository not in resolved.parents
    # the autouse test fixture redirects the audio cache; the rest resolve
    # under the user's home on every platform
    for path in (config.CONFIG_PATH, state.STATE_PATH, playlists_local.DEFAULT_PATH):
        assert Path.home() in Path(path).resolve().parents


def test_no_system_mixer_is_a_supported_fallback():
    """Without wpctl/pactl (the Windows case) playback uses mpv's volume."""
    assert SystemVolume.detect(platform="linux", which=lambda name: None) is None
    # the Player's mixer stays None, so volume() reads/writes mpv's own
    # property; tests/test_volume.py covers that behavior with the fake mpv
