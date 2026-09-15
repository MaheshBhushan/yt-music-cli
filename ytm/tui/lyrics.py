"""Plain or timestamped lyrics, driven by mpv's actual playback position."""

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ytm.timed_lyrics import normalize_timed_lines

NO_LYRICS_TEXT = "No lyrics available"
LOADING_TEXT = "Loading lyrics…"

#: how the active line is drawn, and how every other line is; tests check
#: these spans on the rendered Text rather than a private index
ACTIVE_STYLE = "bold reverse"
INACTIVE_STYLE = "dim"
ACTIVE_MARKER = "› "


class LyricsPane(Vertical):
    """Highlight the current line and keep it visible, including after seeks.

    `set_lyrics` accepts a plain string or a list of timed records. Timed
    records are validated once, on arrival (`ytm.timed_lyrics`), so the
    per-tick `set_position` scan can index its fields without checks; a
    list with nothing valid in it shows as no lyrics.

    The last observed playback position is kept across `set_lyrics`, so
    lyrics that arrive late highlight where the song actually is.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lines = []
        self._position = 0
        self._active = None
        self._recenter_pending = False

    def compose(self):
        yield Static("LYRICS", id="lyrics-title")
        with VerticalScroll(id="lyrics-scroll"):
            yield Static("", id="lyrics-content", markup=False)

    def reset(self, video_id):
        self._position = 0
        self.set_lyrics(LOADING_TEXT if video_id else NO_LYRICS_TEXT)
        self.query_one("#lyrics-scroll", VerticalScroll).scroll_home(animate=False)

    def set_lyrics(self, text):
        self._lines = normalize_timed_lines(text) if isinstance(text, list) else []
        self._active = None
        self.query_one("#lyrics-title", Static).update("LYRICS · SYNCED" if self._lines else "LYRICS")
        if self._lines:
            self._render_lines()
            self.set_position(self._position)
        elif isinstance(text, str) and text:
            self.query_one("#lyrics-content", Static).update(text)
        else:
            self.query_one("#lyrics-content", Static).update(NO_LYRICS_TEXT)

    def set_position(self, seconds):
        self._position = seconds
        millis = seconds * 1000
        # the half-open interval [start, end): reverse scan so that of two
        # overlapping lines the one that started last wins
        active = next((i for i in range(len(self._lines) - 1, -1, -1)
                       if self._lines[i]["start_time"] <= millis < self._lines[i]["end_time"]), None)
        if active != self._active:
            self._active = active
            self._render_lines()
            if active is not None:
                self._schedule_recenter()

    def lyrics_text(self):
        """The Rich Text the pane shows for timed lyrics (empty when plain)."""
        text = Text()
        for i, line in enumerate(self._lines):
            active = i == self._active
            text.append((ACTIVE_MARKER if active else "  ") + line["text"],
                        style=ACTIVE_STYLE if active else INACTIVE_STYLE)
            text.append("\n")
        return text

    def _render_lines(self):
        self.query_one("#lyrics-content", Static).update(self.lyrics_text())

    def on_resize(self, event):
        # a narrower pane wraps the lines above the active one into more
        # rows, so the old scroll offset no longer shows it; paused, no
        # line change would ever come along to repair that
        self._schedule_recenter()

    def on_show(self, event):
        # leaving compact mode: the pane had no width while hidden
        self._schedule_recenter()

    def _schedule_recenter(self):
        """Recenter once layout has caught up; a burst of resizes is one call."""
        if self._active is None or self._recenter_pending:
            return
        self._recenter_pending = True
        self.call_after_refresh(self._recenter)

    def _recenter(self):
        self._recenter_pending = False
        self._scroll_to_active()

    def active_row_span(self, width=None):
        """(first_row, last_row) of the active line in terminal rows of the
        content, wrapped at `width` (default: the content's current width).
        None without an active line. Rich does the wrapping, so wide glyphs
        and combining characters count as the terminal draws them."""
        if self._active is None:
            return None
        content = self.query_one("#lyrics-content", Static)
        width = max(1, width or content.content_size.width)
        console = self.app.console

        def rows(lines):
            if not lines:
                return 0
            return len(Text("\n".join("  " + line["text"] for line in lines)).wrap(console, width))

        first = rows(self._lines[:self._active])
        return first, first + max(1, rows([self._lines[self._active]])) - 1

    def _scroll_to_active(self):
        if self._active is None or not self.is_attached or not self.display:
            return
        scroll = self.query_one("#lyrics-scroll", VerticalScroll)
        height = scroll.content_size.height
        if height <= 0 or self.query_one("#lyrics-content", Static).content_size.width <= 0:
            return  # not laid out yet (or hidden); `on_show`/`on_resize` will come back
        first, _last = self.active_row_span()
        scroll.scroll_to(y=max(0, first - height // 2), animate=False)

    def set_error(self, message):
        self.set_lyrics(f"lyrics error: {message}")
