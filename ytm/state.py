"""The little state mpv and YouTube Music cannot hold for us.

Two things, one JSON file under ``~/.local/state/ytm/``:

* the last search, so ``ytm play 3`` can mean "the third result";
* metadata for tracks that have been queued, keyed by video id, so
  ``ytm status`` can print artist and album for whatever mpv is playing.
  mpv only knows the URL and the title we forced on the entry.

The queue itself, the cursor, pause state, position and volume all live in
mpv and are never mirrored here.

The file is read on every lookup, and the TUI looks a track up for every
queue row on every queue change, so the parsed contents are memoised and
re-read only when the file's mtime/size say it changed. The dict `load`
returns is therefore shared: callers outside this module must treat it as
read-only.
"""

import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ytm.music import track_from_dict

STATE_PATH = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "ytm" / "session.json"

#: how many tracks' metadata to remember before forgetting the oldest
TRACK_MEMORY = 500

#: remember_* are read-modify-write; TUI worker threads call them concurrently
_WRITE_LOCK = threading.RLock()

#: the last parse of the state file: {"key": (path, mtime_ns, size), "data": {...}}
_CACHE = {"key": None, "data": None}


def reset_cache():
    """Forget the in-process parse, primarily after a fork or in tests."""
    with _WRITE_LOCK:
        _CACHE.update(key=None, data=None)


@contextmanager
def _file_lock(path):
    """Serialize session read-modify-write operations across processes."""
    path = Path(path or STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+b") as lock:
        if sys.platform.startswith("win"):
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform.startswith("win"):
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _stamp(path):
    """(path, mtime, size) for `path`, or (path, None) when it is not there."""
    try:
        stat = os.stat(path)
    except OSError:
        return (str(path), None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _normalise(data):
    if not isinstance(data, dict):
        data = {}
    search = data.get("last_search")
    data["last_search"] = search if isinstance(search, list) else []
    tracks = data.get("tracks")
    data["tracks"] = tracks if isinstance(tracks, dict) else {}
    return data


def load(path=None):
    """The parsed state file. The returned dict is shared -- do not mutate it."""
    path = path or STATE_PATH
    key = _stamp(path)
    with _WRITE_LOCK:
        if _CACHE["key"] == key and _CACHE["data"] is not None:
            return _CACHE["data"]
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        data = {}
    data = _normalise(data)
    with _WRITE_LOCK:
        _CACHE["key"], _CACHE["data"] = key, data
    return data


def save(data, path=None):
    """Write `data` atomically and keep the in-process cache in step.

    The temp file carries this process's pid: the CLI, the TUI and the
    ``ytm radio`` mpv spawns for autoplay all write this file, and a shared
    temp name would let one process rename another's half-written copy into
    place.
    """
    path = Path(path or STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(data, file)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    with _WRITE_LOCK:
        _CACHE["key"], _CACHE["data"] = _stamp(path), data


def remember_search(tracks, path=None):
    with _file_lock(path), _WRITE_LOCK:
        reset_cache()
        data = load(path)
        data["last_search"] = [asdict(t) for t in tracks]
        _remember(data, tracks)
        save(data, path)


def last_search(path=None):
    return [track_from_dict(t) for t in load(path)["last_search"]]


def remember_tracks(tracks, path=None):
    tracks = list(tracks)
    if not tracks:
        return
    with _file_lock(path), _WRITE_LOCK:
        reset_cache()
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
    return track_from_dict(data) if data else None


def tracks_for(video_ids, path=None):
    """``{video_id: Track}`` for the ids that are remembered, in one read.

    The per-id lookup of `track_for` costs a stat (and, when the file
    changed, a parse) each; a queue redraw asks for every row at once.
    """
    known = load(path)["tracks"]
    found = {}
    for video_id in video_ids:
        data = known.get(video_id) if video_id else None
        if data:
            found[video_id] = track_from_dict(data)
    return found
