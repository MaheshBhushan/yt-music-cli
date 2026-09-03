"""Offline cache of downloaded track audio.

Downloads a ``videoId``'s audio via yt-dlp (reusing the cookie/options
approach from :mod:`ytm.resolve`) into a cache directory under
``~/.cache/ytm/tracks``, so a track can be replayed later without hitting
the network or resolving a (short-lived) stream URL at all.

Two correctness properties matter more than anything else here:

* A partial/interrupted download must never be mistaken for a complete
  cache entry. Downloads land in a private temp directory and are only
  moved into the cache directory -- via an atomic rename -- once yt-dlp has
  finished successfully. If anything raises along the way, the temp
  directory is discarded and the cache directory never sees a partial file.
* The cache has a size cap enforced by evicting the least-recently-*used*
  entries (recency of playback, tracked via each file's mtime, which is
  touched on every read), not least-recently-downloaded.

Unlike stream URLs, the files this module writes are the point -- they are
supposed to persist. What must never be persisted is a resolved stream URL
itself (see ytm.resolve); this module never touches those.
"""

import os
import shutil
import tempfile
from pathlib import Path

import yt_dlp

from ytm import config as config_mod

#: default location for cached track audio
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "ytm" / "tracks"

#: default total size cap for the cache, in bytes
DEFAULT_CAP_BYTES = 2 * 1024**3

_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


class CacheError(Exception):
    """A download completed without producing a usable file."""


def _resolve_cache_dir(cache_dir=None):
    """The cache directory to use, created if necessary."""
    cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


# -- lookup ------------------------------------------------------------------


def get_cached_path(video_id, cache_dir=None, touch=True):
    """The local path for `video_id` if fully cached, else None.

    Marks the entry as just-used (for LRU purposes) unless `touch` is False.
    """
    cache_dir = _resolve_cache_dir(cache_dir)
    matches = sorted(cache_dir.glob(f"{video_id}.*"))
    if not matches:
        return None
    path = matches[0]
    if touch:
        os.utime(path, None)
    return path


def is_cached(video_id, cache_dir=None):
    """Whether `video_id` has a complete cache entry."""
    return get_cached_path(video_id, cache_dir=cache_dir, touch=False) is not None


def list_cached(cache_dir=None):
    """All complete cache entries as dicts with video_id, path, size, mtime."""
    cache_dir = _resolve_cache_dir(cache_dir)
    entries = []
    for path in sorted(cache_dir.glob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append(
            {
                "video_id": path.stem,
                "path": path,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return entries


# -- mutation -----------------------------------------------------------------


def remove(video_id, cache_dir=None):
    """Delete the cache entry for `video_id`, if any. Returns whether removed."""
    cache_dir = _resolve_cache_dir(cache_dir)
    removed = False
    for path in cache_dir.glob(f"{video_id}.*"):
        path.unlink()
        removed = True
    return removed


def download(video_id, cache_dir=None, cap_bytes=DEFAULT_CAP_BYTES, ydl_class=yt_dlp.YoutubeDL):
    """Download `video_id`'s audio into the cache and return its local path.

    Downloads to a private temporary directory first; the file is only
    moved into the cache directory (via an atomic rename) after a
    successful download, so an interrupted download never leaves behind
    something that looks like a complete cache entry. Applies the LRU size
    cap (if any) after the download lands.
    """
    cache_dir = _resolve_cache_dir(cache_dir)
    tmp_root = cache_dir / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(dir=tmp_root))
    try:
        outtmpl = str(work_dir / f"{video_id}.%(ext)s")
        url = _WATCH_URL.format(video_id=video_id)
        with ydl_class(_ydl_opts(outtmpl)) as ydl:
            ydl.extract_info(url, download=True)

        downloaded = [p for p in work_dir.glob(f"{video_id}.*") if p.is_file()]
        if not downloaded:
            raise CacheError(f"download of {video_id!r} produced no file")
        src = downloaded[0]
        dest = cache_dir / src.name
        remove(video_id, cache_dir=cache_dir)  # drop a stale entry with a different ext
        os.replace(src, dest)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if cap_bytes is not None:
        enforce_cap(cap_bytes, cache_dir=cache_dir)
    return dest


def enforce_cap(cap_bytes, cache_dir=None):
    """Evict least-recently-used entries until the cache is under `cap_bytes`.

    Recency is each file's mtime, which callers touch on every use via
    `get_cached_path`. Returns the list of evicted video_ids.
    """
    cache_dir = _resolve_cache_dir(cache_dir)
    entries = sorted(list_cached(cache_dir=cache_dir), key=lambda e: e["mtime"])
    total = sum(e["size"] for e in entries)
    evicted = []
    for entry in entries:
        if total <= cap_bytes:
            break
        try:
            entry["path"].unlink()
        except OSError:
            continue
        total -= entry["size"]
        evicted.append(entry["video_id"])
    return evicted


# -- playback integration -----------------------------------------------------


def _ydl_opts(outtmpl):
    """yt-dlp options for an anonymous audio download into `outtmpl`.

    Anonymous on purpose: with account cookies YouTube serves URLs that need
    an account-bound PO token, and the download then fails with 403 (the
    same reason ``behaviour.authenticated_streams`` defaults to off).
    """
    opts = {
        "format": "bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
    }
    pot = config_mod.load()["pot"]
    if pot["enabled"]:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [pot["base_url"]]}}
    return opts
