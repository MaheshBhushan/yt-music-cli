"""Which JavaScript runtime yt-dlp may use to solve YouTube challenges.

One discovery point for both consumers: playback hands mpv a yt-dlp raw
option, and the cache downloads through yt-dlp's Python API. Their option
formats differ (a name string versus a mapping), so this module exposes the
runtime and each consumer adapts it.

Only the runtime *name* is propagated. ``shutil.which`` already proved the
executable is on PATH, and a name cannot break mpv's comma-separated
``--ytdl-raw-options`` list the way a path containing a comma could.

With no runtime installed, discovery returns None and each consumer keeps
its previous behaviour (yt-dlp falls back to its own default, which enables
only Deno). Nothing here installs software.
"""

import shutil

#: preference order, matching the playback selector this replaces
PREFERENCE = ("deno", "node")


def find():
    """The name of the first installed runtime, or None."""
    for name in PREFERENCE:
        if shutil.which(name):
            return name
    return None


def ytdlp_option():
    """The ``js_runtimes`` value for yt-dlp's Python API, or None.

    The API takes a mapping of runtime name to runtime options; an empty
    options dict means "find this runtime on PATH".
    """
    name = find()
    return {name: {}} if name else None
