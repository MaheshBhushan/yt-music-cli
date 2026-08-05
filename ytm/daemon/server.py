"""Daemon server entry point.

A newline-delimited JSON protocol over a Unix domain socket at
``$XDG_RUNTIME_DIR/ytm/ytmd.sock``.

Request::

    {"id": 1, "cmd": "play", "args": {"video_id": "dQw4w9WgXcQ"}}

Response::

    {"id": 1, "ok": true, "data": {...}}
    {"id": 1, "ok": false, "error": "..."}

Events are pushed unsolicited to every connected client so clients never
have to poll::

    {"event": "track_changed", "data": {"video_id": "...", "title": "..."}}

The socket is a trust boundary: malformed JSON, an unknown command, a
missing or wrongly typed argument and a client vanishing mid-request are all
answered (where an answer is still possible) with ``ok: false`` and never
take down the connection loop or the daemon.
"""

import asyncio
import contextlib
import errno
import json
import os
import signal
import socket
import sys
import tempfile
from pathlib import Path

from ytm import api, auth, cache, config as config_mod, playlists_local, pot
from ytm.daemon import mpris
from ytm.daemon import state as state_mod
from ytm.daemon.player import Player
from ytm.daemon.queue import Queue

SOCKET_NAME = "ytmd.sock"

#: minimum seconds between two pushed `position` events
POSITION_INTERVAL = 1.0

#: max distinct video_ids kept in the in-memory lyrics cache
LYRICS_CACHE_LIMIT = 100


def socket_path():
    """The daemon socket path, falling back when XDG_RUNTIME_DIR is unset."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = os.path.join(tempfile.gettempdir(), f"ytm-{os.getuid()}")
    return Path(runtime_dir) / "ytm" / SOCKET_NAME


class ProtocolError(Exception):
    """A request the daemon can describe but not carry out."""


# -- argument validation ---------------------------------------------------
#
# Hand-rolled on purpose: the surface is a handful of scalars, which is not
# worth a schema library.


def _args(request):
    """The request's args mapping, rejecting a non-object."""
    args = request.get("args")
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ProtocolError("'args' must be an object")
    return args


def _req_str(args, name):
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"'{name}' must be a non-empty string")
    return value


def _opt_str(args, name, default=None):
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ProtocolError(f"'{name}' must be a string")
    return value


def _req_number(args, name):
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"'{name}' must be a number")
    return value


def _req_int(args, name):
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"'{name}' must be an integer")
    return value


def _opt_int(args, name, default=None):
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"'{name}' must be an integer")
    return value


def _opt_bool(args, name, default=False):
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProtocolError(f"'{name}' must be a boolean")
    return value


def _req_str_list(args, name):
    value = args.get(name)
    if not isinstance(value, list) or not value or not all(
        isinstance(v, str) and v for v in value
    ):
        raise ProtocolError(f"'{name}' must be a non-empty list of strings")
    return value


class _PlayerTap:
    """Wraps a Player so the daemon sees the callbacks the Queue registers.

    The Queue registers its own position/eof/error observers on the player;
    this proxy keeps them and chains the daemon's own handler after them, so
    the daemon can push events without the Queue or the Player knowing about
    it. Running after the Queue matters: the Queue's error handler skips to
    the next track, and the daemon must report the track it skipped *to*.
    """

    def __init__(self, player):
        self._player = player
        self._queue_position = None
        self._queue_eof = None
        self._queue_error = None
        self._tap_position = None
        self._tap_eof = None
        self._tap_error = None
        player.on_position(self._position)
        player.on_eof(self._eof)
        player.on_error(self._error)

    def __getattr__(self, name):
        return getattr(self._player, name)

    def on_position(self, callback):
        self._queue_position = callback

    def on_eof(self, callback):
        self._queue_eof = callback

    def on_error(self, callback):
        self._queue_error = callback

    def tap(self, on_position=None, on_eof=None, on_error=None):
        """Register the daemon's own observers, run after the Queue's."""
        self._tap_position = on_position
        self._tap_eof = on_eof
        self._tap_error = on_error

    def _position(self, value):
        for callback in (self._queue_position, self._tap_position):
            if callback is not None:
                callback(value)

    def _eof(self):
        for callback in (self._queue_eof, self._tap_eof):
            if callback is not None:
                callback()

    def _error(self, message=None):
        for callback in (self._queue_error, self._tap_error):
            if callback is not None:
                callback(message)


