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

        Each row's key is the track's video_id, so the selected track can be
        recovered later without re-parsing displayed text.
        """
        table = self.query_one("#search-results", DataTable)
        table.clear()
        for track in tracks:
            table.add_row(
                track.get("title", ""),
                track.get("artist", ""),
                track.get("album", ""),
                track.get("duration", ""),
                key=track.get("video_id"),
            )
        self._tracks = {track.get("video_id"): track for track in tracks}

    def selected_track(self):
        """The Track dict for the currently highlighted row, or None."""
        table = self.query_one("#search-results", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return getattr(self, "_tracks", {}).get(row_key.value)
