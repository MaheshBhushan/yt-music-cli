"""Tests for T8: local playlist store, playlist daemon routes, confirmation
gating on destructive remote operations, and the cache-aware resolver wiring.

Everything here is offline: a fake ytmusicapi client, temp dirs, no network.
"""

import json
import os

import pytest

from ytm import playlists_local
from ytm.music import Track


def make_track(video_id, title=None):
    return Track(
        video_id=video_id,
        title=title or f"title-{video_id}",
        artist="artist",
        album="album",
        duration="3:00",
        duration_seconds=180,
    )


class FakeYT:
    """Stands in for ytmusicapi's playlist surface, recording every call."""

    def __init__(self):
        self.calls = []
        self.library = []
        self.playlists = {}

    def get_library_playlists(self, limit=None):
        self.calls.append(("get_library_playlists",))
        return self.library

    def get_playlist(self, playlist_id, limit=None):
        self.calls.append(("get_playlist", playlist_id))
        return self.playlists.get(playlist_id, {"title": "x", "tracks": []})

    def create_playlist(self, title, description, privacy_status=None):
        self.calls.append(("create_playlist", title))
        pid = f"remote-{title}"
        self.playlists[pid] = {"title": title, "tracks": []}
        return pid

    def add_playlist_items(self, playlist_id, video_ids):
        self.calls.append(("add_playlist_items", playlist_id, tuple(video_ids)))
        return {"status": "ok"}

    def remove_playlist_items(self, playlist_id, items):
        self.calls.append(("remove_playlist_items", playlist_id, len(items)))
        return {"status": "ok"}

    def delete_playlist(self, playlist_id):
        self.calls.append(("delete_playlist", playlist_id))
        return {"status": "ok"}

    def edit_playlist(self, playlist_id, **kwargs):
        self.calls.append(("edit_playlist", playlist_id))
        return {"status": "ok"}


# -- 1. local store CRUD round-trips + interrupted write safety -----------


def test_local_store_crud_roundtrip(tmp_path):
    path = tmp_path / "playlists.json"
    pid = playlists_local.create("scratch", description="d", path=path)
    assert pid.startswith("local-")

    playlists_local.add_items(pid, [make_track("v1"), make_track("v2")], path=path)
    playlist, tracks = playlists_local.get_playlist(pid, path=path)
    assert playlist.title == "scratch"
    assert playlist.track_count == 2
    assert [t.video_id for t in tracks] == ["v1", "v2"]

    listed = playlists_local.list_playlists(path=path)
    assert len(listed) == 1 and listed[0].local is True

    removed = playlists_local.remove_items(pid, ["v1"], path=path)
    assert removed == 1
    _, tracks = playlists_local.get_playlist(pid, path=path)
    assert [t.video_id for t in tracks] == ["v2"]

    assert playlists_local.delete(pid, path=path) is True
    assert playlists_local.list_playlists(path=path) == []


def test_local_store_survives_interrupted_write(tmp_path):
    path = tmp_path / "playlists.json"
    pid = playlists_local.create("scratch", path=path)
    good_contents = path.read_bytes()

    # Simulate a crash mid-write: a stray temp file from a pid that never
    # got to os.replace() must not disturb the real file.
    stray_tmp = path.with_name(path.name + ".tmp999999")
    stray_tmp.write_bytes(b"{not valid json")

    store = playlists_local.load(path=path)
    assert store["playlists"][0]["playlist_id"] == pid
    assert path.read_bytes() == good_contents
    os.unlink(stray_tmp)


def test_local_store_corrupt_file_degrades_to_empty(tmp_path):
    path = tmp_path / "playlists.json"
    path.write_text("not json at all")
    assert playlists_local.load(path=path) == {"playlists": []}


# -- daemon route wiring ----------------------------------------------------


# -- D2: mutations are transactions across threads and processes --------------


