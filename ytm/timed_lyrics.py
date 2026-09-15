"""The one place timed lyric lines are validated.

The provider (ytmusicapi's ``LyricLine``) normally supplies well-formed
records, but the TUI compares and indexes these fields on every playback
tick inside a Textual message handler, where a ``KeyError`` or a ``TypeError``
takes the whole interface down. So the contract is checked once, here, at
the music/backend boundary, and the widgets trust what they are given.

A normalized line is ``{"text": str, "start_time": int, "end_time": int}``
with ``0 <= start_time < end_time`` in milliseconds (plus ``id`` when the
provider had one). Zero-length markers are dropped: with a half-open
``[start, end)`` interval they can never be active, and inventing a duration
would be guessing. Blank text is kept: an empty timed line is how an
instrumental gap is represented. Overlapping intervals are allowed; the pane
shows the one that started last.
"""

import logging
import math

log = logging.getLogger(__name__)


def _millis(value):
    """`value` as an int of milliseconds, or None when it is not a finite number."""
    # bool is an int subclass; True/False as a timestamp is a bug, not a time
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def normalize_timed_lines(lines):
    """Validate provider records into the contract above.

    Invalid records are skipped with a concise debug line (index and the
    field at fault, never the lyric text). Returns the valid lines stably
    sorted by start time; an empty list when nothing usable remains, so the
    caller can fall back to plain lyrics.
    """
    if not isinstance(lines, (list, tuple)):
        return []
    valid = []
    for index, line in enumerate(lines):
        getter = getattr(line, "get", None)
        if getter is None:
            log.debug("timed lyric %d skipped: record is %s", index, type(line).__name__)
            continue
        text = getter("text")
        if not isinstance(text, str):
            log.debug("timed lyric %d skipped: text is %s", index, type(text).__name__)
            continue
        start = _millis(getter("start_time"))
        end = _millis(getter("end_time"))
        if start is None or end is None:
            log.debug("timed lyric %d skipped: non-numeric start_time/end_time", index)
            continue
        if start < 0 or end <= start:
            log.debug("timed lyric %d skipped: interval %d..%d", index, start, end)
            continue
        record = {"text": text, "start_time": start, "end_time": end}
        line_id = getter("id")
        if line_id is not None:
            record["id"] = line_id
        valid.append(record)
    valid.sort(key=lambda record: record["start_time"])
    return valid
