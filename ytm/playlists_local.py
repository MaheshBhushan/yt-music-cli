"""Local (offline, on-disk) playlist store.

A parallel concept to remote (YouTube Music) playlists: instant, no network,
safe to experiment against. Stored as one plain-JSON file, written
atomically (temp file in the same directory, then rename), following the
same pattern as ``ytm.daemon.state``.

Each local playlist id is prefixed ``local-`` so a caller can tell, from the
id alone, whether an operation belongs here or against the remote API --
this is how ``ytm.daemon.server`` routes ``playlist_*`` commands.

Tracks are stored by ``video_id`` plus the same display metadata
``ytm.daemon.state`` persists for the queue, so a local playlist can be
redisplayed without ever hitting the network.
"""

import json
import os
import uuid
from pathlib import Path

from ytm import api
from ytm.daemon.state import track_from_dict, track_to_dict

DEFAULT_PATH = Path.home() / ".local" / "state" / "ytm" / "playlists.json"

LOCAL_ID_PREFIX = "local-"


def is_local_id(playlist_id):
    """Whether `playlist_id` names a local (not remote) playlist."""
    return isinstance(playlist_id, str) and playlist_id.startswith(LOCAL_ID_PREFIX)


def _new_id():
    return f"{LOCAL_ID_PREFIX}{uuid.uuid4().hex}"


def _empty():
    return {"playlists": []}


def load(path=None):
    """Return the persisted store, or an empty one if absent or corrupt.

    A corrupt or partially written file must not stop the daemon starting,
    so anything unreadable degrades to the empty store.
    """
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("playlists"), list):
        return _empty()
    playlists = []
    for entry in data["playlists"]:
        if not isinstance(entry, dict) or not entry.get("playlist_id"):
            continue
        tracks = [
            track_to_dict(t)
            for t in (track_from_dict(item) for item in entry.get("tracks") or [])
            if t is not None
        ]
        playlists.append(
            {
                "playlist_id": entry["playlist_id"],
                "title": entry.get("title") or "Untitled",
                "description": entry.get("description") or "",
                "privacy": entry.get("privacy") or "PRIVATE",
                "tracks": tracks,
            }
        )
    return {"playlists": playlists}


def save(store, path=None):
    """Atomically write `store` (temp file in the same dir, then rename)."""
    path = Path(path) if path is not None else DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(store, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp_path, path)
    return path


def _find(store, playlist_id):
    for entry in store["playlists"]:
        if entry["playlist_id"] == playlist_id:
            return entry
    return None


def list_playlists(path=None):
    """All local playlists as `api.Playlist` objects."""
    store = load(path)
    return [
        api.Playlist(
            playlist_id=entry["playlist_id"],
            title=entry["title"],
            track_count=len(entry["tracks"]),
            local=True,
        )
        for entry in store["playlists"]
    ]


def get_playlist(playlist_id, path=None):
    """(Playlist, [Track, ...]) for `playlist_id`, or (None, None) if absent."""
    store = load(path)
    entry = _find(store, playlist_id)
    if entry is None:
        return None, None
    playlist = api.Playlist(
        playlist_id=entry["playlist_id"],
        title=entry["title"],
        track_count=len(entry["tracks"]),
        local=True,
    )
    tracks = [track_from_dict(t) for t in entry["tracks"]]
    return playlist, [t for t in tracks if t is not None]


def create(title, description="", privacy="PRIVATE", path=None):
    """Create a local playlist and return its id."""
    store = load(path)
    playlist_id = _new_id()
    store["playlists"].append(
        {
            "playlist_id": playlist_id,
            "title": title,
            "description": description or "",
            "privacy": privacy or "PRIVATE",
            "tracks": [],
        }
    )
    save(store, path)
    return playlist_id


def add_items(playlist_id, tracks, path=None):
    """Append `tracks` (Track objects) to a local playlist.

    Raises KeyError if `playlist_id` does not exist.
    """
    store = load(path)
    entry = _find(store, playlist_id)
    if entry is None:
        raise KeyError(playlist_id)
    entry["tracks"].extend(track_to_dict(t) for t in tracks)
    save(store, path)
    return len(entry["tracks"])


def remove_items(playlist_id, video_ids, path=None):
    """Remove tracks matching `video_ids` from a local playlist.

    Raises KeyError if `playlist_id` does not exist.
    """
    store = load(path)
    entry = _find(store, playlist_id)
    if entry is None:
        raise KeyError(playlist_id)
    video_ids = set(video_ids)
    before = len(entry["tracks"])
    entry["tracks"] = [t for t in entry["tracks"] if t.get("video_id") not in video_ids]
    save(store, path)
    return before - len(entry["tracks"])


def delete(playlist_id, path=None):
    """Delete a local playlist. Returns whether it existed."""
    store = load(path)
    entry = _find(store, playlist_id)
    if entry is None:
        return False
    store["playlists"].remove(entry)
    save(store, path)
    return True