class Daemon:
    """Routes JSON commands to the player/queue and pushes events out."""

    def __init__(
        self,
        path=None,
        player=None,
        queue=None,
        state_path=None,
        yt=None,
        playlists_path=None,
        config=None,
    ):
        self._config = config if config is not None else config_mod.load()
        self._path = Path(path) if path is not None else socket_path()
        self._state_path = (
            Path(state_path) if state_path is not None else state_mod.STATE_PATH
        )
        self._playlists_path = (
            Path(playlists_path)
            if playlists_path is not None
            else playlists_local.DEFAULT_PATH
        )
        self._yt = yt
        self._own_player = player is None
        device = self._config["audio"]["device"]
        self._player = _PlayerTap(
            player
            if player is not None
            else Player(audio_device=None if device == "auto" else device)
        )
        self._queue = (
            queue
            if queue is not None
            else Queue(
                self._player,
                yt=yt,
                resolver=cache.cache_aware_resolver(),
                autoplay_radio=self._config["behaviour"]["autoplay_radio"],
            )
        )
        self._player.tap(
            on_position=self._pushed_position,
            on_eof=self._pushed_eof,
            on_error=self._pushed_skip,
        )
        self._queue.on_error(self._pushed_error)

        self._clients = set()
        self._server = None
        self._loop = None
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._shutdown_requested = False
        self._volume = self._config["audio"]["volume"]
        self._last_played = None
        self._last_position = 0.0
        self._last_position_push = 0.0
        self._mpris = None
        #: in-memory only, per video_id -- (lyrics, source), bounded by
        #: LYRICS_CACHE_LIMIT so repeated switching doesn't refetch
        self._lyrics_cache = {}

        #: routing table -- later subtasks add `playlist_*` entries here only
        self._routes = {
            "search": self._cmd_search,
            "play": self._cmd_play,
            "enqueue": self._cmd_enqueue,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "toggle": self._cmd_toggle,
            "next": self._cmd_next,
            "prev": self._cmd_prev,
            "seek": self._cmd_seek,
            "volume": self._cmd_volume,
            "status": self._cmd_status,
            "queue_get": self._cmd_queue_get,
            "queue_clear": self._cmd_queue_clear,
            "queue_move": self._cmd_queue_move,
            "queue_remove": self._cmd_queue_remove,
            "radio": self._cmd_radio,
            "lyrics": self._cmd_lyrics,
            "shutdown": self._cmd_shutdown,
            "playlist_list": self._cmd_playlist_list,
            "playlist_get": self._cmd_playlist_get,
            "playlist_create": self._cmd_playlist_create,
            "playlist_add": self._cmd_playlist_add,
            "playlist_remove": self._cmd_playlist_remove,
            "playlist_delete": self._cmd_playlist_delete,
        }

    # -- state -------------------------------------------------------------

    def restore(self):
        """Load persisted state into the queue without starting playback.

        Volume precedence: a persisted state file (more recent user intent)
        wins over the config default; the config default only applies when
        no state file exists yet (e.g. first run).
        """
        loaded = state_mod.load(self._state_path)
        if Path(self._state_path).exists():
            self._volume = loaded["volume"]
        self._last_played = loaded["last_played"]
        if loaded["tracks"]:
            # Set the queue's cursor and tracks directly: enqueue() would
            # start playback and resolve a stream URL, which restoring must
            # not do.
            self._queue._tracks = list(loaded["tracks"])
            self._queue._index = loaded["index"]
        with contextlib.suppress(Exception):
            self._player.set_volume(self._volume)

    def save(self):
        """Persist the queue, cursor, volume and last-played track."""
        snapshot = {
            "tracks": self._queue.tracks,
            "index": self._queue.index,
            "volume": self._volume,
            "last_played": self._last_played,
        }
        with contextlib.suppress(OSError):
            state_mod.save(snapshot, self._state_path)

    # -- events ------------------------------------------------------------

    def _emit(self, event, data=None):
        """Queue an event for delivery to every connected client.

        Safe to call from the player's IPC reader thread as well as from the
        event loop.
        """
        if self._mpris is not None:
            with contextlib.suppress(Exception):
                self._mpris.on_daemon_event(event, data or {})
        payload = json.dumps({"event": event, "data": data or {}}) + "\n"
        raw = payload.encode("utf-8")
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._broadcast, raw)

    def _broadcast(self, raw):
        for writer in list(self._clients):
            try:
                writer.write(raw)
            except (OSError, RuntimeError):
                self._clients.discard(writer)

    def _emit_track_changed(self):
        track = self._queue.current
        if track is None:
            return
        self._last_played = track.video_id
        self._emit(
            "track_changed", {"video_id": track.video_id, "title": track.title}
        )

    def _emit_queue_changed(self):
        self._emit("queue_changed", self._queue_data())

    def _emit_if_track_moved(self):
        """Announce a new current track, but only if the cursor really moved.

        For paths that may or may not move it: an error the circuit breaker
        swallows, an error at the tail with autoplay off, a removal that
        misses the current track. Announcing unconditionally would claim a
        track is playing when nothing was loaded, and `_emit_track_changed`
        also records `_last_played`, which `save()` persists.

        Returns whether it announced anything, so callers can decide what
        else to push alongside it.
        """
        track = self._queue.current
        if track is None or track.video_id == self._last_played:
            return False
        self._emit_track_changed()
        self._emit_state_changed()
        return True

    def _emit_state_changed(self):
        self._emit("state_changed", {"paused": self._player.paused, "volume": self._volume})

    def _pushed_position(self, value):
        self._last_position = value
        now = value if isinstance(value, (int, float)) else 0.0
        if abs(now - self._last_position_push) < POSITION_INTERVAL:
            return
        self._last_position_push = now
        track = self._queue.current
        self._emit(
            "position",
            {
                "position": value,
                "video_id": track.video_id if track else None,
                "duration_seconds": track.duration_seconds if track else 0,
            },
        )

    def _pushed_eof(self):
        self._emit_track_changed()
        self._emit_queue_changed()

    def _pushed_error(self, message):
        # Registered on the Queue, which calls it *before* skipping, so this
        # describes the track that failed. The skip itself is reported by
        # `_pushed_skip`.
        self._emit("error", {"error": message, "kind": "playback"})

    def _pushed_skip(self, message=None):
        """Report the track the Queue skipped to after a playback failure.

        Chained after the Queue's own error handler, so the cursor has
        already moved. Without this a failed track leaves clients showing
        the track that died while a different one plays.

        `message` is unused -- it is part of the player's error-observer
        signature, and what failed is already reported by `_pushed_error`.
        """
        if self._emit_if_track_moved():
            self._emit_queue_changed()

    # -- helpers -----------------------------------------------------------

    def _client(self):
        """The ytmusicapi client, resolved lazily so tests can inject one."""
        if self._yt is None:
            self._yt = auth.client()
        return self._yt

    def _track_data(self, track):
        return None if track is None else state_mod.track_to_dict(track)

    def _queue_data(self):
        return {
            "tracks": [state_mod.track_to_dict(t) for t in self._queue.tracks],
            "index": self._queue.index,
        }

    def _status_data(self):
        return {
            "current": self._track_data(self._queue.current),
            "index": self._queue.index,
            "count": len(self._queue.tracks),
            "paused": self._player.paused,
            "volume": self._volume,
            "position": self._last_position,
            "last_played": self._last_played,
        }

    # -- commands ----------------------------------------------------------

    def _cmd_search(self, args):
        query = _req_str(args, "query")
        filt = _opt_str(args, "filter", "songs")
        if filt != "songs":
            raise ProtocolError("only the 'songs' filter is supported")
        tracks = api.search(query, yt=self._client())
        return {"tracks": [state_mod.track_to_dict(t) for t in tracks]}

    def _track_from_args(self, args):
        """A Track built from the caller's video_id plus optional metadata."""
        video_id = _req_str(args, "video_id")
        return state_mod.track_from_dict(
            {
                "video_id": video_id,
                "title": _opt_str(args, "title") or video_id,
                "artist": _opt_str(args, "artist"),
                "album": _opt_str(args, "album"),
                "duration": _opt_str(args, "duration"),
                "duration_seconds": _opt_int(args, "duration_seconds", 0),
            }
        )

    def _cmd_play(self, args):
        track = self._track_from_args(args)
        existing = None
        for i, queued in enumerate(self._queue.tracks):
            if queued.video_id == track.video_id:
                existing = i
                break
        if existing is not None:
            self._queue.play_at(existing)
        elif not self._queue.tracks:
            self._queue.enqueue(track)
        else:
            self._queue.enqueue(track, position=self._queue.index + 1)
            self._queue.next()
        self._emit_track_changed()
        self._emit_queue_changed()
        self._emit_state_changed()
        return self._status_data()

    def _cmd_enqueue(self, args):
        track = self._track_from_args(args)
        position = _opt_int(args, "position")
        started = not self._queue.tracks
        self._queue.enqueue(track, position=position)
        self._emit_queue_changed()
        if started:
            self._emit_track_changed()
            self._emit_state_changed()
        return self._queue_data()

    def _cmd_pause(self, args):
        self._player.pause()
        self._emit_state_changed()
        return self._status_data()

    def _cmd_resume(self, args):
        self._player.resume()
        self._emit_state_changed()
        return self._status_data()

    def _cmd_toggle(self, args):
        self._player.toggle()
        self._emit_state_changed()
        return self._status_data()

    def _cmd_next(self, args):
        self._queue.next()
        self._emit_track_changed()
        self._emit_queue_changed()
        self._emit_state_changed()
        return self._status_data()

    def _cmd_prev(self, args):
        self._queue.prev()
        self._emit_track_changed()
        self._emit_state_changed()
        return self._status_data()

    def _cmd_seek(self, args):
        seconds = _req_number(args, "seconds")
        absolute = _opt_bool(args, "absolute")
        self._player.seek(seconds, absolute=absolute)
        return {"seconds": seconds, "absolute": absolute}

    def _cmd_volume(self, args):
        level = _req_number(args, "level")
        level = max(0, min(100, level))
        self._player.set_volume(level)
        self._volume = level
        self._emit_state_changed()
        return {"volume": self._volume}

    def _cmd_status(self, args):
        return self._status_data()

    def _cmd_queue_get(self, args):
        return self._queue_data()

    def _cmd_queue_clear(self, args):
        self._queue.clear()
        self._emit_queue_changed()
        return self._queue_data()

    def _cmd_queue_move(self, args):
        from_index = _req_int(args, "from_index")
        to_index = _req_int(args, "to_index")
        try:
            self._queue.move(from_index, to_index)
        except IndexError:
            raise ProtocolError(f"no track at index {from_index}") from None
        self._emit_queue_changed()
        return self._queue_data()

    def _cmd_queue_remove(self, args):
        index = _req_int(args, "index")
        try:
            self._queue.remove(index)
        except IndexError:
            raise ProtocolError(f"no track at index {index}") from None
        self._emit_queue_changed()
        # Removing the playing track makes the next one current and starts
        # it, which also clears any pause -- the same silent state change
        # `next`/`prev` had. Removing any other track moves nothing.
        self._emit_if_track_moved()
        return self._queue_data()

    def _cmd_radio(self, args):
        video_id = _req_str(args, "video_id")
        watch = self._client().get_watch_playlist(videoId=video_id)
        tracks = api.to_tracks((watch or {}).get("tracks") or [])
        if not tracks:
            raise ProtocolError(f"no radio available for {video_id}")
        self._queue.clear()
        self._queue.enqueue(tracks)
        self._emit_track_changed()
        self._emit_queue_changed()
        self._emit_state_changed()
        return self._queue_data()

    def _cmd_lyrics(self, args):
        video_id = _req_str(args, "video_id")
        if video_id in self._lyrics_cache:
            lyrics, source = self._lyrics_cache[video_id]
        else:
            lyrics, source = api.get_lyrics(video_id, yt=self._client())
            if len(self._lyrics_cache) >= LYRICS_CACHE_LIMIT:
                self._lyrics_cache.pop(next(iter(self._lyrics_cache)))
            self._lyrics_cache[video_id] = (lyrics, source)
        return {"video_id": video_id, "lyrics": lyrics, "source": source}

    def _cmd_shutdown(self, args):
        # the stop is triggered only once the response has been flushed, so
        # the client always sees its ok before the socket goes away
        self._shutdown_requested = True
        return {"stopping": True}

    # -- playlists -----------------------------------------------------
    #
    # A playlist id starting with "local-" lives entirely on disk (see
    # ytm.playlists_local); anything else is a remote YouTube Music
    # playlist id and is dispatched to ytm.api instead. Destructive remote
    # operations (delete, bulk remove) require the caller to pass
    # confirm=true -- they write to the user's real account and cannot be
    # undone. Local ops never need confirmation.

    def _playlist_to_dict(self, playlist):
        return {
            "playlist_id": playlist.playlist_id,
            "title": playlist.title,
            "track_count": playlist.track_count,
            "local": playlist.local,
        }

    def _cmd_playlist_list(self, args):
        local = playlists_local.list_playlists(path=self._playlists_path)
        remote = api.library_playlists(yt=self._client())
        playlists = local + remote
        return {"playlists": [self._playlist_to_dict(p) for p in playlists]}

    def _cmd_playlist_get(self, args):
        playlist_id = _req_str(args, "playlist_id")
        if playlists_local.is_local_id(playlist_id):
            playlist, tracks = playlists_local.get_playlist(
                playlist_id, path=self._playlists_path
            )
            if playlist is None:
                raise ProtocolError(f"no local playlist {playlist_id}")
        else:
            playlist, tracks = api.get_playlist(playlist_id, yt=self._client())
        return {
            "playlist": self._playlist_to_dict(playlist),
            "tracks": [state_mod.track_to_dict(t) for t in tracks],
        }

    def _cmd_playlist_create(self, args):
        name = _req_str(args, "name")
        description = _opt_str(args, "description", "")
        privacy = _opt_str(args, "privacy", "PRIVATE")
        local = _opt_bool(args, "local", False)
        if local:
            playlist_id = playlists_local.create(
                name, description=description, privacy=privacy,
                path=self._playlists_path,
            )
        else:
            playlist_id = api.create_playlist(
                name, description=description, privacy=privacy, yt=self._client()
            )
        self._emit("playlists_changed", {})
        return {"playlist_id": playlist_id}

    def _cmd_playlist_add(self, args):
        playlist_id = _req_str(args, "playlist_id")
        video_ids = _req_str_list(args, "video_ids")
        if playlists_local.is_local_id(playlist_id):
            tracks_meta = {t.get("video_id"): t for t in (args.get("tracks") or [])}
            tracks = [
                state_mod.track_from_dict(
                    tracks_meta.get(video_id) or {"video_id": video_id}
                )
                for video_id in video_ids
            ]
            try:
                playlists_local.add_items(playlist_id, tracks, path=self._playlists_path)
            except KeyError:
                raise ProtocolError(f"no local playlist {playlist_id}") from None
        else:
            api.add_playlist_items(playlist_id, video_ids, yt=self._client())
        self._emit("playlists_changed", {})
        return {"playlist_id": playlist_id, "added": len(video_ids)}

    def _cmd_playlist_remove(self, args):
        playlist_id = _req_str(args, "playlist_id")
        video_ids = _req_str_list(args, "video_ids")
        if playlists_local.is_local_id(playlist_id):
            try:
                removed = playlists_local.remove_items(
                    playlist_id, video_ids, path=self._playlists_path
                )
            except KeyError:
                raise ProtocolError(f"no local playlist {playlist_id}") from None
        else:
            if self._config["behaviour"]["confirm_remote_delete"] and not _opt_bool(
                args, "confirm", False
            ):
                raise ProtocolError(
                    "removing tracks from a remote playlist requires confirm=true"
                )
            api.remove_playlist_items(playlist_id, video_ids, yt=self._client())
            removed = len(video_ids)
        self._emit("playlists_changed", {})
        return {"playlist_id": playlist_id, "removed": removed}

    def _cmd_playlist_delete(self, args):
        playlist_id = _req_str(args, "playlist_id")
        if playlists_local.is_local_id(playlist_id):
            existed = playlists_local.delete(playlist_id, path=self._playlists_path)
            if not existed:
                raise ProtocolError(f"no local playlist {playlist_id}")
        else:
            if self._config["behaviour"]["confirm_remote_delete"] and not _opt_bool(
                args, "confirm", False
            ):
                raise ProtocolError(
                    "deleting a remote playlist requires confirm=true"
                )
            api.delete_playlist(playlist_id, yt=self._client())
        self._emit("playlists_changed", {})
        return {"playlist_id": playlist_id, "deleted": True}

    # -- request handling --------------------------------------------------

    async def handle_request(self, request):
        """Run one parsed request and return its response object."""
        request_id = request.get("id") if isinstance(request, dict) else None
        if not isinstance(request, dict):
            return {"id": None, "ok": False, "error": "request must be a JSON object"}
        cmd = request.get("cmd")
        if not isinstance(cmd, str):
            return {"id": request_id, "ok": False, "error": "'cmd' must be a string"}
        handler = self._routes.get(cmd)
        if handler is None:
            return {"id": request_id, "ok": False, "error": f"unknown cmd: {cmd}"}
        try:
            args = _args(request)
        except ProtocolError as exc:
            return {"id": request_id, "ok": False, "error": str(exc)}
        try:
            async with self._lock:
                # Handlers block on yt-dlp/ytmusicapi, so they run off the
                # event loop; the lock keeps queue mutation serialised.
                data = await asyncio.to_thread(handler, args)
        except ProtocolError as exc:
            return {"id": request_id, "ok": False, "error": str(exc)}
        except auth.AuthError as exc:
            self._emit("error", {"error": str(exc), "kind": "auth"})
            return {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "kind": "auth",
            }
        except Exception as exc:  # never let one bad command kill the daemon
            message = f"{type(exc).__name__}: {exc}"
            self._emit("error", {"error": message, "cmd": cmd})
            return {"id": request_id, "ok": False, "error": message}
        return {"id": request_id, "ok": True, "data": data}

    async def _serve_client(self, reader, writer):
        self._clients.add(writer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionError, OSError):
                    return
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    response = {"id": None, "ok": False, "error": f"malformed JSON: {exc}"}
                else:
                    response = await self.handle_request(request)
                try:
                    writer.write((json.dumps(response) + "\n").encode("utf-8"))
                    await writer.drain()
                except (ConnectionError, OSError):
                    # client vanished mid-request: drop it, keep serving
                    return
                if self._shutdown_requested:
                    self._stopped.set()
                    return
        finally:
            self._clients.discard(writer)
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()

    # -- lifecycle ---------------------------------------------------------

    async def start(self):
        """Bind the socket and begin serving."""
        self._loop = asyncio.get_running_loop()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        claim_socket(self._path)
        self._server = await asyncio.start_unix_server(
            self._serve_client, path=str(self._path)
        )
        self._mpris = await mpris.register(self)
        return self._server

    async def serve_forever(self):
        """Serve until a `shutdown` command arrives."""
        await self._stopped.wait()

    async def stop(self):
        """Stop the player, save state, close the socket and clean up."""
        if self._mpris is not None:
            with contextlib.suppress(Exception):
                await self._mpris.close()
            self._mpris = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for writer in list(self._clients):
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
        self._clients.clear()
        with contextlib.suppress(Exception):
            self._player.pause()
        if self._own_player:
            with contextlib.suppress(Exception):
                self._player.close()
        self.save()
        with contextlib.suppress(OSError):
            os.unlink(self._path)


