"""Daemon state management module.

Persists just enough to redisplay and resume the session across daemon
restarts: the queue as video ids plus display metadata, the cursor, the
volume and the last-played video id. Resolved stream URLs are deliberately
never written here -- they expire within hours, so they are re-resolved at
play time instead.

Writes go to a temporary file in the same directory and are then renamed
over the real file, so a crash mid-write leaves the previous state intact
rather than a truncated file.
"""

import json
import os
from pathlib import Path

from ytm import api

STATE_PATH = Path.home() / ".local" / "state" / "ytm" / "state.json"

#: only these Track fields are persisted -- never a resolved URL
_TRACK_FIELDS = ("video_id", "title", "artist", "album", "duration", "duration_seconds")

DEFAULT_VOLUME = 100


def track_to_dict(track):
    """The persistable subset of a Track."""
    return {field: getattr(track, field) for field in _TRACK_FIELDS}


def track_from_dict(data):
    """Rebuild a Track from persisted fields, tolerating missing keys."""
    if not isinstance(data, dict) or not data.get("video_id"):
        return None
    seconds = data.get("duration_seconds")
    return api.Track(
        video_id=str(data["video_id"]),
        title=str(data.get("title") or "Unknown Title"),
        artist=str(data.get("artist") or "Unknown Artist"),
        album=str(data.get("album") or ""),
        duration=str(data.get("duration") or "0:00"),
        duration_seconds=seconds if isinstance(seconds, int) else 0,
    )


def empty():
    """The state of a daemon that has never played anything."""
    return {"tracks": [], "index": -1, "volume": DEFAULT_VOLUME, "last_played": None}


def load(path=STATE_PATH):
    """Return the persisted state, or a default one if it is absent or bad.

    A corrupt or partially written file must not stop the daemon starting,
    so anything unreadable degrades to the empty state.
    """
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    state = empty()
    tracks = data.get("tracks")
    if isinstance(tracks, list):
        state["tracks"] = [
            entry for entry in (track_from_dict(item) for item in tracks) if entry
        ]
    index = data.get("index")
    if isinstance(index, int) and 0 <= index < len(state["tracks"]):
        state["index"] = index
    volume = data.get("volume")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        state["volume"] = max(0, min(100, volume))
    last_played = data.get("last_played")
    if isinstance(last_played, str):
        state["last_played"] = last_played
    return state


def save(state, path=STATE_PATH):
    """Atomically write state (temp file in the same dir, then rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tracks": [track_to_dict(track) for track in state.get("tracks") or []],
        "index": state.get("index", -1),
        "volume": state.get("volume", DEFAULT_VOLUME),
        "last_played": state.get("last_played"),
    }
    tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp_path, path)
    return path
