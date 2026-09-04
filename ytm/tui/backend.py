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
from dataclasses import asdict

from ytm import music, playlists_local, state
from ytm.music import Track
from ytm.player import Player, PlayerError, watch_url


#: YouTube's auto-playlists: ids are fixed and they cannot take plain inserts
LIKED_MUSIC_ID = "LM"
EPISODES_ID = "SE"


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
    )


def _label(track):
    return f"{track.title} / {track.artist}" if track.artist else track.title


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
        self._lock = threading.Lock()
        self._subscribers = []
        self._closed = False
        # mixes are re-rolled by YouTube on every fetch, so a session keeps
        # the list and each tracklist until `mixes_refresh` asks for new ones
        self._mixes = None
        self._mix_tracks = {}
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
            "shutdown": self._shutdown,
        }

    # -- requests -------------------------------------------------------------

    def request(self, cmd, args=None):
        handler = self._routes.get(cmd)
        if handler is None:
            raise BackendError(f"unknown command: {cmd}")
        try:
            with self._lock:
                return handler(args or {})
        except PlayerError as exc:
            raise BackendError(str(exc)) from exc
        except Exception as exc:  # auth or network failure
            raise BackendError(f"{type(exc).__name__}: {exc}") from exc

    def _current(self):
        for entry in self._player.playlist():
            if entry["current"]:
                known = state.track_for(entry["video_id"]) if entry["video_id"] else None
                return known or Track(entry["video_id"] or "", entry["title"] or entry["url"], "", "", "", 0)
        return None

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
        tracks = music.search(args.get("query") or "", limit=int(args.get("limit") or 20))
        state.remember_search(tracks)
        return {"tracks": [asdict(t) for t in tracks]}

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
        method(watch_url(track.video_id), title=_label(track))
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
        entries = self._player.playlist()
        tracks, index = [], -1
        for i, entry in enumerate(entries):
            known = state.track_for(entry["video_id"]) if entry["video_id"] else None
            tracks.append(asdict(known) if known else asdict(
                Track(entry["video_id"] or "", entry["title"] or entry["url"], "", "", "", 0)
            ))
            if entry["current"]:
                index = i
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
        self._player.play(watch_url(seed.video_id), title=_label(seed))
        for track in tracks:
            self._player.enqueue(watch_url(track.video_id), title=_label(track))
        return self._queue()

    def _lyrics(self, args):
        video_id = args.get("video_id")
        lyrics, source = music.get_lyrics(video_id)
        return {"video_id": video_id, "lyrics": lyrics, "source": source}

    def _playlist_list(self, args):
        local = playlists_local.list_playlists()
        remote = music.library_playlists()
        for playlist in remote:
            # the listing has no count for the auto-playlists; ask per playlist
            if not playlist.track_count:
                try:
                    count = music.playlist_count(playlist.playlist_id)
                except Exception:
                    count = None
                if count is not None:
                    playlist.track_count = count
        if self._mixes is None:
            self._mixes = music.mixes()
        return {
            "playlists": [_playlist_dict(p) for p in local + remote]
            + [_playlist_dict(m, kind="mix") for m in self._mixes]
        }

    def _mixes_refresh(self, args):
        """Forget the cached mixes and their tracklists; the next listing
        and the next play fetch fresh ones."""
        self._mixes = None
        self._mix_tracks = {}
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
        return {"playlist": _playlist_dict(playlist), "tracks": [asdict(t) for t in tracks]}

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
            # the library listing can lag behind an add; hand the UI a fresh
            # count so the row is right without waiting for the next refresh
            try:
                result["track_count"] = music.playlist_count(playlist_id)
            except Exception:
                pass
        return result

    def _playlist_play(self, args):
        """Replace the queue with a playlist's tracks and start the first."""
        tracks = self._playlist_get(args)["tracks"]
        if not tracks:
            raise BackendError("that playlist is empty")
        tracks = [_from_args(entry) for entry in tracks]
        state.remember_tracks(tracks)
        self._player.stop()
        self._player.play(watch_url(tracks[0].video_id), title=_label(tracks[0]))
        for track in tracks[1:]:
            self._player.enqueue(watch_url(track.video_id), title=_label(track))
        return self._queue()

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
        current_id = None
        duration = 0
        position = None

        def emit_position():
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
                    duration = value or 0
                    if position is not None:
                        emit_position()
                elif name == "time-pos":
                    if value is not None:
                        position = value
                        emit_position()
                elif name in ("pause", "volume"):
                    with self._lock:
                        s = self._player.status()
                    self._emit("state_changed", {"paused": s["paused"], "volume": s["volume"]})
                elif name in ("playlist-pos", "playlist-count"):
                    with self._lock:
                        queue = self._queue()
                    self._emit("queue_changed", queue)
                    track = queue["tracks"][queue["index"]] if queue["index"] >= 0 else None
                    new_id = track["video_id"] if track else None
                    if new_id != current_id:
                        current_id = new_id
                        if track:
                            self._emit("track_changed", track)
        except PlayerError as exc:
            if not self._closed:
                raise BackendError(str(exc)) from exc
        finally:
            observer.close()

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