def is_socket_alive(path):
    """Whether something is actually accepting connections on `path`."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(str(path))
    except OSError as exc:
        # ECONNREFUSED/ENOENT mean the file (if any) is a stale leftover
        if exc.errno in (errno.ECONNREFUSED, errno.ENOENT):
            return False
        return True
    finally:
        sock.close()
    return True


def claim_socket(path):
    """Ensure `path` is free to bind, removing a stale socket file.

    Raises RuntimeError if a live daemon already owns the socket, so a second
    ytmd refuses to start instead of fighting over it.
    """
    path = Path(path)
    if not path.exists():
        return path
    if is_socket_alive(path):
        raise RuntimeError(f"a ytm daemon is already running at {path}")
    os.unlink(path)
    return path


async def run(path=None):
    """Start the daemon, restore state and serve until shutdown."""
    path = Path(path) if path is not None else socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # claimed before mpv is spawned, so a refused start costs nothing
        claim_socket(path)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    # the PO token provider is what makes authenticated resolution work; a
    # daemon that cannot reach it still starts, and resolution falls back to
    # trying without cookies, so this is advisory only
    if not await asyncio.to_thread(pot.ensure_provider):
        print(
            "ytm: PO token provider unavailable; "
            "falling back to unauthenticated stream resolution",
            file=sys.stderr,
        )
    daemon = Daemon(path=path)
    await daemon.start()
    daemon.restore()
    loop = asyncio.get_running_loop()
    # SIGTERM/SIGINT must run the same teardown `shutdown` already uses
    # (player closed, mpv reaped, state saved, socket removed) instead of
    # the OS default disposition, which would kill the process outright and
    # orphan the mpv subprocess. Setting the same event `serve_forever`
    # waits on is idempotent, so a second signal -- or one arriving during
    # an in-flight `shutdown` command -- is harmless.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon._stopped.set)
    try:
        await daemon.serve_forever()
    finally:
        await daemon.stop()
    return 0


def main():
    """Start the daemon server."""
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except auth.AuthError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