def _create_in_child(path, title, gate):
    """Spawn-safe helper for the cross-process create test."""
    from ytm import playlists_local as child_store

    gate.wait(5)
    child_store.create(title, path=path)


def _run_threads(worker, count=2, timeout=10):
    """Start `count` daemon threads and bound the join, never hanging a test."""
    import threading

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
    assert not any(thread.is_alive() for thread in threads), "a mutation worker never finished"


def test_concurrent_thread_creates_preserve_both(tmp_path):
    import threading

    path = tmp_path / "playlists.json"
    start = threading.Barrier(2, timeout=5)

    def worker(index):
        start.wait(5)  # synchronize before entering the transaction
        playlists_local.create(f"list-{index}", path=path)

    _run_threads(worker)
    titles = sorted(p["title"] for p in json.loads(path.read_text())["playlists"])
    assert titles == ["list-0", "list-1"]


def test_concurrent_process_creates_preserve_both(tmp_path):
    import multiprocessing

    path = tmp_path / "playlists.json"
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    workers = [
        context.Process(target=_create_in_child, args=(path, title, gate))
        for title in ("from-a", "from-b")
    ]
    for worker in workers:
        worker.start()
    gate.set()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    titles = sorted(p["title"] for p in json.loads(path.read_text())["playlists"])
    assert titles == ["from-a", "from-b"]


def test_concurrent_adds_to_one_playlist_keep_both(tmp_path):
    import threading

    path = tmp_path / "playlists.json"
    playlist_id = playlists_local.create("mix", path=path)
    start = threading.Barrier(2, timeout=5)

    def worker(index):
        start.wait(5)
        tracks = [make_track("a1"), make_track("a2")] if index == 0 else [make_track("b1")]
        playlists_local.add_items(playlist_id, tracks, path=path)

    _run_threads(worker)
    _, tracks = playlists_local.get_playlist(playlist_id, path=path)
    assert sorted(t.video_id for t in tracks) == ["a1", "a2", "b1"]


def test_concurrent_edits_of_different_playlists_keep_both(tmp_path):
    import threading

    path = tmp_path / "playlists.json"
    first = playlists_local.create("first", path=path)
    second = playlists_local.create("second", path=path)
    start = threading.Barrier(2, timeout=5)

    def worker(index):
        start.wait(5)
        if index == 0:
            playlists_local.add_items(first, [make_track("v1")], path=path)
        else:
            playlists_local.remove_items(second, ["v2"], path=path)

    playlists_local.add_items(second, [make_track("v2"), make_track("v3")], path=path)
    _run_threads(worker)
    assert [t.video_id for t in playlists_local.get_playlist(first, path=path)[1]] == ["v1"]
    assert [t.video_id for t in playlists_local.get_playlist(second, path=path)[1]] == ["v3"]


def test_a_delete_add_race_has_a_defined_serial_outcome(tmp_path):
    import threading

    path = tmp_path / "playlists.json"
    playlist_id = playlists_local.create("doomed", path=path)
    start = threading.Barrier(2, timeout=5)
    outcome = []

    def worker(index):
        start.wait(5)
        if index == 0:
            playlists_local.delete(playlist_id, path=path)
        else:
            try:
                playlists_local.add_items(playlist_id, [make_track("late")], path=path)
                outcome.append("added")
            except KeyError:
                outcome.append("refused")

    _run_threads(worker)
    # either the add landed first and the delete removed it, or the add was
    # refused: the playlist is gone and the file is intact either way
    assert outcome in (["added"], ["refused"])
    data = json.loads(path.read_text())
    assert all(p["playlist_id"] != playlist_id for p in data["playlists"])


# -- D5: corruption and write failures must not destroy data ------------------


def test_a_failed_replace_preserves_the_original_file(tmp_path, monkeypatch):
    path = tmp_path / "playlists.json"
    playlists_local.create("keep", path=path)
    before = path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(playlists_local.os, "replace", fail_replace)
    with pytest.raises(playlists_local.PlaylistStorageError, match="previous file was preserved"):
        playlists_local.create("lost", path=path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("playlists.json.tmp*"))


