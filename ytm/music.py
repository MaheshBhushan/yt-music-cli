"""Catalogue and account operations: a thin, normalising layer over ytmusicapi.

Owns nothing YouTube Music already owns. Every function takes an optional
`yt` client so tests inject a fake; results come back as the small Track and
Playlist records the CLI needs and nothing more.
"""
import contextlib
import functools
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import ytmusicapi
from ytmusicapi.exceptions import YTMusicError, YTMusicServerError

from ytm import auth as auth_mod
from ytm.auth import (
    _EXPIRED_HINT,
    _REFRESH_FAILED_HINT,
    _SIGNED_OUT_HINT,
    AuthError,
    AuthExpired,
    client,
    is_expiry,
)
from ytm.timed_lyrics import normalize_timed_lines

#: Search has its own anonymous client, independent of account credentials.
_CATALOGUE_LOCK = threading.Lock()
_CATALOGUE_CLIENT = None


def catalogue_client():
    """The process-wide anonymous search client, built on first use."""
    global _CATALOGUE_CLIENT
    with _CATALOGUE_LOCK:
        if _CATALOGUE_CLIENT is None:
            _CATALOGUE_CLIENT = ytmusicapi.YTMusic()
        return _CATALOGUE_CLIENT


#: One authenticated ytmusicapi client is kept per process for account calls.
#: Building one is not free: it opens a fresh TLS connection to YouTube and,
#: on its first request, downloads the music.youtube.com home page just to
#: read a visitor id out of it. A client per call meant every search, every
#: playlist listing and every per-playlist track count paid both again --
#: listing a library of ten playlists made a dozen handshakes and a dozen
#: home-page downloads for twelve API calls.
_CLIENT_LOCK = threading.RLock()
_CLIENT_READY = threading.Condition(_CLIENT_LOCK)
_CLIENT = {
    "key": None,
    "client": None,
    "visitor_saved": False,
    "building": False,
    "generation": 0,
}

#: Where the visitor id YouTube handed out is kept, so a one-shot CLI run
#: does not have to fetch the home page to learn it again.
VISITOR_PATH = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "ytm" / "visitor.json"

#: how long a stored visitor id is reused before it is fetched again
VISITOR_TTL = 24 * 60 * 60

_VISITOR_HEADER = "X-Goog-Visitor-Id"


