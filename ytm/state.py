"""The little state mpv and YouTube Music cannot hold for us.

Two things, one JSON file under ``~/.local/state/ytm/``:

* the last search, so ``ytm play 3`` can mean "the third result";
* metadata for tracks that have been queued, keyed by video id, so
  ``ytm status`` can print artist and album for whatever mpv is playing.
  mpv only knows the URL and the title we forced on the entry.

The queue itself, the cursor, pause state, position and volume all live in
mpv and are never mirrored here.
"""

import json
import os
import threading
from dataclasses import asdict
from pathlib import Path

from ytm.music import Track

STATE_PATH = Path.home() / ".local" / "state" / "ytm" / "session.json"

#: how many tracks' metadata to remember before forgetting the oldest
TRACK_MEMORY = 500


def load(path=None):
    path = path or STATE_PATH
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("last_search", [])
    data.setdefault("tracks", {})
    return data


def save(data, path=None):
    path = Path(path or STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file)
    os.replace(tmp, path)


#: remember_* are read-modify-write; TUI worker threads call them concurrently
_WRITE_LOCK = threading.Lock()


def remember_search(tracks, path=None):
    with _WRITE_LOCK:
        data = load(path)
        data["last_search"] = [asdict(t) for t in tracks]
        _remember(data, tracks)
        save(data, path)


def last_search(path=None):
    return [Track(**t) for t in load(path)["last_search"]]


def remember_tracks(tracks, path=None):
    with _WRITE_LOCK:
        data = load(path)
        _remember(data, tracks)
        save(data, path)


def _remember(data, tracks):
    known = data["tracks"]
    for track in tracks:
        known.pop(track.video_id, None)  # re-insert at the end: most recent last
        known[track.video_id] = asdict(track)
    while len(known) > TRACK_MEMORY:
        known.pop(next(iter(known)))


def track_for(video_id, path=None):
    """Remembered metadata for `video_id`, or None."""
    data = load(path)["tracks"].get(video_id)
    return Track(**data) if data else None
