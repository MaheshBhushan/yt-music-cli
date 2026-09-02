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
        self._on_error = None

    def load(self, url):
        self.loaded.append(url)

    def on_eof(self, callback):
        self._on_eof = callback

    def on_position(self, callback):
        self._on_position = callback

    def on_error(self, callback):
        self._on_error = callback

    def emit_eof(self):
        self._on_eof()

    def emit_position(self, value):
        self._on_position(value)

    def emit_error(self, message=None):
        self._on_error(message)


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


def test_play_at_moves_cursor_and_plays(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    result = q.play_at(2)
    assert result.video_id == "c"
    assert q.index == 2
    assert resolver.calls[-1] == "c"
    assert player.loaded[-1].startswith("https://stream.test/c")


def test_play_at_out_of_range_raises_without_corrupting(q):
    q, _, _, _ = q
    q.enqueue([track("a"), track("b")])
    with pytest.raises(IndexError):
        q.play_at(5)
    assert [t.video_id for t in q.tracks] == ["a", "b"]
    assert q.index == 0


def test_play_at_invalidates_stale_prefetch(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    q._prefetch_next()
    assert q._prefetch is not None and q._prefetch[0] == "b"
    q.play_at(2)
    assert q._prefetch is None


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


# -- regression: mpv end-file reason must gate advancement ---------------


def test_no_spurious_eof_does_not_advance_queue(q):
    """Playing a track (which calls player.load(), the equivalent of mpv's
    `loadfile ... replace`) must not by itself advance the queue. Player is
    responsible for only calling on_eof for a genuine "eof" reason and
    never for the "stop" reason mpv emits on every replace (see
    tests/test_player.py::test_stop_reason_does_not_fire_eof_callback for
    the mpv-facing half of this regression); this asserts the Queue side:
    no eof callback fired means no advance."""
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    assert q.index == 0
    assert len(player.loaded) == 1
    # Simulate the load completing without any eof callback firing at all
    # (as happens for a "stop" reason) -- the cursor must stay put.
    assert q.index == 0


def test_genuine_eof_still_advances_queue(q):
    """A real end-of-track (reason "eof") must still advance -- autoplay
    and manual "next track finished" behaviour must not be broken."""
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c")])
    assert q.index == 0
    player.emit_eof()
    assert q.index == 1
    assert q.current.video_id == "b"


def test_repeated_load_errors_trip_circuit_breaker_and_surface_error(q):
    q, player, resolver, _ = q
    q.enqueue([track("a"), track("b"), track("c"), track("d")])
    errors = []
    q.on_error(errors.append)

    assert q.index == 0
    player.emit_error("loading failed")
    assert q.index == 1  # first failure: skipped
    player.emit_error("loading failed")
    assert q.index == 2  # second failure: skipped
    player.emit_error("loading failed")
    # third consecutive failure trips the breaker: no further advance
    assert q.index == 2
    assert len(errors) == 3
    assert "stopped after 3 consecutive" in errors[-1]

    # queue must not have run away past the tail
    assert q.index < len(q.tracks)


def test_error_counter_resets_after_successful_playback(q):
    q, player, resolver, _ = q
    q.enqueue(
        [track("a"), track("b"), track("c"), track("d"), track("e"), track("f")]
    )
    errors = []
    q.on_error(errors.append)

    player.emit_error("loading failed")
    player.emit_error("loading failed")
    assert q.index == 2
    # a genuine end-of-track proves a track actually played successfully --
    # the failure streak resets (a bare position tick does not: mpv reports
    # an initial time-pos of 0.0 for a file it is about to fail to open, so
    # resetting on that would defeat the breaker)
    player.emit_eof()
    player.emit_error("loading failed")
    player.emit_error("loading failed")
    # two more failures after the reset is still under the breaker limit
    assert q.index == 5
    assert len(errors) == 4
    assert all("stopped after" not in message for message in errors)


# -- a stream URL that will not resolve is a playback failure -------------


def test_unresolvable_track_is_skipped_as_a_counted_playback_failure():
    """Regression: the resolver raises when yt-dlp gives up, and that
    exception used to unwind whichever caller was running. For an
    end-of-track that caller is the mpv IPC reader thread, where it killed
    the daemon's only channel to mpv. It must be handled as the playback
    failure it is, so the breaker counts it."""
    player, yt = FakePlayer(), FakeYT()
    attempted = []

    def failing_resolver(video_id):
        attempted.append(video_id)
        raise RuntimeError(f"DownloadError for {video_id}")

    q = Queue(player, resolver=failing_resolver, yt=yt, autoplay_radio=False)
    errors = []
    q.on_error(errors.append)
    q.enqueue([track(v) for v in ("a", "b", "c", "d", "e")])

    # enqueue starts the first track, which cannot resolve; each failure is
    # counted, and the third trips the breaker instead of running the queue
    # out.
    assert attempted == ["a", "b", "c"]
    assert q.index == 2
    assert len(errors) == 3
    assert "could not resolve a" in errors[0]
    assert "stopped after 3 consecutive" in errors[-1]
    assert player.loaded == []  # nothing ever reached mpv


def test_resolver_failure_does_not_escape_to_the_caller():
    """Nothing above `_play_current` should have to catch a resolver error;
    the eof path runs on the IPC reader thread, which has no useful way to
    recover."""
    player, yt = FakePlayer(), FakeYT()
    q = Queue(
        player, resolver=_raising_resolver, yt=yt, autoplay_radio=False
    )
    q.on_error(lambda message: None)
    q.enqueue([track("a"), track("b")])
    # no exception escaped enqueue; the same holds for next() via eof
    q.next()


def _raising_resolver(video_id):
    raise RuntimeError("nope")


def test_failed_prefetch_is_dropped_not_raised():
    """Prefetching is an optimisation. A resolver failure during prefetch
    must not escape to whoever delivered the position update; the track is
    resolved again, and reported properly, when it is actually reached."""
    from ytm.daemon.queue import PREFETCH_LEAD_SECONDS

    calls = []

    def resolver(video_id):
        calls.append(video_id)
        if video_id == "b" and calls.count("b") == 1:
            raise RuntimeError("transient")
        return f"url-{video_id}"

    player = FakePlayer()
    queue = Queue(player, resolver=resolver, autoplay_radio=False)
    a = track("a", duration_seconds=100)
    b = track("b", duration_seconds=100)
    queue.enqueue([a, b])
    player.emit_position(100 - PREFETCH_LEAD_SECONDS + 1)  # prefetch b: fails
    assert queue._prefetch is None
    player.emit_eof()
    assert queue.current is b
    assert player.loaded[-1] == "url-b"