def _auth_stamp(path):
    """(path, mtime, size) of the auth file: the cached client's identity.

    Re-running ``ytm auth`` (in this process or another) rewrites the file,
    which changes the stamp and retires the client built from the old one.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return (str(path), None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _read_visitor_id(path=None, now=None):
    """The stored visitor id if it is still fresh, else None."""
    path = Path(path or VISITOR_PATH)
    now = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as file:
            stored = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    written = stored.get("written_at")
    if (
        not isinstance(written, (int, float))
        or written > now
        or now - written > VISITOR_TTL
    ):
        return None
    visitor_id = stored.get("visitor_id")
    return visitor_id if isinstance(visitor_id, str) and visitor_id else None


def _write_visitor_id(visitor_id, path=None, now=None):
    """Remember `visitor_id`; a failure to write is never worth an error."""
    path = Path(path or VISITOR_PATH)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump({"visitor_id": visitor_id, "written_at": time.time() if now is None else now}, file)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _live_visitor_id(yt):
    """The visitor id the client has already worked out, or None.

    ytmusicapi computes its base headers lazily and caches them on the
    instance, so reading them out of the instance dict is free -- while
    `yt.base_headers` would go and fetch the home page if the first request
    had not happened yet, which is the very request being avoided here.
    """
    headers = getattr(yt, "__dict__", {}).get("base_headers")
    try:
        return headers.get(_VISITOR_HEADER) if headers is not None else None
    except AttributeError:
        return None


def _remember_visitor_id():
    """Persist the visitor id the live client learned, if this run has not.

    Written once per process: the id does not change under a live client.
    """
    with _CLIENT_LOCK:
        if _CLIENT["visitor_saved"] or _CLIENT["client"] is None:
            return
        visitor_id = _live_visitor_id(_CLIENT["client"])
        if not visitor_id:
            return
        _CLIENT["visitor_saved"] = True
    if visitor_id != _read_visitor_id():
        _write_visitor_id(visitor_id)


def reset_client():
    """Drop the cached client; the next call builds a fresh one."""
    with _CLIENT_READY:
        _CLIENT.update(key=None, client=None, visitor_saved=False)
        _CLIENT["generation"] += 1
        _CLIENT_READY.notify_all()


def shared_client():
    """The process-wide authenticated client, built on first use.

    Errors are never cached: a missing or expired auth file raises here
    exactly as a per-call client did, and the next call tries again.
    """
    path = auth_mod.AUTH_PATH
    while True:
        key = _auth_stamp(path)
        with _CLIENT_READY:
            if _CLIENT["key"] == key and _CLIENT["client"] is not None:
                return _CLIENT["client"]
            if _CLIENT["building"]:
                _CLIENT_READY.wait()
                continue
            _CLIENT["building"] = True
            generation = _CLIENT["generation"]
        try:
            # OAuth construction may refresh a token over the network. Keep
            # reset_client() and readers of an existing client responsive.
            headers = _seeded_headers(path)
            built = (
                client(path)
                if headers is None
                else auth_mod.client_from_headers(headers, path)
            )
        except BaseException:
            with _CLIENT_READY:
                _CLIENT["building"] = False
                _CLIENT_READY.notify_all()
            raise
        with _CLIENT_READY:
            unchanged = (
                generation == _CLIENT["generation"]
                and key == _auth_stamp(path)
            )
            if unchanged:
                _CLIENT.update(key=key, client=built, visitor_saved=False)
            _CLIENT["building"] = False
            _CLIENT_READY.notify_all()
            if unchanged:
                return built


def _seeded_headers(path):
    """Stored browser headers with a known-good visitor id added, or None.

    Only browser auth can be seeded this way: ytmusicapi builds an OAuth
    client's headers itself, so that path keeps fetching the home page once
    per process.
    """
    visitor_id = _read_visitor_id()
    if not visitor_id:
        return None
    try:
        headers = auth_mod.browser_headers(path)
    except AuthError:
        return None
    if headers is None or _VISITOR_HEADER in headers:
        return None
    return {**headers, _VISITOR_HEADER: visitor_id}


def _refreshing(fn):
    """Retry `fn` once with re-extracted browser cookies when the stored ones
    have gone stale.

    Google rotates the browser's session tokens about daily; the copy in
    auth.json is then answered with the signed-out page (or a 401/403) and
    `fn` raises AuthExpired. If the auth came from a browser, pull the live
    cookies from it again and retry, so the TUI recovers by itself. Only
    applies when `fn` builds its own client: a caller that passes `yt` owns
    that client, and the error is theirs to handle.
    """
    @functools.wraps(fn)
    def wrapper(*args, yt=None, **kwargs):
        if yt is not None:
            return fn(*args, yt=yt, **kwargs)
        try:
            result = fn(*args, **kwargs)
        except AuthExpired as stale:
            source = auth_mod.browser_source(auth_mod.AUTH_PATH)
            if source is None:
                raise
            try:
                auth_mod.refresh_from_browser(auth_mod.AUTH_PATH)
            except AuthError as exc:
                raise AuthExpired(
                    str(stale) + _REFRESH_FAILED_HINT.format(browser=source["browser"], reason=exc)
                ) from exc
            reset_client()  # the refreshed cookies need a client of their own
            result = fn(*args, **kwargs)
        _remember_visitor_id()
        return result

    return wrapper

#: search results whose videoType is an upload are out of scope for this player
UPLOAD_VIDEO_TYPE = "MUSIC_VIDEO_TYPE_PRIVATELY_OWNED_TRACK"


@dataclass
class Track:
    """A single playable song, normalised from a ytmusicapi search result."""

    video_id: str
    title: str
    artist: str
    album: str
    duration: str
    duration_seconds: int
    #: URL of a small cover image, or "" when the result carried none
    thumbnail: str = ""


#: cover art this wide is plenty for a terminal and keeps the fetch small
THUMBNAIL_MIN_WIDTH = 120


def pick_thumbnail(thumbnails):
    """The smallest thumbnail at least THUMBNAIL_MIN_WIDTH wide, else the largest."""
    candidates = [t for t in (thumbnails or []) if isinstance(t, dict) and t.get("url")]
    if not candidates:
        return ""
    candidates.sort(key=lambda t: t.get("width") or 0)
    for thumb in candidates:
        if (thumb.get("width") or 0) >= THUMBNAIL_MIN_WIDTH:
            return thumb["url"]
    return candidates[-1]["url"]


@dataclass
class Playlist:
    """A playlist, either remote (YouTube Music) or local (later subtask)."""

    playlist_id: str
    title: str
    track_count: int
    local: bool = False


def _join_artists(result):
    """Join artist names, tolerating a missing or empty artists list."""
    artists = result.get("artists") or []
    names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
    return ", ".join(names) if names else "Unknown Artist"


def _album_name(result):
    """Album name, tolerating an absent or null album field."""
    album = result.get("album")
    if isinstance(album, dict):
        return album.get("name") or ""
    return album or ""


def _duration(result):
    """Duration as (display string, seconds), tolerating nulls."""
    seconds = result.get("duration_seconds")
    seconds = int(seconds) if isinstance(seconds, (int, float)) else 0
    display = result.get("duration")
    if not display:
        display = f"{seconds // 60}:{seconds % 60:02d}" if seconds else "0:00"
    return display, seconds


def is_upload(result):
    """Whether a search result came from the user's personal uploads."""
    return (
        result.get("videoType") == UPLOAD_VIDEO_TYPE
        or result.get("resultType") == "upload"
        or "entityId" in result
    )


