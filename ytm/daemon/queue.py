"""Playback queue module.

A queue state machine driving a ``ytm.daemon.player.Player``: an ordered list
of ``Track`` objects plus an integer cursor pointing at the current track.
Stream URLs are resolved at play time only (they expire within hours) and are
held in memory only -- nothing here writes a URL to disk.

Boundary behaviour of the cursor:

* ``next()`` at the tail: if ``autoplay_radio`` is enabled the queue is
  refilled from YouTube Music's radio for the last-played track and playback
  continues; otherwise playback simply stops, the cursor stays on the last
  track and ``next()`` returns ``None``.
* ``prev()`` at the head: the cursor stays at index 0 and the current track
  restarts from the beginning.

Gapless playback is achieved by resolving the URL of the next track (n+1
only, never further ahead) shortly before it is needed. A prefetched URL is
discarded and re-resolved if it has gone stale or if it no longer belongs to
the track that is actually next -- e.g. after a reorder or a skip.
"""

import time

from ytm import api, resolve

#: how long before the end of the current track to resolve the next one
PREFETCH_LEAD_SECONDS = 15.0

#: how long a prefetched URL may be trusted before it is re-resolved
PREFETCH_TTL_SECONDS = 300.0

#: consecutive load failures tolerated before the circuit breaker stops
#: advancing the queue, so a run of dead tracks can't skip forever
MAX_CONSECUTIVE_FAILURES = 3


