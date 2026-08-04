"""Tests for ytm.daemon.queue with a fake player, resolver and API client.

Nothing here touches real mpv, real yt-dlp or the network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ytm.api import Track
from ytm.daemon import queue as queue_mod
from ytm.daemon.queue import Queue


class FakePlayer:
    """Records loads and captures the observers Queue registers."""

    def __init__(self):
        self.loaded = []
        self._on_eof = None
        self._on_position = None

    def load(self, url):
        self.loaded.append(url)

    def on_eof(self, callback):
        self._on_eof = callback

    def on_position(self, callback):
        self._on_position = callback

    def emit_eof(self):
        self._on_eof()

    def emit_position(self, value):
        self._on_position(value)


class FakeResolver:
    """Counts resolutions per videoId and returns a distinguishable URL."""

    def __init__(self):
        self.calls = []
        self.counter = 0

    def __call__(self, video_id):
        self.counter += 1
        self.calls.append(video_id)
        return f"https://stream.test/{video_id}?t={self.counter}"


class FakeYT:
    """Stands in for ytmusicapi's get_watch_playlist."""

    def __init__(self, tracks=None):
        self.tracks = tracks if tracks is not None else []
        self.calls = []

    def get_watch_playlist(self, videoId):
        self.calls.append(videoId)
        return {"tracks": self.tracks}


def track(video_id, duration_seconds=180):
    return Track(
        video_id=video_id,
        title=f"title-{video_id}",
        artist="artist",
        album="album",
        duration="3:00",
        duration_seconds=duration_seconds,
    )


def raw(video_id):
    return {"videoId": video_id, "title": f"title-{video_id}", "duration": "3:00"}


@pytest.fixture
def q():
    player = FakePlayer()
    resolver = FakeResolver()
    yt = FakeYT()
    return Queue(player, resolver=resolver, yt=yt), player, resolver, yt


# -- criterion 1: next/prev at both boundaries ---------------------------


def test_next_at_tail_without_radio_does_not_move_or_crash():
    player, resolver = FakePlayer(), FakeResolver()
    q = Queue(player, resolver=resolver, yt=FakeYT(), autoplay_radio=False)
    q.enqueue([track("a"), track("b")])
    assert q.next().video_id == "b"
    assert q.next() is None
    assert q.index == 1
    assert q.current.video_id == "b"