def to_track(result):
    """Normalise one ytmusicapi search/playlist item into a Track."""
    display, seconds = _duration(result)
    return Track(
        video_id=result.get("videoId") or "",
        title=result.get("title") or "Unknown Title",
        artist=_join_artists(result),
        album=_album_name(result),
        duration=display,
        duration_seconds=seconds,
        # search results say "thumbnails", watch-playlist (radio) items "thumbnail"
        thumbnail=pick_thumbnail(result.get("thumbnails") or result.get("thumbnail")),
    )


def to_tracks(results):
    """Normalise search results into Tracks, dropping uploads and unplayable items."""
    return [
        to_track(result)
        for result in results
        if not is_upload(result) and result.get("videoId")
    ]


def to_playlist(result, local=False):
    """Normalise one ytmusicapi playlist item into a Playlist."""
    count = result.get("count") or result.get("trackCount") or 0
    try:
        count = int(str(count).replace(",", ""))
    except ValueError:
        count = 0
    return Playlist(
        playlist_id=result.get("playlistId") or "",
        title=result.get("title") or "Untitled",
        track_count=count,
        local=local,
    )


def search(query, limit=20, yt=None):
    """Search the YouTube Music catalogue for songs and return Tracks.

    Uses filter="songs" so results are Art Tracks (better audio than the
    "videos" filter), and never includes personal uploads. The default
    client is anonymous and needs no stored authentication.
    """
    yt = yt if yt is not None else catalogue_client()
    results = yt.search(query, filter="songs", limit=limit)
    # ytmusicapi treats `limit` as a page-size hint and returns whole pages
    return to_tracks(results)[:limit]


def _timed_lines(line):
    """A provider LyricLine (or an already-plain mapping) as a dict."""
    return line if hasattr(line, "get") else asdict(line)


def _usable_lyrics(result):
    """(lyrics, source) from one lyrics response, or (None, None) when it
    carries nothing a pane could show: a null envelope, an empty or
    all-invalid timed list, or blank plain text."""
    if not result:
        return None, None
    lyrics = result.get("lyrics")
    if result.get("hasTimestamps"):
        lines = normalize_timed_lines([_timed_lines(line) for line in (lyrics or [])])
        return (lines or None), result.get("source")
    if isinstance(lyrics, str) and lyrics.strip():
        return lyrics, result.get("source")
    return None, None


