"""The Textual TUI's connection to the core: mpv over IPC plus ytmusicapi.

Speaks the request/event vocabulary the TUI was written against, so the
panes did not have to change when the daemon went away. Requests that only
touch mpv return in a few milliseconds; the network ones (search, lyrics,
playlists) are the caller's job to keep off the UI thread.

Events come from a second mpv connection that observes properties; every
change is translated into the same `track_changed` / `position` /
`state_changed` / `queue_changed` events the TUI already understands.
"""

import threading
import time
from dataclasses import asdict

import requests
from ytmusicapi.exceptions import YTMusicError

from ytm import cache, music, playlists_local, state
from ytm.auth import AuthError
from ytm.music import Track
from ytm.player import PlayerError

#: YouTube's auto-playlists: ids are fixed and they cannot take plain inserts
LIKED_MUSIC_ID = "LM"
EPISODES_ID = "SE"

#: how long an add is remembered while YouTube has not shown it yet (seconds;
#: propagation takes a few seconds, well under this)
PENDING_ADD_TTL = 120.0

#: how long a search result set is reused for the same query. Typing, pausing
#: and typing on runs several searches, and backspacing runs the earlier ones
#: again; the catalogue does not change between two keystrokes.
SEARCH_TTL = 300.0

#: how many queries and how many tracklists of lyrics to keep
SEARCH_CACHE_SIZE = 32
LYRICS_CACHE_SIZE = 64

REMOTE_ERRORS = (AuthError, requests.RequestException, YTMusicError)


class BackendError(Exception):
    """A request the core could not carry out; shown as a banner by the TUI."""


def _from_args(args):
    args = args or {}
    video_id = args.get("video_id") or ""
    return Track(
        video_id=video_id,
        title=args.get("title") or video_id,
        artist=args.get("artist") or "",
        album=args.get("album") or "",
        duration=args.get("duration") or "",
        duration_seconds=int(args.get("duration_seconds") or 0),
        thumbnail=args.get("thumbnail") or "",
        liked=bool(args.get("liked")),
    )


def _label(track):
    return f"{track.title} / {track.artist}" if track.artist else track.title


def _resolve(entry, known):
    """A Track for one mpv playlist entry, from `known` or from mpv alone."""
    track = known.get(entry["video_id"]) if entry["video_id"] else None
    if track is not None:
        return track
    return Track(entry["video_id"] or "", entry["title"] or entry["url"], "", "", "", 0)


def _remember_in(cache, key, value, limit):
    """Store `value` in `cache`, dropping the oldest entry past `limit`."""
    cache.pop(key, None)  # re-insert at the end: most recent last
    cache[key] = value
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


class _InFlight:
    """One lyrics fetch in progress: whoever else asks for the same song
    waits on `done` and reads `result` or `error`."""

    __slots__ = ("done", "error", "result")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error = None


def _playlist_dict(playlist, kind=None):
    return {
        "playlist_id": playlist.playlist_id,
        "title": playlist.title,
        "track_count": playlist.track_count,
        "local": playlist.local,
        "kind": kind or ("local" if playlist.local else "remote"),
    }