def test_prev_at_head_restarts_current_and_keeps_cursor(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b")])
    assert q.prev().video_id == "a"
    assert q.index == 0
    assert len(player.loaded) == 2  # initial play + restart
    assert resolver.calls == ["a", "a"]


def test_next_and_prev_on_empty_queue_return_none(q):
    q, _, _, _ = q
    assert q.next() is None
    assert q.prev() is None
    assert q.index == -1
    assert q.current is None


def test_prev_from_tail_steps_back(q):
    q, _, _, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    q.next()
    q.next()
    assert q.index == 2
    assert q.prev().video_id == "b"
    assert q.prev().video_id == "a"
    assert q.prev().video_id == "a"
    assert q.index == 0


# -- criterion 2: move/remove index integrity -----------------------------


def test_remove_before_cursor_keeps_same_current(q):
    q, _, _, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    q.next()
    assert q.current.video_id == "b"
    q.remove(0)
    assert q.current.video_id == "b"
    assert q.index == 0
    assert [t.video_id for t in q.tracks] == ["b", "c"]


def test_remove_current_plays_next(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    q.remove(0)
    assert q.index == 0
    assert q.current.video_id == "b"
    assert resolver.calls == ["a", "b"]
    assert player.loaded[-1].startswith("https://stream.test/b")


def test_remove_last_current_stops_and_keeps_valid_cursor(q):
    q, player, _, _ = q
    q.enqueue([track("a"), track("b")])
    q.next()
    loads = len(player.loaded)
    q.remove(1)
    assert q.index == 0
    assert q.current.video_id == "a"
    assert len(player.loaded) == loads  # playback stopped, nothing new loaded


def test_remove_only_track_empties_queue(q):
    q, _, _, _ = q
    q.enqueue(track("a"))
    q.remove(0)
    assert q.tracks == []
    assert q.index == -1
    assert q.current is None


def test_move_before_cursor_preserves_current(q):
    q, _, _, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    q.next()
    q.move(0, 2)
    assert [t.video_id for t in q.tracks] == ["b", "c", "a"]
    assert q.current.video_id == "b"
    assert q.index == 0


def test_move_current_track_follows_cursor(q):
    q, _, _, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    q.move(0, 2)
    assert [t.video_id for t in q.tracks] == ["b", "c", "a"]
    assert q.current.video_id == "a"
    assert q.index == 2


def test_enqueue_before_cursor_shifts_cursor(q):
    q, _, _, _ = q
    q.enqueue([track("a"), track("b")])
    q.next()
    q.enqueue(track("z"), position=0)
    assert [t.video_id for t in q.tracks] == ["z", "a", "b"]
    assert q.current.video_id == "b"
    assert q.index == 2


def test_out_of_range_move_and_remove_raise_without_corrupting(q):
    q, _, _, _ = q
    q.enqueue([track("a"), track("b")])
    with pytest.raises(IndexError):
        q.remove(5)
    with pytest.raises(IndexError):
        q.move(5, 0)
    assert [t.video_id for t in q.tracks] == ["a", "b"]
    assert q.index == 0


# -- criterion 3: radio autoplay -----------------------------------------


def test_radio_fills_when_queue_empties():
    player, resolver = FakePlayer(), FakeResolver()
    yt = FakeYT([raw("a"), raw("r1"), raw("r2")])
    q = Queue(player, resolver=resolver, yt=yt)
    q.enqueue(track("a"))
    assert q.next().video_id == "r1"
    assert yt.calls == ["a"]
    assert [t.video_id for t in q.tracks] == ["a", "r1", "r2"]


def test_radio_not_called_while_queue_has_items():
    player = FakePlayer()
    yt = FakeYT([raw("r1")])
    q = Queue(player, resolver=FakeResolver(), yt=yt)
    q.enqueue([track("a"), track("b"), track("c")])
    q.next()
    q.next()
    assert yt.calls == []
    assert [t.video_id for t in q.tracks] == ["a", "b", "c"]


def test_radio_not_called_when_disabled():
    player = FakePlayer()
    yt = FakeYT([raw("r1")])
    q = Queue(player, resolver=FakeResolver(), yt=yt, autoplay_radio=False)
    q.enqueue(track("a"))
    assert q.next() is None
    assert yt.calls == []


def test_eof_at_tail_triggers_radio_and_continues():
    player, resolver = FakePlayer(), FakeResolver()
    yt = FakeYT([raw("r1")])
    q = Queue(player, resolver=resolver, yt=yt)
    q.enqueue(track("a"))
    player.emit_eof()
    assert q.current.video_id == "r1"
    assert player.loaded[-1].startswith("https://stream.test/r1")


# -- criterion 4: prefetch is n+1 only -----------------------------------


def test_prefetch_resolves_next_only(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c"), track("d")])
    assert resolver.calls == ["a"]
    player.emit_position(179.0)
    assert resolver.calls == ["a", "b"]
    # repeated position events must not re-resolve, nor reach n+2
    player.emit_position(179.5)
    assert resolver.calls == ["a", "b"]
    assert "c" not in resolver.calls
    assert "d" not in resolver.calls


def test_prefetch_not_started_early(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b")])
    player.emit_position(10.0)
    assert resolver.calls == ["a"]


def test_prefetched_url_is_used_without_re_resolving(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b")])
    player.emit_position(179.0)
    prefetched = f"https://stream.test/b?t={resolver.counter}"
    q.next()
    assert player.loaded[-1] == prefetched
    assert resolver.calls == ["a", "b"]


# -- criterion 5: stale / invalidated prefetch ---------------------------


def test_stale_prefetch_is_discarded_and_re_resolved(q, monkeypatch):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b")])
    player.emit_position(179.0)
    stale = player.loaded[:]
    assert resolver.calls == ["a", "b"]

    real_monotonic = queue_mod.time.monotonic
    monkeypatch.setattr(
        queue_mod.time,
        "monotonic",
        lambda: real_monotonic() + queue_mod.PREFETCH_TTL_SECONDS + 1,
    )
    q.next()
    assert resolver.calls == ["a", "b", "b"]
    assert player.loaded[-1] not in stale


def test_reorder_invalidates_prefetch(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    player.emit_position(179.0)
    assert resolver.calls == ["a", "b"]
    prefetched = f"https://stream.test/b?t={resolver.counter}"
    q.move(2, 1)  # c is now next, not b
    q.next()
    assert q.current.video_id == "c"
    assert resolver.calls == ["a", "b", "c"]
    assert prefetched not in player.loaded


def test_skip_invalidates_prefetch(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    player.emit_position(179.0)
    prefetched = f"https://stream.test/b?t={resolver.counter}"
    q.next()  # consumes the prefetch for b
    assert player.loaded[-1] == prefetched
    q.next()  # c had never been prefetched -> resolved fresh
    assert resolver.calls == ["a", "b", "c"]
    assert q.current.video_id == "c"


def test_remove_of_prefetched_next_re_resolves(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    player.emit_position(179.0)
    prefetched = f"https://stream.test/b?t={resolver.counter}"
    q.remove(1)  # drop the prefetched track
    q.next()
    assert q.current.video_id == "c"
    assert prefetched not in player.loaded
    assert resolver.calls == ["a", "b", "c"]


# -- criterion 6: no resolved URL is ever written to disk ----------------


def test_queue_module_never_writes_to_disk():
    source = open(queue_mod.__file__).read()
    for forbidden in ("open(", "write(", "json.dump", "pickle", "Path(", "shutil"):
        assert forbidden not in source


def test_no_file_writes_during_full_playback_cycle(monkeypatch, tmp_path):
    """Fail loudly if any code path under Queue opens a file for writing."""
    real_open = open

    def guard_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"queue wrote to disk: {file!r} ({mode})")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guard_open)

    player, resolver = FakePlayer(), FakeResolver()
    yt = FakeYT([raw("r1"), raw("r2")])
    q = Queue(player, resolver=resolver, yt=yt)
    q.enqueue([track("a"), track("b")])
    player.emit_position(179.0)
    q.next()
    q.move(0, 1)
    q.remove(0)
    player.emit_eof()
    q.prev()
    q.clear()
    assert not list(tmp_path.iterdir())
