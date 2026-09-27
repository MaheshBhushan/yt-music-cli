"""Local (offline, on-disk) playlist store.

A parallel concept to remote (YouTube Music) playlists: instant, no network,
safe to experiment against. Stored as one plain-JSON file, written
atomically (unique temp file in the same directory, then rename), following
the same atomic-write pattern as ``ytm.state``.

Each local playlist id is prefixed ``local-`` so a caller can tell, from the
id alone, whether an operation belongs here or against the remote API --
this is how the TUI backend routes ``playlist_*`` commands.

Tracks are stored by ``video_id`` plus the same display metadata
``ytm.state`` remembers for queued tracks, so a local playlist can be
redisplayed without ever hitting the network.

Reads are tolerant: a corrupt file displays as empty so the TUI can still
start. Mutations are not: every create/add/remove/delete runs as one
transaction (in-process lock, cross-process lock, strict load, write,
replace) and refuses to touch a file it cannot understand exactly, because
replacing bytes that might still be recoverable loses the user's data.
"""

import json
import os
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from ytm import music as api
from ytm.music import track_from_dict, track_to_dict

DEFAULT_PATH = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "ytm" / "playlists.json"

LOCAL_ID_PREFIX = "local-"


class PlaylistStorageError(RuntimeError):
    """Local playlist storage could not be read or written safely.

    Mutations refuse to proceed rather than replace data that might still be
    recoverable; the message says whether the original file was preserved.
    """


#: create/add/remove/delete are read-modify-write; TUI worker threads and
#: separate processes (CLI, mpv autoplay children) can run them at once
_MUTATION_LOCK = threading.RLock()


@contextmanager
def _file_lock(path):
    """Serialize local-playlist transactions across processes.

    Same advisory pattern as ``ytm.state``: a stable sidecar lock file next
    to the store, released by the OS when the process dies. The lock file is
    never unlinked while another process may hold it.
    """
    path = Path(path)
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


def _new_id():
    return f"{LOCAL_ID_PREFIX}{uuid.uuid4().hex}"


def is_local_id(playlist_id):
    """Whether `playlist_id` names a local (not remote) playlist."""
    return isinstance(playlist_id, str) and playlist_id.startswith(LOCAL_ID_PREFIX)


def _empty():
    return {"playlists": []}


def load(path=None):
    """Return the persisted store for display, or an empty one.

    A corrupt or partially written file must not stop the daemon starting,
    so unreadable *content* degrades to the empty store. A file that exists
    but cannot be read at all (permissions, I/O) is reported instead: an
    access failure must not look like an empty library.
    """
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise PlaylistStorageError(
            f"Could not read the local playlist file at {path} ({exc}); "
            "nothing was changed."
        ) from exc
    except ValueError:
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


def _validate_store(data, path):
    """The store exactly as written, or a storage error.

    Structural problems refuse the mutation: a missing playlist id, a
    non-list ``tracks`` or a track entry without an id is broken data, while
    unknown optional keys and missing optional fields are historical
    compatibility and pass through untouched.
    """
    if not isinstance(data, dict) or not isinstance(data.get("playlists"), list):
        raise PlaylistStorageError(
            f"The local playlist file at {path} does not have the expected shape; "
            "it was left untouched."
        )
    for entry in data["playlists"]:
        if not isinstance(entry, dict):
            raise PlaylistStorageError(
                f"The local playlist file at {path} contains a non-object playlist; "
                "it was left untouched."
            )
        playlist_id = entry.get("playlist_id")
        if not isinstance(playlist_id, str) or not playlist_id:
            raise PlaylistStorageError(
                f"The local playlist file at {path} contains a playlist without an id; "
                "it was left untouched."
            )
        tracks = entry.get("tracks")
        if tracks is None:
            entry["tracks"] = []  # an old record that never carried tracks
        elif not isinstance(tracks, list):
            raise PlaylistStorageError(
                f"Local playlist {playlist_id} has a non-list 'tracks' value; "
                "the file was left untouched."
            )
        else:
            for item in tracks:
                if not isinstance(item, dict) or not isinstance(item.get("video_id"), str) \
                        or not item["video_id"]:
                    raise PlaylistStorageError(
                        f"Local playlist {playlist_id} contains a track without an id; "
                        "the file was left untouched."
                    )
    return data


def _load_for_mutation(path):
    """The on-disk store, or a storage error explaining what is wrong.

    A missing file is a new empty store; everything else must be
    understood exactly, because this copy is about to be written back.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise PlaylistStorageError(
            f"Could not read the local playlist file at {path} ({exc}); "
            "nothing was changed."
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PlaylistStorageError(
            f"The local playlist file at {path} is not valid JSON; it was left untouched."
        ) from exc
    return _validate_store(data, path)


def save(store, path=None):
    """Atomically replace the whole store (temp file, fsync, rename).

    Low-level whole-store replacement; ordinary mutations go through
    `_mutate` so they read and write under one lock. On failure the previous
    file is preserved and the temporary file is removed.
    """
    path = Path(path) if path is not None else DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(store, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError) as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise PlaylistStorageError(
            f"Could not write the local playlist file at {path} ({exc}); "
            "the previous file was preserved."
        ) from exc
    return path


def _mutate(path, operation):
    """Run one read-modify-write transaction and return the operation result.

    The lock covers the strict load, the change and the atomic replace, so
    two writers cannot each start from a stale snapshot. The lock is never
    held across network or player calls: the operation only touches the
    parsed store.
    """
    path = Path(path) if path is not None else DEFAULT_PATH
    with _file_lock(path), _MUTATION_LOCK:
        store = _load_for_mutation(path)
        result = operation(store)
        save(store, path)
    return result


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
    def operation(store):
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
        return playlist_id

    return _mutate(path, operation)


def add_items(playlist_id, tracks, path=None):
    """Append `tracks` (Track objects) to a local playlist.

    Raises KeyError if `playlist_id` does not exist.
    """
    def operation(store):
        entry = _find(store, playlist_id)
        if entry is None:
            raise KeyError(playlist_id)
        if not isinstance(entry.get("tracks"), list):
            entry["tracks"] = []
        entry["tracks"].extend(track_to_dict(t) for t in tracks)
        return len(entry["tracks"])

    return _mutate(path, operation)


def remove_items(playlist_id, video_ids, path=None):
    """Remove tracks matching `video_ids` from a local playlist.

    Raises KeyError if `playlist_id` does not exist.
    """
    def operation(store):
        entry = _find(store, playlist_id)
        if entry is None:
            raise KeyError(playlist_id)
        wanted = set(video_ids)
        before = len(entry["tracks"])
        entry["tracks"] = [t for t in entry["tracks"] if t.get("video_id") not in wanted]
        return before - len(entry["tracks"])

    return _mutate(path, operation)


def delete(playlist_id, path=None):
    """Delete a local playlist. Returns whether it existed."""
    def operation(store):
        entry = _find(store, playlist_id)
        if entry is None:
            return False
        store["playlists"].remove(entry)
        return True

    return _mutate(path, operation)
