"""Search pane: an Input plus a scrollable results DataTable."""

from textual.containers import Vertical
from textual.widgets import DataTable, Input

COLUMNS = ("TITLE", "ARTIST", "ALBUM", "TIME")


class SearchPane(Vertical):
    """Search box on top, results table below."""

    def compose(self):
        yield Input(placeholder="Search...", id="search-input")
        table = DataTable(id="search-results", cursor_type="row")
        for column in COLUMNS:
            table.add_column(column, key=column)
        yield table

    def set_results(self, tracks):
        """Replace the results table's rows with `tracks` (list of dicts).

        Each row's key is its position in `tracks`, so the selected track can
        be recovered later without re-parsing displayed text -- results can
        legitimately repeat a video_id, so position (not video_id) is used
        as the row key.
        """
        table = self.query_one("#search-results", DataTable)
        table.clear()
        self._tracks = tracks
        for position, track in enumerate(tracks):
            table.add_row(
                track.get("title", ""),
                track.get("artist", ""),
                track.get("album", ""),
                track.get("duration", ""),
                key=str(position),
            )

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