@contextlib.contextmanager
def _without_cookies(yt):
    """Send the requests inside the block without the browser cookie header.

    ytmusicapi keeps the stored browser headers in `base_headers` and adds
    the SAPISID authorization on every request, so dropping the cookie
    leaves the request signed but not tied to the session. Clients without
    that attribute (OAuth, test doubles) are left alone.
    """
    headers = getattr(yt, "base_headers", None)
    if headers is None or "cookie" not in headers:
        yield
        return
    cookie = headers.pop("cookie")
    try:
        yield
    finally:
        headers["cookie"] = cookie


@_refreshing
def get_lyrics(video_id, yt=None, *, timestamps=False):
    """Fetch lyrics for a track, or None if it has none.

    Returns (lyrics_text, source), or (list of timed line dicts, source)
    with timestamps=True. Both are None when the track has no lyrics
    available. Two calls under the hood: the watch playlist gives the lyrics
    browseId, then that id is used to fetch the actual text.

    With timestamps the timed lines are validated (see `ytm.timed_lyrics`);
    when the timed response has no usable content -- None, an empty list,
    nothing but malformed records -- the plain endpoint is asked exactly
    once, and its own source is reported. A plain response to the timed
    request (the track has no timing) is kept as is, without a second call.

    The timed request goes out without the browser's cookies (#46): for
    some accounts YouTube answers the mobile-client request with HTTP 400
    when the session cookies are attached, while the same request without
    them, or signed out entirely, returns the timed lines. Lyrics do not
    depend on the account, so nothing is lost. The watch lookup before it
    and any plain fallback after it keep the full credentials.

    Exception policy: an expired session raises AuthExpired so `_refreshing`
    can retry after a refresh. HTTP 400 from the timed endpoint falls back
    to plain lyrics, since the mobile request may be rejected even when
    plain lyrics work. All other provider errors propagate.
    """
    # Timed requests temporarily switch the client to mobile. Keep that
    # mutation isolated from concurrent searches on the shared client.
    yt = yt if yt is not None else (client() if timestamps else shared_client())
    try:
        watch = yt.get_watch_playlist(videoId=video_id)
        browse_id = (watch or {}).get("lyrics")
        if not browse_id:
            return None, None
        if not timestamps:
            return _usable_lyrics(yt.get_lyrics(browse_id))
        try:
            with _without_cookies(yt):
                result = yt.get_lyrics(browse_id, timestamps=True)
        except YTMusicServerError as exc:
            if "HTTP 400:" not in str(exc):
                raise
            result = None
        lyrics, source = _usable_lyrics(result)
        if lyrics is None:
            lyrics, source = _usable_lyrics(yt.get_lyrics(browse_id))
    except YTMusicError as exc:
        if is_expiry(exc):
            raise AuthExpired(_EXPIRED_HINT) from exc
        raise
    return lyrics, source


#: personal daily mixes (My Supermix, Discover Mix, ...) all share this prefix
MIX_ID_PREFIX = "RDTMAK"


def is_mix_id(playlist_id):
    """Whether `playlist_id` is one of YouTube's personal daily mixes."""
    return (playlist_id or "").startswith(MIX_ID_PREFIX)


@_refreshing
def mixes(yt=None):
    """Personal daily mixes (My Supermix, Discover Mix, ...) from the home feed.

    These are scattered across several home shelves and change daily, so
    they are never cached: read fresh every call, deduped by id and kept in
    feed order. Track count is unknown until the mix itself is fetched.
    """
    yt = yt if yt is not None else shared_client()
    try:
        shelves = yt.get_home(limit=40)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    result = _mixes_from_home(shelves)
    if not result:
        # no mixes at all is what a signed-out home feed looks like; tell
        # them apart from a brand-new account by the library listing
        library_playlists(limit=1, yt=yt)
    return result


