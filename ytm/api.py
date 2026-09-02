"""Compatibility shim: the catalogue layer now lives in :mod:`ytm.music`.

Kept only until the daemon and Textual TUI that import it are removed.
"""

from ytm.music import *  # noqa: F401,F403
from ytm.music import (  # noqa: F401
    Playlist,
    Track,
    UPLOAD_VIDEO_TYPE,
    _album_name,
    _duration,
    _join_artists,
    _wrap_ytmusic_error,
    add_playlist_items,
    create_playlist,
    delete_playlist,
    edit_playlist,
    get_lyrics,
    get_playlist,
    is_upload,
    library_playlists,
    remove_playlist_items,
    search,
    to_playlist,
    to_track,
    to_tracks,
)
