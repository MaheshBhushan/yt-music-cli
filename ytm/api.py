"""Backward-compatible catalogue imports.

The implementation lives in :mod:`ytm.music`. Keep this module so code
written against pre-0.6 releases does not fail at import time.
"""

from ytm import music as _music
from ytm.music import *

_album_name = _music._album_name
_duration = _music._duration
_join_artists = _music._join_artists
_wrap_ytmusic_error = _music._wrap_ytmusic_error
