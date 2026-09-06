"""User configuration file.

Reads ``~/.config/ytm/config.toml`` (stdlib ``tomllib``, no dependency) at
startup. The shape and defaults are::

    [audio]
    volume = 70
    device = "auto"

    [behaviour]
    autoplay_radio = true
    confirm_remote_delete = true
    authenticated_streams = false

    [auth]
    x-goog-authuser = "0"

    [ui]
    theme = "dark"
    art = "blocks"

    [tui]
    queue_column_width = 40

    [pot]
    enabled = true
    base_url = "http://127.0.0.1:4416"

    [keys]
    toggle = "space"
    next = "n"
    prev = "p"
    search = "/"
    quit = "e"

    [update]
    check = true
    auto = false

A missing file yields exactly these defaults. A partial file overrides only
the keys it specifies -- every other key, and every key in a section that
is omitted entirely, keeps its default.

Malformed TOML, an unknown section/key, or a value of the wrong type never
raises out of startup: a readable warning is printed to stderr and the
default is used for whatever could not be parsed, so a bad config degrades
the experience (a wrong key is ignored) rather than refusing to start.
"""

import sys
import tomllib
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "ytm" / "config.toml"

DEFAULTS = {
    "audio": {"volume": 70, "device": "auto"},
    # authenticated_streams: hand the account cookies to yt-dlp when mpv
    # resolves a stream. Off by default: with cookies YouTube serves URLs
    # that need an account-bound PO token, which the provider does not
    # currently supply, and the audio server then answers 403. Anonymous
    # resolution plays the same catalogue; only private or age-gated tracks
    # need this on.
    "behaviour": {
        "autoplay_radio": True,
        "confirm_remote_delete": True,
        "authenticated_streams": False,
    },
    # x-goog-authuser: Google account index to authenticate as when browser
    # cookies include more than one signed-in account. YouTube Music expects
    # this as a string header value such as "0", "1", or "2".
    "auth": {"x-goog-authuser": "0"},
    # art: cover art in the TUI. "blocks" (default) draws coloured half-cell
    # glyphs and works in every terminal, tmux included. "kitty" and "sixel"
    # use the terminal's pixel graphics protocol and "auto" probes for one;
    # they are opt-in because Sixel in Konsole froze the now-playing pane.
    # "ascii" is plain block characters; "off" hides the art.
    "ui": {"theme": "dark", "art": "blocks"},
    # queue_column_width: maximum width of each PLAYED / UP NEXT column in
    # the TUI now-playing strip. 0 means no maximum.
    "tui": {"queue_column_width": 40},
    "pot": {"enabled": True, "base_url": "http://127.0.0.1:4416"},
    "keys": {
        "toggle": "space",
        "next": "n",
        "prev": "p",
        "search": "/",
        "quit": "e",
    },
    # check: ask PyPI once a day whether a newer ytm exists and say so in
    # the TUI. auto: also install it (and a fresh yt-dlp) when one exists.
    "update": {"check": True, "auto": False},
}

#: expected Python type for each known (section, key) -- bool is checked
#: before int since bool is a subclass of int in Python
_TYPES = {
    ("audio", "volume"): int,
    ("audio", "device"): str,
    ("behaviour", "autoplay_radio"): bool,
    ("behaviour", "confirm_remote_delete"): bool,
    ("behaviour", "authenticated_streams"): bool,
    ("auth", "x-goog-authuser"): str,
    ("ui", "theme"): str,
    ("ui", "art"): str,
    ("tui", "queue_column_width"): int,
    ("pot", "enabled"): bool,
    ("pot", "base_url"): str,
    ("keys", "toggle"): str,
    ("keys", "next"): str,
    ("keys", "prev"): str,
    ("keys", "search"): str,
    ("keys", "quit"): str,
    ("update", "check"): bool,
    ("update", "auto"): bool,
}


def _warn(message):
    print(f"ytm: config warning: {message}", file=sys.stderr)


def _default_config():
    return {section: dict(values) for section, values in DEFAULTS.items()}


def _matches_type(value, expected):
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def load(path=None):
    """The effective config: documented defaults overridden by `path`.

    `path` defaults to `CONFIG_PATH`. Never raises -- see module docstring
    for how a missing file, malformed TOML, or a bad value are handled.
    """
    path = Path(path) if path is not None else CONFIG_PATH
    config = _default_config()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return config

    try:
        raw = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        _warn(f"could not parse {path} ({exc}); using defaults")
        return config

    for section, values in raw.items():
        if section not in DEFAULTS:
            _warn(f"unknown section '{section}' in {path}; ignoring")
            continue
        if not isinstance(values, dict):
            _warn(f"section '{section}' must be a table in {path}; ignoring")
            continue
        for key, value in values.items():
            if key not in DEFAULTS[section]:
                _warn(f"unknown key '{section}.{key}' in {path}; ignoring")
                continue
            expected = _TYPES[(section, key)]
            if not _matches_type(value, expected):
                _warn(
                    f"'{section}.{key}' must be a {expected.__name__} in {path}; "
                    "using default"
                )
                continue
            if (section, key) == ("auth", "x-goog-authuser") and not value.isdecimal():
                _warn(
                    f"'{section}.{key}' must be a string representation of a "
                    f"number in {path}; using default"
                )
                continue
            if (section, key) == ("tui", "queue_column_width") and value < 0:
                _warn(
                    f"'{section}.{key}' must be a positive integer or 0 in {path}; "
                    "using default"
                )
                continue
            config[section][key] = value
    return config
