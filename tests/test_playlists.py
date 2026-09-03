"""Tests for T8: local playlist store, playlist daemon routes, confirmation
gating on destructive remote operations, and the cache-aware resolver wiring.

Everything here is offline: a fake ytmusicapi client, temp dirs, no network.
"""

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