def test_a_failed_serialization_preserves_the_original_file(tmp_path, monkeypatch):
    path = tmp_path / "playlists.json"
    playlists_local.create("keep", path=path)
    before = path.read_bytes()

    def fail_dump(*args, **kwargs):
        raise ValueError("not serializable")

    monkeypatch.setattr(playlists_local.json, "dump", fail_dump)
    with pytest.raises(playlists_local.PlaylistStorageError, match="previous file was preserved"):
        playlists_local.create("lost", path=path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("playlists.json.tmp*"))


@pytest.mark.parametrize("mutate", [
    lambda path: playlists_local.create("x", path=path),
    lambda path: playlists_local.add_items("local-x", [make_track("v")], path=path),
    lambda path: playlists_local.remove_items("local-x", ["v"], path=path),
    lambda path: playlists_local.delete("local-x", path=path),
])
def test_mutations_refuse_invalid_json(tmp_path, mutate):
    path = tmp_path / "playlists.json"
    path.write_text("{broken")
    before = path.read_bytes()
    with pytest.raises(playlists_local.PlaylistStorageError, match="not valid JSON"):
        mutate(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("payload", ['["not", "a", "store"]', '{"playlists": {"not": "a list"}}'])
def test_mutations_refuse_an_invalid_top_level_shape(tmp_path, payload):
    path = tmp_path / "playlists.json"
    path.write_text(payload)
    before = path.read_bytes()
    with pytest.raises(playlists_local.PlaylistStorageError, match="expected shape"):
        playlists_local.create("x", path=path)
    assert path.read_bytes() == before


def test_a_broken_nested_entry_is_not_silently_discarded(tmp_path):
    path = tmp_path / "playlists.json"
    playlist_id = playlists_local.create("keep", path=path)
    data = json.loads(path.read_text())
    data["playlists"][0]["tracks"].append({"video_id": "good", "title": "G"})
    data["playlists"][0]["tracks"].append("not a track object")
    path.write_text(json.dumps(data))
    before = path.read_bytes()

    with pytest.raises(playlists_local.PlaylistStorageError, match="track without an id"):
        playlists_local.add_items(playlist_id, [make_track("new")], path=path)
    assert path.read_bytes() == before


def test_a_missing_file_is_a_new_store(tmp_path):
    path = tmp_path / "playlists.json"
    playlist_id = playlists_local.create("fresh", path=path)
    assert [p.playlist_id for p in playlists_local.list_playlists(path=path)] == [playlist_id]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission test")
def test_an_unreadable_store_is_not_an_empty_library(tmp_path):
    path = tmp_path / "playlists.json"
    path.write_text(json.dumps({"playlists": [{"playlist_id": "local-a", "title": "A", "tracks": []}]}))
    path.chmod(0)
    try:
        with pytest.raises(playlists_local.PlaylistStorageError):
            playlists_local.load(path=path)
        with pytest.raises(playlists_local.PlaylistStorageError):
            playlists_local.create("x", path=path)
    finally:
        path.chmod(0o600)


def test_existing_ids_order_and_metadata_survive_edits(tmp_path):
    path = tmp_path / "playlists.json"
    first = playlists_local.create("first", description="d1", path=path)
    second = playlists_local.create("second", privacy="PUBLIC", path=path)
    playlists_local.add_items(second, [make_track("v2")], path=path)
    playlists_local.add_items(first, [make_track("v1")], path=path)

    data = json.loads(path.read_text())
    assert [p["playlist_id"] for p in data["playlists"]] == [first, second]
    assert data["playlists"][0]["description"] == "d1"
    assert data["playlists"][1]["privacy"] == "PUBLIC"
    assert [t.video_id for t in playlists_local.get_playlist(first, path=path)[1]] == ["v1"]
