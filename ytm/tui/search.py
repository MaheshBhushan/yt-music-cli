"""Search pane: an Input plus a scrollable results DataTable."""

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import DataTable, Input

from ytm.tui.widgets import SelectOnClickTable

COLUMNS = ("TITLE", "ARTIST", "ALBUM", "TIME")

# Column widths as a fraction of the table's current width, except TIME which
# is a fixed number of cells (durations are short and don't need scaling).
COLUMN_FRACTIONS = {"TITLE": 0.45, "ARTIST": 0.25, "ALBUM": 0.20}
TIME_WIDTH = 6
MIN_COLUMN_WIDTH = 4


def _truncated(value, width):
    """A Text cell that ellipsizes instead of wrapping or overflowing."""
    text = Text(str(value), no_wrap=True, overflow="ellipsis")
    if width:
        text.truncate(width, overflow="ellipsis")
    return text


#: keys the search box lets through to the app's volume bindings. Textual
#: hands an Input every printable key before priority bindings are tried;
#: `+` and `-` slip past only because their key names ("plus", "minus")
#: have no character mapping, while "equals_sign" does.
VOLUME_KEYS = frozenset({"plus", "minus", "equals_sign"})


class SearchInput(Input):
    """The search box: every printable key is text except the volume keys."""

    def check_consume_key(self, key, character):
        if key in VOLUME_KEYS:
            return False
        return super().check_consume_key(key, character)


class SearchPane(Vertical):
    """Search box on top, results table below."""

    def compose(self):
        yield SearchInput(placeholder="Search...  (Esc or ↓ for the lists, l for playlists)", id="search-input")
        table = SelectOnClickTable(id="search-results", cursor_type="row")
        for column in COLUMNS:
            table.add_column(column, key=column, width=MIN_COLUMN_WIDTH)
        yield table

    def on_mount(self):
        self._apply_column_widths()

    def on_resize(self):
        self._apply_column_widths()

    def _apply_column_widths(self):
        """Size columns from the table's current width, then re-render rows.

        Fixed widths (rather than Textual's default auto-sizing to the
        widest cell) keep the table within the terminal width regardless of
        how long a title/artist/album gets -- long cells are truncated with
        an ellipsis instead of pushing other columns off screen.

        This is a layout-only refresh: it must keep the highlighted result
        and the scroll position, unlike a new result set (UI-02).
        """
        table = self.query_one("#search-results", DataTable)
        width = table.size.width or 80
        # Each column's rendered width also includes cell padding on both
        # sides, so that overhead has to come off before splitting the
        # remaining space by fraction -- otherwise the columns collectively
        # overflow the table and the last one (TIME) gets clipped entirely.
        overhead = 2 * table.cell_padding * len(COLUMNS)
        usable = max(width - overhead, MIN_COLUMN_WIDTH * len(COLUMNS))
        for key, fraction in COLUMN_FRACTIONS.items():
            column = table.columns.get(key)
            if column is not None:
                column.width = max(int(usable * fraction), MIN_COLUMN_WIDTH)
        time_column = table.columns.get("TIME")
        if time_column is not None:
            time_column.width = TIME_WIDTH
        self._render_rows(preserve_selection=True)

    def _render_rows(self, preserve_selection=False):
        tracks = getattr(self, "_tracks", None)
        if tracks is None:
            return
        table = self.query_one("#search-results", DataTable)
        widths = {key: table.columns[key].width for key in COLUMNS if key in table.columns}
        previous_key = self._selected_key() if preserve_selection else None
        previous_row = table.cursor_row if preserve_selection else None
        previous_scroll = table.scroll_offset.y if preserve_selection else 0
        table.clear()
        for position, track in enumerate(tracks):
            table.add_row(
                _truncated(track.get("title", ""), widths.get("TITLE")),
                _truncated(track.get("artist", ""), widths.get("ARTIST")),
                _truncated(track.get("album", ""), widths.get("ALBUM")),
                _truncated(track.get("duration", ""), widths.get("TIME")),
                key=str(position),
            )
        if not tracks:
            return
        if preserve_selection:
            row = self._row_of_key(previous_key)
            if row is None or not 0 <= row < len(tracks):
                # the same list was rebuilt, or it shrank under the cursor
                row = min(previous_row or 0, len(tracks) - 1)
            table.move_cursor(row=row, scroll=False)
            if previous_scroll:
                table.scroll_y = previous_scroll
            return
        table.move_cursor(row=0)

    @staticmethod
    def _row_of_key(key):
        try:
            return int(str(key))
        except (TypeError, ValueError):
            return None

    def _selected_key(self):
        table = self.query_one("#search-results", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value if row_key is not None else None

    def set_results(self, tracks):
        """Replace the results table's rows with `tracks` (list of dicts).

        Each row's key is its position in `tracks`, so the selected track can
        be recovered later without re-parsing displayed text -- results can
        legitimately repeat a video_id, so position (not video_id) is used
        as the row key.
        """
        self._tracks = tracks
        self._render_rows()

    def selected_track(self):
        """The Track dict for the currently highlighted row, or None."""
        tracks = getattr(self, "_tracks", [])
        try:
            return tracks[int(self._selected_key())]
        except (TypeError, ValueError, IndexError):
            return None