def _mixes_from_home(shelves):
    seen = set()
    result = []
    for shelf in shelves or []:
        for item in (shelf or {}).get("contents") or []:
            if not item:
                continue
            playlist_id = item.get("playlistId") or ""
            if not is_mix_id(playlist_id) or playlist_id in seen:
                continue
            seen.add(playlist_id)
            result.append(
                Playlist(
                    playlist_id=playlist_id,
                    title=item.get("title") or "Untitled",
                    track_count=None,
                    local=False,
                )
            )
    return result


@_refreshing
def library_playlists(limit=25, yt=None):
    """Return the user's remote playlists as Playlist objects."""
    yt = yt if yt is not None else shared_client()
    try:
        results = yt.get_library_playlists(limit=limit)
    except YTMusicError as exc:
        if is_expiry(exc):
            raise AuthExpired(_EXPIRED_HINT) from exc
        raise
    if not results:
        # a signed-in library always lists at least Liked Music; YouTube
        # answers stale cookies with the signed-out (empty) page, not an error
        raise AuthExpired(_SIGNED_OUT_HINT)
    return [to_playlist(result) for result in results]


def _signed_out_or_unexpected(exc, playlist_id, yt):
    """Explain a KeyError ytmusicapi raised while navigating a playlist response.

    YouTube answers stale cookies with the signed-out page rather than an
    error. A personal mix does not exist for an anonymous visitor, so that
    page has no 'contents' and ytmusicapi fails a path lookup with a KeyError
    carrying the whole response. The library probe tells a dead session apart
    from a genuinely unexpected response; either way the raw dump never
    reaches the user.
    """
    try:
        signed_in = bool(yt.get_library_playlists(limit=1))
    except Exception:  # the probe itself failing is not the answer we want
        signed_in = True
    if not signed_in:
        return AuthExpired(_SIGNED_OUT_HINT)
    kind = "mix" if is_mix_id(playlist_id) else "playlist"
    # ytmusicapi's message is "Unable to find 'contents' using path [...] on
    # {the entire response}"; keep the field name, drop the dump
    match = re.search(r"Unable to find '([^']+)'", str(exc))
    missing = match.group(1) if match else "a field"
    return RuntimeError(
        f"YouTube Music returned an unexpected response for {kind} {playlist_id} "
        f"(missing '{missing}'). It may have been removed, or ytmusicapi may need an update."
    )


def _wrap_ytmusic_error(exc):
    if is_expiry(exc):
        return AuthExpired(_EXPIRED_HINT)
    return exc


@_refreshing
def get_playlist(playlist_id, limit=100, yt=None):
    """Fetch one remote playlist's details and tracks.

    Returns (Playlist, [Track, ...]). Real responses sometimes omit
    'trackCount' or 'title', hence the same defensive normalisation used
    elsewhere in this module.
    """
    yt = yt if yt is not None else shared_client()
    try:
        result = yt.get_playlist(playlist_id, limit=limit)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    except KeyError as exc:
        raise _signed_out_or_unexpected(exc, playlist_id, yt) from exc
    result = result or {}
    tracks = to_tracks(result.get("tracks") or [])
    playlist = to_playlist(
        {
            "playlistId": result.get("id") or playlist_id,
            "title": result.get("title"),
            "count": result.get("trackCount") or len(tracks),
        }
    )
    return playlist, tracks


@_refreshing
def playlist_count(playlist_id, yt=None):
    """How many tracks a remote playlist holds.

    The library listing omits the count for YouTube's auto-playlists (Liked
    Music, Episodes for Later), so this asks for the playlist itself with a
    one-track page and reads ``trackCount``. Returns None when unknown.
    """
    yt = yt if yt is not None else shared_client()
    try:
        result = yt.get_playlist(playlist_id, limit=1) or {}
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    count = result.get("trackCount")
    if count is None:
        return None
    try:
        return int(str(count).replace(",", ""))
    except ValueError:
        return None


@_refreshing
def create_playlist(title, description="", privacy="PRIVATE", yt=None):
    """Create a remote playlist and return its playlist id."""
    yt = yt if yt is not None else shared_client()
    try:
        result = yt.create_playlist(title, description or "", privacy_status=privacy)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    # ytmusicapi has returned either a bare id string or {"playlistId": ...}
    # across versions -- tolerate both.
    if isinstance(result, dict):
        return result.get("playlistId") or ""
    return result or ""


