"""Queue pane: shows the play queue, kept live by `queue_changed` events."""

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ytm.tui.widgets import SelectOnClickTable


class QueuePane(Vertical):
    """The current play queue, with the playing track marked."""

    def compose(self):
        yield Static("QUEUE", id="queue-title")
        table = SelectOnClickTable(id="queue-table", cursor_type="row", show_header=False)
        table.add_column("#")
        table.add_column("track")
        yield table

    def set_queue(self, data):
        """Render `data` (the `queue_get`/`queue_changed` payload)."""
        table = self.query_one("#queue-table", DataTable)
        table.clear()
        tracks = (data or {}).get("tracks") or []
        index = (data or {}).get("index")
        self._tracks = tracks
        for position, track in enumerate(tracks):
            marker = ">" if position == index else " "
            label = f"{track.get('title', '')} — {track.get('artist', '')}"
            table.add_row(f"{marker}{position + 1}.", label, key=str(position))

    def selected_track(self):
        """The Track dict for the currently highlighted row, or None."""
        table = self.query_one("#queue-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
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