class Backend:
    def __init__(self, player_factory=None):
        self._make_player = player_factory or _default_player
        try:
            self._player = self._make_player(spawn=True)
        except PlayerError as exc:
            raise BackendError(str(exc)) from exc
        self._subscribers = []
        self._closed = False
        # mixes are re-rolled by YouTube on every fetch, so a session keeps
        # the list and each tracklist until `mixes_refresh` asks for new ones
        self._mixes = None
        self._mix_tracks = {}
        # tracks added to a remote playlist that YouTube has not yet shown
        # in the playlist itself: {playlist_id: [(Track, monotonic time)]}.
        # An add is acknowledged at once but takes a few seconds to appear,
        # so a fetch straight after it would leave the new song out.
        self._pending_adds = {}
        # {(query, limit): (monotonic time, [track dicts])}; see SEARCH_TTL
        self._searches = {}
        # {video_id: (lyrics, source)}, so replaying a song is free
        self._lyrics_cache = {}
        # {video_id: _InFlight} for lyrics being fetched right now: a second
        # request for the same song waits for the first instead of asking
        # YouTube twice. The lock guards the two dicts only, never the fetch.
        self._lyrics_inflight = {}
        self._lyrics_lock = threading.Lock()
        # remote track counts the library listing does not carry, looked up
        # once per playlist instead of on every refresh of the pane
        self._playlist_counts = {}
        self._routes = {
            "status": self._status,
            "search": self._search,
            "play": lambda a: self._load(a, play=True),
            "enqueue": lambda a: self._load(a, play=False),
            "enqueue_next": lambda a: self._load(a, play=False, up_next=True),
            "pause": lambda a: self._transport("pause"),
            "resume": lambda a: self._transport("resume"),
            "toggle": lambda a: self._transport("toggle"),
            "next": lambda a: self._transport("next"),
            "prev": lambda a: self._transport("prev"),
            "seek": self._seek,
            "volume": self._volume,
            "queue_get": lambda a: self._queue(),
            "queue_clear": self._queue_clear,
            "queue_remove": self._queue_remove,
            "queue_move": self._queue_move,
            "queue_play": self._queue_play,
            "radio": self._radio,
            "lyrics": self._lyrics,
            "playlist_list": self._playlist_list,
            "playlist_get": self._playlist_get,
            "mixes_refresh": self._mixes_refresh,
            "playlist_add": self._playlist_add,
            "playlist_play": self._playlist_play,
            "playlist_create": self._playlist_create,
            "like_set": self._like_set,
            "shutdown": self._shutdown,
        }

    # -- requests -------------------------------------------------------------

    def request(self, cmd, args=None):
        handler = self._routes.get(cmd)
        if handler is None:
            raise BackendError(f"unknown command: {cmd}")
        # no lock here: handlers that talk to YouTube take seconds, and a
        # volume or pause request must not queue behind them. The player
        # connection serialises its own commands.
        try:
            return handler(args or {})
        except (PlayerError, AuthError) as exc:
            # both already read as sentences, and "AuthExpired: run ytm auth"
            # on the banner only buries the instruction
            raise BackendError(str(exc)) from exc
        except Exception as exc:  # network failure, unexpected response
            raise BackendError(f"{type(exc).__name__}: {exc}") from exc

    def _current(self):
        entries = self._player.playlist()
        current = next((entry for entry in entries if entry["current"]), None)
        if current is None:
            return None
        return _resolve(current, state.tracks_for([current["video_id"]]))

    def _status(self, args):
        s = self._player.status()
        current = None if s["idle"] else self._current()
        return {
            "current": asdict(current) if current else None,
            "index": s["index"],
            "count": s["count"],
            "paused": s["paused"],
            "volume": s["volume"],
            "position": s["position"],
        }

    def _search(self, args):
        query = args.get("query") or ""
        limit = int(args.get("limit") or 20)
        cached = self._searches.get((query, limit))
        if cached is not None and time.monotonic() - cached[0] < SEARCH_TTL:
            # still the last search as far as `ytm play 3` is concerned
            state.remember_search([_from_args(t) for t in cached[1]])
            return {"tracks": cached[1]}
        tracks = music.search(query, limit=limit)
        state.remember_search(tracks)
        results = [asdict(t) for t in tracks]
        _remember_in(self._searches, (query, limit), (time.monotonic(), results), SEARCH_CACHE_SIZE)
        return {"tracks": results}

    def _load(self, args, play, up_next=False):
        track = _from_args(args)
        if not track.video_id:
            raise BackendError("'video_id' is required")
        if not track.thumbnail:
            # a client that only knows the basics must not erase what a
            # search already told us about this track
            known = state.track_for(track.video_id)
            if known and known.thumbnail:
                track.thumbnail = known.thumbnail
        state.remember_tracks([track])
        if play:
            method = self._player.play
        elif up_next:
            method = self._player.enqueue_next
        else:
            method = self._player.enqueue
        method(cache.playback_url(track.video_id), title=_label(track))
        return self._status(args)

    def _transport(self, name):
        getattr(self._player, name)()
        return self._status({})

    def _seek(self, args):
        seconds = float(args.get("seconds") or 0)
        absolute = bool(args.get("absolute"))
        self._player.seek(seconds, absolute=absolute)
        return {"seconds": seconds, "absolute": absolute}

    def _volume(self, args):
        level = args.get("level")
        return {"volume": self._player.volume(None if level is None else float(level))}

    def _queue(self):
        """The queue as the panes want it: mpv's entries, filled in from the
        metadata ytm remembered when each was queued.

        The remembered metadata is read once for the whole queue. Asking per
        row re-read (and re-parsed) the state file for every entry, on every
        queue change -- and loading a playlist changes the queue once per
        track it holds.
        """
        entries = self._player.playlist()
        known = state.tracks_for(entry["video_id"] for entry in entries)
        tracks = [asdict(_resolve(entry, known)) for entry in entries]
        index = next((i for i, entry in enumerate(entries) if entry["current"]), -1)
        return {"tracks": tracks, "index": index}

    def _queue_clear(self, args):
        self._player.clear()
        return self._queue()

    def _queue_remove(self, args):
        self._player.remove(int(args["index"]))
        return self._queue()

    def _queue_move(self, args):
        self._player.move(int(args["from_index"]), int(args["to_index"]))
        return self._queue()

    def _queue_play(self, args):
        """Jump to queue entry `index` (a click or Enter on the queue pane)."""
        self._player.play_index(int(args["index"]))
        return self._status(args)

    def _radio(self, args):
        seed = _from_args(args)
        tracks = music.radio(seed.video_id)
        if not tracks:
            raise BackendError(f"no radio available for {seed.video_id}")
        state.remember_tracks([seed] + tracks)
        self._player.stop()
        self._player.play(cache.playback_url(seed.video_id), title=_label(seed))
        self._player.enqueue_many(
            (cache.playback_url(track.video_id), _label(track)) for track in tracks
        )
        return self._queue()

    def _lyrics(self, args):
        """Lyrics for one track, remembered for the rest of the session.

        Two requests to YouTube every time (the watch playlist for the
        lyrics id, then the lyrics), and the TUI asks again on every track
        change -- including when the queue comes back round to a song.
        """
        video_id = args.get("video_id")
        with self._lyrics_lock:
            found = self._lyrics_cache.get(video_id)
            waiter = None if found is not None else self._lyrics_inflight.get(video_id)
            owner = found is None and waiter is None
            if owner:
                waiter = self._lyrics_inflight[video_id] = _InFlight()
        if owner:
            try:
                # the client's session has a 30 s timeout, so a stalled fetch
                # ends and the entry below is always cleared
                found = music.get_lyrics(video_id, timestamps=True)
            except BaseException as exc:
                waiter.error = exc
                raise
            else:
                waiter.result = found
            finally:
                with self._lyrics_lock:
                    self._lyrics_inflight.pop(video_id, None)
                    if waiter.error is None:
                        _remember_in(self._lyrics_cache, video_id, found, LYRICS_CACHE_SIZE)
                waiter.done.set()
        elif found is None:
            waiter.done.wait()
            if waiter.error is not None:
                raise BackendError(f"{type(waiter.error).__name__}: {waiter.error}") from waiter.error
            found = waiter.result
        lyrics, source = found
        return {"video_id": video_id, "lyrics": lyrics, "source": source}

    def _playlist_list(self, args):
        local = playlists_local.list_playlists()
        try:
            return self._playlist_list_remote(local)
        except REMOTE_ERRORS as exc:
            # signed out, or YouTube unreachable: still list the local
            # playlists, and say why the rest is missing instead of
            # silently showing fewer rows
            return {"playlists": [_playlist_dict(p) for p in local], "error": str(exc)}

    def _playlist_list_remote(self, local):
        remote = music.library_playlists()
        for playlist in remote:
            # the listing has no count for the auto-playlists; ask per
            # playlist, once -- the answer is remembered for the session so
            # a refresh of the pane is one request, not one per playlist
            if not playlist.track_count:
                count = self._count_of(playlist.playlist_id)
                if count is not None:
                    playlist.track_count = count
        if self._mixes is None:
            # [] is an answer (a signed-out home feed has no mixes); asking
            # again on every listing is not
            self._mixes = music.mixes()
        return {
            "playlists": [_playlist_dict(p) for p in local + remote]
            + [_playlist_dict(m, kind="mix") for m in self._mixes]
        }

    def _count_of(self, playlist_id):
        """How many tracks a remote playlist holds, asked at most once."""
        if playlist_id in self._playlist_counts:
            return self._playlist_counts[playlist_id]
        try:
            count = music.playlist_count(playlist_id)
        except REMOTE_ERRORS:
            return None  # not remembered: a failure is worth retrying
        if count is not None:
            self._playlist_counts[playlist_id] = count
        return count

    def _mixes_refresh(self, args):
        """Forget the cached mixes and their tracklists; the next listing
        and the next play fetch fresh ones."""
        self._mixes = None
        self._mix_tracks = {}
        self._playlist_counts = {}
        return self._playlist_list(args)

    def _playlist_create(self, args):
        """Create a playlist named `title`; remote unless `local` is set."""
        title = (args.get("title") or "").strip()
        if not title:
            raise BackendError("a playlist needs a name")
        if args.get("local"):
            playlist_id = playlists_local.create(title)
        else:
            playlist_id = music.create_playlist(title)
        return {"playlist_id": playlist_id, "title": title, "local": bool(args.get("local"))}

    def _playlist_get(self, args):
        playlist_id = args["playlist_id"]
        if playlists_local.is_local_id(playlist_id):
            playlist, tracks = playlists_local.get_playlist(playlist_id)
            if playlist is None:
                raise BackendError(f"no local playlist {playlist_id}")
        elif music.is_mix_id(playlist_id):
            if playlist_id not in self._mix_tracks:
                self._mix_tracks[playlist_id] = music.get_playlist(playlist_id)
            playlist, tracks = self._mix_tracks[playlist_id]
        else:
            playlist, tracks = music.get_playlist(playlist_id)
            tracks = self._with_pending_adds(playlist_id, tracks)
            if playlist.track_count is not None:
                playlist.track_count = max(playlist.track_count, len(tracks))
        return {"playlist": _playlist_dict(playlist), "tracks": [asdict(t) for t in tracks]}

    def _with_pending_adds(self, playlist_id, tracks):
        """`tracks` plus any recent adds YouTube has not surfaced yet.

        Once a pending track shows up in the fetched list, or it is older
        than PENDING_ADD_TTL, it is forgotten. Liked Music lists newest
        first, so likes go to the front; a plain playlist appends.
        """
        pending = self._pending_adds.get(playlist_id)
        if not pending:
            return tracks
        now = time.monotonic()
        present = {t.video_id for t in tracks}
        still = [(t, when) for t, when in pending
                 if t.video_id not in present and now - when < PENDING_ADD_TTL]
        if still:
            self._pending_adds[playlist_id] = still
        else:
            self._pending_adds.pop(playlist_id, None)
        missing = [t for t, _ in still]
        if not missing:
            return tracks
        return missing[::-1] + tracks if playlist_id == LIKED_MUSIC_ID else tracks + missing

    def _playlist_add(self, args):
        playlist_id = args["playlist_id"]
        video_ids = list(args.get("video_ids") or [])
        if playlists_local.is_local_id(playlist_id):
            meta = {t.get("video_id"): t for t in (args.get("tracks") or [])}
            tracks = [_from_args(meta.get(v) or {"video_id": v}) for v in video_ids]
            playlists_local.add_items(playlist_id, tracks)
        elif playlist_id == LIKED_MUSIC_ID:
            # YouTube rejects playlist inserts into the auto-playlist with an
            # HTTP 400; liking the song is what puts it there.
            for video_id in video_ids:
                music.like(video_id)
        elif playlist_id == EPISODES_ID:
            raise BackendError("Episodes for Later only takes podcast episodes")
        elif music.is_mix_id(playlist_id):
            raise BackendError("Mixes are generated by YouTube and cannot be edited")
        else:
            music.add_playlist_items(playlist_id, video_ids)
        result = {"playlist_id": playlist_id, "added": len(video_ids)}
        if not playlists_local.is_local_id(playlist_id):
            meta = {t.get("video_id"): t for t in (args.get("tracks") or [])}
            now = time.monotonic()
            self._pending_adds.setdefault(playlist_id, []).extend(
                (_from_args(meta.get(v) or {"video_id": v}), now) for v in video_ids
            )
            # the library listing can lag behind an add; hand the UI a fresh
            # count so the row is right without waiting for the next refresh
            self._playlist_counts.pop(playlist_id, None)
            try:
                count = music.playlist_count(playlist_id)
            except REMOTE_ERRORS:
                pass
            else:
                if count is not None:
                    result["track_count"] = count
                    self._playlist_counts[playlist_id] = count
        return result

    def _playlist_play(self, args):
        """Replace the queue with a playlist's tracks and start the first."""
        tracks = self._playlist_get(args)["tracks"]
        if not tracks:
            raise BackendError("that playlist is empty")
        tracks = [_from_args(entry) for entry in tracks]
        state.remember_tracks(tracks)
        self._player.stop()
        self._player.play(cache.playback_url(tracks[0].video_id), title=_label(tracks[0]))
        self._player.enqueue_many(
            (cache.playback_url(track.video_id), _label(track)) for track in tracks[1:]
        )
        return self._queue()

    def _like_set(self, args):
        video_id = args.get("video_id")
        current = None
        if not video_id:
            current = self._current()
            video_id = current.video_id if current else ""
        if not video_id:
            raise BackendError("nothing is playing")
        liked = bool(args.get("liked"))
        if liked:
            music.like(video_id)
        else:
            music.unlike(video_id)
        track = state.track_for(video_id) or current
        if track is not None:
            track.liked = liked
            state.remember_tracks([track])
        return {"video_id": video_id, "liked": liked}

    def _shutdown(self, args):
        self._player.quit()
        return {"stopping": True}

    # -- events -----------------------------------------------------------------

    def on_event(self, callback):
        self._subscribers.append(callback)

    def _emit(self, event, data):
        for callback in list(self._subscribers):
            callback(event, data)

    def listen(self):
        """Block, translating mpv property changes into TUI events.

        Runs on the TUI's listener thread until the connection drops or
        `close()` is called. Uses its own connection so the command socket
        is never shared with a blocking read.
        """
        try:
            observer = self._make_player(spawn=False, timeout=None)
        except PlayerError as exc:
            raise BackendError(str(exc)) from exc
        stop_mixer = self._watch_mixer()
        current_id = None
        duration = 0
        position = None
        last_emitted = None
        paused = False
        volume = None

        def emit_position():
            # Preserve sub-second positions for timed lyrics; deduplicate only
            # identical observations. Seeking must also take effect immediately.
            nonlocal last_emitted
            key = (position or 0, duration, current_id)
            if key == last_emitted:
                return
            last_emitted = key
            self._emit("position", {
                "position": position, "video_id": current_id, "duration_seconds": duration,
            })

        try:
            # duration is observed before time-pos so the initial burst of
            # values arrives in a usable order; a duration that turns up
            # later (mpv learns it after the first time-pos of a new file)
            # re-announces the position so the bar gets its total
            for name, value in observer.observe(
                "playlist-pos", "playlist-count", "pause", "volume", "duration", "time-pos"
            ):
                if self._closed:
                    return
                if name == "duration":
                    # None while the next file loads: keep the duration the
                    # track change already announced instead of zeroing it
                    if value is None:
                        continue
                    duration = value
                    if position is not None:
                        emit_position()
                elif name == "time-pos":
                    if value is not None:
                        position = value
                        emit_position()
                elif name in ("pause", "volume"):
                    # the observed value is the answer already; asking mpv
                    # for a whole status here re-read seven properties and,
                    # with a system mixer, shelled out to wpctl -- on every
                    # press of the play/pause key
                    if name == "pause":
                        paused = bool(value)
                    elif self._player.mixer is not None:
                        # mpv's own volume is pinned to 100 with a mixer, so
                        # it says nothing about what the desktop is playing at
                        volume = self._player.volume()
                    elif value is not None:
                        volume = value
                    if volume is None:
                        volume = self._player.volume()
                    self._emit("state_changed", {"paused": paused, "volume": volume})
                elif name in ("playlist-pos", "playlist-count"):
                    queue = self._queue()
                    self._emit("queue_changed", queue)
                    track = queue["tracks"][queue["index"]] if queue["index"] >= 0 else None
                    new_id = track["video_id"] if track else None
                    if new_id != current_id:
                        current_id = new_id
                        self._emit("track_changed", track)
                        if track:
                            # mpv only reports time-pos/duration once yt-dlp
                            # has resolved the stream, seconds later; snap
                            # the bar to 0:00 of the known length right away
                            position = 0
                            duration = track.get("duration_seconds") or 0
                            emit_position()
        except PlayerError as exc:
            if not self._closed:
                raise BackendError(str(exc)) from exc
        finally:
            stop_mixer.set()
            observer.close()

    def _watch_mixer(self):
        """Follow the system volume while `listen()` runs, so a media key or
        the tray slider shows up in the TUI like a `+`/`-` would. Returns
        the event that stops the watcher thread; a no-op without a mixer."""
        stop = threading.Event()
        mixer = getattr(self._player, "mixer", None)
        if mixer is None:
            return stop

        def changed(level):
            if self._closed or stop.is_set():
                return
            try:
                paused = bool(self._player.get("pause", False))
            except PlayerError:
                paused = False
            self._emit("state_changed", {"paused": paused, "volume": level})

        threading.Thread(
            target=mixer.watch, args=(changed, stop), daemon=True, name="ytm-mixer"
        ).start()
        return stop

    def close(self):
        self._closed = True
        self._player.close()


def _default_player(spawn=True, timeout=None):
    """`timeout=None` really means no timeout: the observing connection
    blocks for as long as mpv is silent, which while paused is forever.
    (It used to fall back to the 5 s default, so the listener died quietly
    after five seconds of pause and the pane froze.)"""
    from ytm import cli

    return cli.player(spawn=spawn, timeout=timeout)