@_refreshing
def add_playlist_items(playlist_id, video_ids, yt=None):
    """Add tracks (by video id) to a remote playlist."""
    yt = yt if yt is not None else shared_client()
    try:
        return yt.add_playlist_items(playlist_id, list(video_ids))
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc


@_refreshing
def remove_playlist_items(playlist_id, video_ids, yt=None):
    """Remove tracks (by video id) from a remote playlist.

    ytmusicapi's remove call wants the actual playlist-item dicts (it needs
    each track's setVideoId), so the current contents are fetched first and
    filtered down to the requested video ids.
    """
    yt = yt if yt is not None else shared_client()
    try:
        current = yt.get_playlist(playlist_id, limit=None)
        items = [
            item
            for item in (current or {}).get("tracks") or []
            if item.get("videoId") in set(video_ids)
        ]
        if not items:
            return {"removed": 0}
        return yt.remove_playlist_items(playlist_id, items)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc


@_refreshing
def edit_playlist(playlist_id, title=None, description=None, privacy=None, yt=None):
    """Edit a remote playlist's metadata."""
    yt = yt if yt is not None else shared_client()
    kwargs = {}
    if title is not None:
        kwargs["title"] = title
    if description is not None:
        kwargs["description"] = description
    if privacy is not None:
        kwargs["privacyStatus"] = privacy
    try:
        return yt.edit_playlist(playlist_id, **kwargs)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc


@_refreshing
def delete_playlist(playlist_id, yt=None):
    """Delete a remote playlist. Irreversible -- callers must confirm first."""
    yt = yt if yt is not None else shared_client()
    try:
        return yt.delete_playlist(playlist_id)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc


# -- additions for the simplified core -------------------------------------


def watch_url(video_id):
    """The YouTube Music URL mpv is handed for `video_id`; mpv resolves it."""
    return f"https://music.youtube.com/watch?v={video_id}"


@_refreshing
def song(video_id, yt=None):
    """Metadata for one track by id, as a Track; None if YouTube has nothing."""
    yt = yt if yt is not None else shared_client()
    try:
        result = yt.get_song(video_id)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    details = (result or {}).get("videoDetails") or {}
    if not details.get("videoId"):
        return None
    seconds = int(details.get("lengthSeconds") or 0)
    return Track(
        video_id=details["videoId"],
        title=details.get("title") or "Unknown Title",
        artist=details.get("author") or "Unknown Artist",
        album="",
        duration=f"{seconds // 60}:{seconds % 60:02d}" if seconds else "0:00",
        duration_seconds=seconds,
        thumbnail=pick_thumbnail((details.get("thumbnail") or {}).get("thumbnails")),
    )


@_refreshing
def radio(video_id, limit=25, yt=None):
    """Tracks YouTube Music would play after `video_id`, seed excluded."""
    yt = yt if yt is not None else shared_client()
    try:
        watch = yt.get_watch_playlist(videoId=video_id, radio=True, limit=limit)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    return [
        track
        for track in to_tracks((watch or {}).get("tracks") or [])
        if track.video_id != video_id
    ][:limit]


@_refreshing
def like(video_id, yt=None):
    """Mark `video_id` as liked in the user's account."""
    yt = yt if yt is not None else shared_client()
    try:
        return yt.rate_song(video_id, "LIKE")
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc


def track_to_dict(track):
    return asdict(track)


def track_from_dict(data):
    """A Track from a stored dict, tolerating missing keys."""
    data = data or {}
    video_id = data.get("video_id") or ""
    return Track(
        video_id=video_id,
        title=data.get("title") or video_id,
        artist=data.get("artist") or "",
        album=data.get("album") or "",
        duration=data.get("duration") or "",
        duration_seconds=int(data.get("duration_seconds") or 0),
        thumbnail=data.get("thumbnail") or "",
    )
