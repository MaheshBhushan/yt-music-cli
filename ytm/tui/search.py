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


class SearchPane(Vertical):
    """Search box on top, results table below."""

    def compose(self):
        yield Input(placeholder="Search...", id="search-input")
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
        self._render_rows()

    def _render_rows(self):
        tracks = getattr(self, "_tracks", None)
        if tracks is None:
            return
        table = self.query_one("#search-results", DataTable)
        widths = {key: table.columns[key].width for key in COLUMNS if key in table.columns}
        table.clear()
        for position, track in enumerate(tracks):
            table.add_row(
                _truncated(track.get("title", ""), widths.get("TITLE")),
                _truncated(track.get("artist", ""), widths.get("ARTIST")),
                _truncated(track.get("album", ""), widths.get("ALBUM")),
                _truncated(track.get("duration", ""), widths.get("TIME")),
                key=str(position),
            )

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
        table = self.query_one("#search-results", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        tracks = getattr(self, "_tracks", [])
        try:
            return tracks[int(row_key.value)]
        except (TypeError, ValueError, IndexError):
            return None
