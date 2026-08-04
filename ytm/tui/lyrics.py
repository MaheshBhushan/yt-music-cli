"""Lyrics pane: shows lyrics for the currently playing track.

Kept live by `track_changed` daemon events -- the app fetches lyrics off
the UI thread (mirroring its daemon-listener thread + `post_message`
pattern) and pushes the result in here via `set_lyrics`/`set_error`.
"""

from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

NO_LYRICS_TEXT = "No lyrics available"


class LyricsPane(Vertical):
    """Title bar plus a scrollable lyrics body."""

    def compose(self):
        yield Static("LYRICS", id="lyrics-title")
        with VerticalScroll(id="lyrics-scroll"):
            yield Static("", id="lyrics-content")

    def set_lyrics(self, text):
        """Render `text` (already resolved to the no-lyrics fallback by the
        caller when the daemon returned `lyrics: null`)."""
        self.query_one("#lyrics-content", Static).update(text or NO_LYRICS_TEXT)

    def set_error(self, message):
        """Render a fetch failure without raising."""
        self.query_one("#lyrics-content", Static).update(f"lyrics error: {message}")
