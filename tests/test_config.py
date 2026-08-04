"""Tests for ytm.config: defaults, partial overrides, and malformed input.

Fully offline: reads from tmp_path files only, never the real
~/.config/ytm/config.toml.
"""

from ytm import config as config_mod


def test_missing_file_yields_documented_defaults(tmp_path):
    config = config_mod.load(tmp_path / "does-not-exist.toml")
    assert config == {
        "audio": {"volume": 70, "device": "auto"},
        "behaviour": {"autoplay_radio": True, "confirm_remote_delete": True},
        "ui": {"theme": "dark"},
        "pot": {"enabled": True, "base_url": "http://127.0.0.1:4416"},
        "keys": {
            "toggle": "space",
            "next": "n",
            "prev": "p",
            "search": "/",
            "quit": "q",
        },
    }


def test_partial_config_overrides_only_its_own_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [audio]
        volume = 42

        [keys]
        toggle = "k"
        """
    )
    config = config_mod.load(path)
    assert config["audio"]["volume"] == 42
    assert config["audio"]["device"] == "auto"
    assert config["keys"]["toggle"] == "k"
    assert config["keys"]["next"] == "n"
    assert config["keys"]["prev"] == "p"
    assert config["keys"]["search"] == "/"
    assert config["keys"]["quit"] == "q"
    assert config["behaviour"] == {
        "autoplay_radio": True,
        "confirm_remote_delete": True,
    }
    assert config["ui"] == {"theme": "dark"}


def test_malformed_toml_falls_back_to_defaults_with_warning(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("this is [ not valid toml at all =")
    config = config_mod.load(path)  # must not raise
    assert config == config_mod._default_config()
    captured = capsys.readouterr()
    assert "config warning" in captured.err
    assert "not" not in captured.out  # nothing printed to stdout


def test_wrong_typed_value_falls_back_to_default_for_that_key(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [behaviour]
        autoplay_radio = "yes"
        confirm_remote_delete = false
        """
    )
    config = config_mod.load(path)
    # the malformed key keeps its default...
    assert config["behaviour"]["autoplay_radio"] is True
    # ...but a correctly typed sibling key in the same table still applies
    assert config["behaviour"]["confirm_remote_delete"] is False
    captured = capsys.readouterr()
    assert "config warning" in captured.err


def test_unknown_section_and_key_are_ignored_with_warning(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [nonsense]
        foo = 1

        [ui]
        theme = "light"
        color_blind_mode = true
        """
    )
    config = config_mod.load(path)
    assert "nonsense" not in config
    assert config["ui"] == {"theme": "light"}
    captured = capsys.readouterr()
    assert "config warning" in captured.err
