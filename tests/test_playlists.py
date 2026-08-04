"""Tests for T8: local playlist store, playlist daemon routes, confirmation
gating on destructive remote operations, and the cache-aware resolver wiring.

Everything here is offline: a fake ytmusicapi client, temp dirs, no network.
"""

import asyncio
import os

import pytest

from ytm import playlists_local
from ytm.api import Track
from ytm.daemon.server import Daemon
from ytm.daemon.queue import Queue

from tests.test_server import FakePlayer, FakeQueue, build_daemon


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


def build_playlist_daemon(tmp_path, yt=None):
    player = FakePlayer()
    queue = FakeQueue(player)
    daemon = Daemon(
        path=tmp_path / "ytmd.sock",
        player=player,
        queue=queue,
        state_path=tmp_path / "state.json",
        playlists_path=tmp_path / "playlists.json",
        yt=yt if yt is not None else FakeYT(),
    )
    return daemon


def _run(daemon, cmd, args=None):
    request = {"id": 1, "cmd": cmd, "args": args or {}}
    return asyncio.run(daemon.handle_request(request))


# -- 5. every new playlist_* route returns a well-formed response ----------


def test_playlist_create_local_and_add_get_list(tmp_path):
    daemon = build_playlist_daemon(tmp_path)

    resp = _run(daemon, "playlist_create", {"name": "scratch", "local": True})
    assert resp["ok"] is True
    pid = resp["data"]["playlist_id"]
    assert pid.startswith("local-")

    resp = _run(
        daemon,
        "playlist_add",
        {"playlist_id": pid, "video_ids": ["v1"], "tracks": [{"video_id": "v1", "title": "T"}]},
    )
    assert resp["ok"] is True and resp["data"]["added"] == 1

    resp = _run(daemon, "playlist_get", {"playlist_id": pid})
    assert resp["ok"] is True
    assert resp["data"]["tracks"][0]["title"] == "T"

    resp = _run(daemon, "playlist_list")
    assert resp["ok"] is True
    assert any(p["playlist_id"] == pid and p["local"] for p in resp["data"]["playlists"])


def test_playlist_create_remote_dispatches_to_api(tmp_path):
    yt = FakeYT()
    daemon = build_playlist_daemon(tmp_path, yt=yt)
    resp = _run(daemon, "playlist_create", {"name": "Deep Work"})
    assert resp["ok"] is True
    assert ("create_playlist", "Deep Work") in yt.calls


def test_playlist_malformed_args_return_ok_false_daemon_alive(tmp_path):
    daemon = build_playlist_daemon(tmp_path)
    resp = _run(daemon, "playlist_get", {})  # missing playlist_id
    assert resp["ok"] is False

    resp = _run(daemon, "playlist_add", {"playlist_id": "local-nope"})  # missing video_ids
    assert resp["ok"] is False

    resp = _run(daemon, "playlist_add", {"playlist_id": "local-nope", "video_ids": []})
    assert resp["ok"] is False

    # daemon still alive and routable after malformed input
    resp = _run(daemon, "playlist_list")
    assert resp["ok"] is True


# -- 2 & 3. confirmation gating on destructive ops -------------------------


def test_remote_delete_refused_without_confirmation_no_api_call(tmp_path):
    yt = FakeYT()
    yt.playlists["remote-x"] = {"title": "x", "tracks": []}
    daemon = build_playlist_daemon(tmp_path, yt=yt)

    resp = _run(daemon, "playlist_delete", {"playlist_id": "remote-x"})
    assert resp["ok"] is False
    assert not any(c[0] == "delete_playlist" for c in yt.calls)


def test_remote_delete_proceeds_when_confirmed(tmp_path):
    yt = FakeYT()
    yt.playlists["remote-x"] = {"title": "x", "tracks": []}
    daemon = build_playlist_daemon(tmp_path, yt=yt)

    resp = _run(daemon, "playlist_delete", {"playlist_id": "remote-x", "confirm": True})
    assert resp["ok"] is True
    assert ("delete_playlist", "remote-x") in yt.calls


def test_remote_bulk_remove_refused_without_confirmation_no_api_call(tmp_path):
    yt = FakeYT()
    yt.playlists["remote-x"] = {"title": "x", "tracks": [{"videoId": "v1"}]}
    daemon = build_playlist_daemon(tmp_path, yt=yt)

    resp = _run(
        daemon, "playlist_remove", {"playlist_id": "remote-x", "video_ids": ["v1"]}
    )
    assert resp["ok"] is False
    assert not any(c[0] == "remove_playlist_items" for c in yt.calls)


def test_remote_bulk_remove_proceeds_when_confirmed(tmp_path):
    yt = FakeYT()
    yt.playlists["remote-x"] = {"title": "x", "tracks": [{"videoId": "v1"}]}
    daemon = build_playlist_daemon(tmp_path, yt=yt)

    resp = _run(
        daemon,
        "playlist_remove",
        {"playlist_id": "remote-x", "video_ids": ["v1"], "confirm": True},
    )
    assert resp["ok"] is True
    assert any(c[0] == "remove_playlist_items" for c in yt.calls)


def test_local_destructive_ops_need_no_confirmation(tmp_path):
    daemon = build_playlist_daemon(tmp_path)
    pid = _run(daemon, "playlist_create", {"name": "scratch", "local": True})["data"][
        "playlist_id"
    ]
    _run(
        daemon,
        "playlist_add",
        {"playlist_id": pid, "video_ids": ["v1"]},
    )
    resp = _run(daemon, "playlist_remove", {"playlist_id": pid, "video_ids": ["v1"]})
    assert resp["ok"] is True

    resp = _run(daemon, "playlist_delete", {"playlist_id": pid})
    assert resp["ok"] is True


# -- 6. cache-aware resolver wiring -----------------------------------------


def test_daemon_constructs_queue_with_cache_aware_resolver(tmp_path, monkeypatch):
    from ytm import cache, resolve

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached_file = cache_dir / "cachedvid.m4a"
    cached_file.write_bytes(b"fake audio")

    calls = []

    def fake_resolve_stream_url(video_id):
        calls.append(video_id)
        return "http://network/" + video_id

    monkeypatch.setattr(resolve, "resolve_stream_url", fake_resolve_stream_url)
    monkeypatch.setattr(cache, "DEFAULT_CACHE_DIR", cache_dir)

    player = FakePlayer()
    daemon = Daemon(
        path=tmp_path / "ytmd.sock",
        player=player,
        state_path=tmp_path / "state.json",
        playlists_path=tmp_path / "playlists.json",
        yt=object(),
    )
    assert isinstance(daemon._queue, Queue)

    daemon._queue.enqueue(make_track("cachedvid"))
    assert calls == []
    assert ("load", str(cached_file)) in player.calls