class Queue:
    """An ordered list of Tracks with a cursor, driving a Player."""

    def __init__(self, player, resolver=None, yt=None, autoplay_radio=True):
        self._player = player
        self._resolver = resolver or resolve.resolve_stream_url
        self._yt = yt
        self._autoplay_radio = autoplay_radio

        self._tracks = []
        self._index = -1
        #: (video_id, url, monotonic timestamp) for the next track, or None
        self._prefetch = None
        self._consecutive_failures = 0
        self._on_error = None

        self._player.on_eof(self._handle_eof)
        self._player.on_position(self._handle_position)
        self._player.on_error(self._handle_error)

    # -- inspection --------------------------------------------------------

    @property
    def tracks(self):
        """The full queue in play order."""
        return list(self._tracks)

    @property
    def index(self):
        """Cursor position, or -1 when the queue is empty."""
        return self._index

    @property
    def current(self):
        """The currently selected Track, or None when the queue is empty."""
        if 0 <= self._index < len(self._tracks):
            return self._tracks[self._index]
        return None

    def __len__(self):
        return len(self._tracks)

    # -- queue operations --------------------------------------------------

    def enqueue(self, tracks, position=None):
        """Add one Track or a list of Tracks, at `position` or at the end.

        If nothing was playing, the first added track becomes current and
        starts playing.
        """
        if isinstance(tracks, api.Track):
            tracks = [tracks]
        tracks = list(tracks)
        if not tracks:
            return
        was_empty = not self._tracks
        if position is None:
            self._tracks.extend(tracks)
        else:
            position = max(0, min(len(self._tracks), position))
            self._tracks[position:position] = tracks
            if position <= self._index:
                self._index += len(tracks)
        self._invalidate_prefetch()
        if was_empty:
            self._index = 0
            self._play_current()

    def move(self, from_index, to_index):
        """Move the track at `from_index` to `to_index`, keeping the cursor
        on the same track it was already on."""
        if not (0 <= from_index < len(self._tracks)):
            raise IndexError(from_index)
        to_index = max(0, min(len(self._tracks) - 1, to_index))
        if from_index == to_index:
            return
        current = self.current
        track = self._tracks.pop(from_index)
        self._tracks.insert(to_index, track)
        if current is not None:
            self._index = self._tracks.index(current)
        self._invalidate_prefetch()

    def remove(self, index):
        """Remove the track at `index`.

        Removing a track before the cursor shifts the cursor to keep it on
        the same track. Removing the currently-playing track makes the next
        track current and starts playing it; if the removed track was the
        last one, playback stops.
        """
        if not (0 <= index < len(self._tracks)):
            raise IndexError(index)
        was_current = index == self._index
        self._tracks.pop(index)
        self._invalidate_prefetch()
        if not self._tracks:
            self._index = -1
            return
        if index < self._index:
            self._index -= 1
        elif was_current:
            if self._index >= len(self._tracks):
                self._index = len(self._tracks) - 1
            else:
                self._play_current()

    def clear(self):
        """Empty the queue and reset the cursor."""
        self._tracks = []
        self._index = -1
        self._invalidate_prefetch()

    def next(self):
        """Advance to the next track and play it; returns the new current.

        At the tail this refills from radio when autoplay is enabled, and
        otherwise returns None leaving the cursor on the last track.
        """
        if not self._tracks:
            return None
        if self._index >= len(self._tracks) - 1:
            if not self._fill_radio():
                return None
        self._index += 1
        self._play_current()
        return self.current

    def prev(self):
        """Step back one track, or restart the current one at the head."""
        if not self._tracks:
            return None
        if self._index > 0:
            self._index -= 1
        self._play_current()
        return self.current

    def play_at(self, index):
        """Move the cursor to `index` and (re)play it; returns the new current."""
        if not (0 <= index < len(self._tracks)):
            raise IndexError(index)
        self._index = index
        self._play_current()
        return self.current

    # -- playback ----------------------------------------------------------

    def _play_current(self):
        """Resolve the current track's URL now and hand it to the player."""
        track = self.current
        if track is None:
            return
        url = self._take_prefetched(track.video_id)
        if url is None:
            url = self._resolver(track.video_id)
        self._player.load(url)
        self._invalidate_prefetch()

    def on_error(self, callback):
        """Register a callback invoked with a message when playback fails."""
        self._on_error = callback

    def _handle_eof(self):
        self._consecutive_failures = 0
        self.next()

    def _handle_error(self, message=None):
        """A track failed to load. Skip it, unless too many failed in a row.

        Resets only on a genuine successful playback (see `_handle_eof`), so
        a run of dead tracks trips the breaker instead of skipping through
        the whole queue. It is deliberately *not* reset by `_handle_position`
        -- mpv reports an initial time-pos of 0.0 for a file it is about to
        fail to open, and resetting on that would defeat the breaker.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            if self._on_error is not None:
                self._on_error(
                    f"stopped after {self._consecutive_failures} consecutive "
                    f"playback failures: {message or 'playback error'}"
                )
            return
        if self._on_error is not None:
            self._on_error(message or "playback error")
        self.next()

    def _handle_position(self, position):
        """Prefetch the next track's URL as the current one nears its end."""
        track = self.current
        if track is None:
            return
        duration = track.duration_seconds
        if not duration:
            return
        if duration - position > PREFETCH_LEAD_SECONDS:
            return
        self._prefetch_next()

    # -- prefetch ----------------------------------------------------------

    def _next_track(self):
        """The track after the cursor, or None if the cursor is at the tail."""
        if 0 <= self._index < len(self._tracks) - 1:
            return self._tracks[self._index + 1]
        return None

    def _prefetch_next(self):
        """Resolve the URL of n+1 only, at most once while it stays valid."""
        upcoming = self._next_track()
        if upcoming is None:
            return
        if self._prefetch is not None and self._prefetch_valid(upcoming.video_id):
            return
        self._prefetch = (
            upcoming.video_id,
            self._resolver(upcoming.video_id),
            time.monotonic(),
        )

    def _prefetch_valid(self, video_id):
        """Whether the held prefetch is fresh and for `video_id`."""
        if self._prefetch is None:
            return False
        held_id, _, resolved_at = self._prefetch
        if held_id != video_id:
            return False
        return time.monotonic() - resolved_at <= PREFETCH_TTL_SECONDS

    def _take_prefetched(self, video_id):
        """Consume the prefetched URL for `video_id`, or None if unusable."""
        if not self._prefetch_valid(video_id):
            self._invalidate_prefetch()
            return None
        url = self._prefetch[1]
        self._prefetch = None
        return url

    def _invalidate_prefetch(self):
        """Drop a prefetch that no longer matches the actual next track."""
        if self._prefetch is None:
            return
        upcoming = self._next_track()
        if upcoming is None or upcoming.video_id != self._prefetch[0]:
            self._prefetch = None

    # -- radio -------------------------------------------------------------

    def _fill_radio(self):
        """Append radio tracks for the last-played track; True if any added."""
        if not self._autoplay_radio:
            return False
        seed = self.current
        if seed is None:
            return False
        yt = self._yt if self._yt is not None else _default_client()
        watch = yt.get_watch_playlist(videoId=seed.video_id)
        results = (watch or {}).get("tracks") or []
        tracks = [
            track
            for track in api.to_tracks(results)
            if track.video_id != seed.video_id
        ]
        if not tracks:
            return False
        self._tracks.extend(tracks)
        return True


def _default_client():
    """The authenticated ytmusicapi client, imported lazily."""
    from ytm import auth

    return auth.client()
