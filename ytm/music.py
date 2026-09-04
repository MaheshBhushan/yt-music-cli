"""Catalogue and account operations: a thin, normalising layer over ytmusicapi.

Owns nothing YouTube Music already owns. Every function takes an optional
`yt` client so tests inject a fake; results come back as the small Track and
Playlist records the CLI needs and nothing more.
"""
from dataclasses import asdict, dataclass

from ytmusicapi.exceptions import YTMusicError

from ytm.auth import AuthExpired, _EXPIRED_HINT, _SIGNED_OUT_HINT, client, is_expiry

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
    "videos" filter), and never includes personal uploads.
    """
    yt = yt if yt is not None else client()
    try:
        results = yt.search(query, filter="songs", limit=limit)
    except YTMusicError as exc:
        if is_expiry(exc):
            raise AuthExpired(_EXPIRED_HINT) from exc
        raise
    # ytmusicapi treats `limit` as a page-size hint and returns whole pages
    return to_tracks(results)[:limit]


def get_lyrics(video_id, yt=None):
    """Fetch lyrics for a track, or None if it has none.

    Returns (lyrics_text, source) -- both None when the track has no lyrics
    available. Two calls under the hood: the watch playlist gives the lyrics
    browseId, then that id is used to fetch the actual text.
    """
    yt = yt if yt is not None else client()
    try:
        watch = yt.get_watch_playlist(videoId=video_id)
        browse_id = (watch or {}).get("lyrics")
        if not browse_id:
            return None, None
        result = yt.get_lyrics(browse_id)
    except YTMusicError as exc:
        if is_expiry(exc):
            raise AuthExpired(_EXPIRED_HINT) from exc
        raise
    if not result:
        return None, None
    return result.get("lyrics"), result.get("source")


#: personal daily mixes (My Supermix, Discover Mix, ...) all share this prefix
MIX_ID_PREFIX = "RDTMAK"


def is_mix_id(playlist_id):
    """Whether `playlist_id` is one of YouTube's personal daily mixes."""
    return (playlist_id or "").startswith(MIX_ID_PREFIX)


def mixes(yt=None):
    """Personal daily mixes (My Supermix, Discover Mix, ...) from the home feed.

    These are scattered across several home shelves and change daily, so
    they are never cached: read fresh every call, deduped by id and kept in
    feed order. Track count is unknown until the mix itself is fetched.
    """
    yt = yt if yt is not None else client()
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


def library_playlists(limit=25, yt=None):
    """Return the user's remote playlists as Playlist objects."""
    yt = yt if yt is not None else client()
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


def _wrap_ytmusic_error(exc):
    if is_expiry(exc):
        return AuthExpired(_EXPIRED_HINT)
    return exc


def get_playlist(playlist_id, limit=100, yt=None):
    """Fetch one remote playlist's details and tracks.

    Returns (Playlist, [Track, ...]). Real responses sometimes omit
    'trackCount' or 'title', hence the same defensive normalisation used
    elsewhere in this module.
    """
    yt = yt if yt is not None else client()
    try:
        result = yt.get_playlist(playlist_id, limit=limit)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
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


def playlist_count(playlist_id, yt=None):
    """How many tracks a remote playlist holds.

    The library listing omits the count for YouTube's auto-playlists (Liked
    Music, Episodes for Later), so this asks for the playlist itself with a
    one-track page and reads ``trackCount``. Returns None when unknown.
    """
    yt = yt if yt is not None else client()
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


def create_playlist(title, description="", privacy="PRIVATE", yt=None):
    """Create a remote playlist and return its playlist id."""
    yt = yt if yt is not None else client()
    try:
        result = yt.create_playlist(title, description or "", privacy_status=privacy)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    # ytmusicapi has returned either a bare id string or {"playlistId": ...}
    # across versions -- tolerate both.
    if isinstance(result, dict):
        return result.get("playlistId") or ""
    return result or ""


def add_playlist_items(playlist_id, video_ids, yt=None):
    """Add tracks (by video id) to a remote playlist."""
    yt = yt if yt is not None else client()
    try:
        return yt.add_playlist_items(playlist_id, list(video_ids))
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc


def remove_playlist_items(playlist_id, video_ids, yt=None):
    """Remove tracks (by video id) from a remote playlist.

    ytmusicapi's remove call wants the actual playlist-item dicts (it needs
    each track's setVideoId), so the current contents are fetched first and
    filtered down to the requested video ids.
    """
    yt = yt if yt is not None else client()
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


def edit_playlist(playlist_id, title=None, description=None, privacy=None, yt=None):
    """Edit a remote playlist's metadata."""
    yt = yt if yt is not None else client()
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


def delete_playlist(playlist_id, yt=None):
    """Delete a remote playlist. Irreversible -- callers must confirm first."""
    yt = yt if yt is not None else client()
    try:
        return yt.delete_playlist(playlist_id)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc


# -- additions for the simplified core -------------------------------------


def watch_url(video_id):
    """The YouTube Music URL mpv is handed for `video_id`; mpv resolves it."""
    return f"https://music.youtube.com/watch?v={video_id}"


def song(video_id, yt=None):
    """Metadata for one track by id, as a Track; None if YouTube has nothing."""
    yt = yt if yt is not None else client()
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


def radio(video_id, limit=25, yt=None):
    """Tracks YouTube Music would play after `video_id`, seed excluded."""
    yt = yt if yt is not None else client()
    try:
        watch = yt.get_watch_playlist(videoId=video_id, radio=True, limit=limit)
    except YTMusicError as exc:
        raise _wrap_ytmusic_error(exc) from exc
    return [
        track
        for track in to_tracks((watch or {}).get("tracks") or [])
        if track.video_id != video_id
    ][:limit]


def like(video_id, yt=None):
    """Mark `video_id` as liked in the user's account."""
    yt = yt if yt is not None else client()
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
