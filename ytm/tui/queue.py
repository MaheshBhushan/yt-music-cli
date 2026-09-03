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
        previous_key = self._selected_key()
        previous_index = getattr(self, "_index", None)
        table.clear()
        tracks = (data or {}).get("tracks") or []
        index = (data or {}).get("index")
        self._tracks = tracks
        self._index = index
        for position, track in enumerate(tracks):
            marker = ">" if position == index else " "
            label = f"{track.get('title', '')} — {track.get('artist', '')}"
            table.add_row(f"{marker}{position + 1}.", label, key=str(position))
        # `clear()` parks the cursor on row 0, so `A`/`a` would always mean the
        # first song. Follow the playing track unless the user moved the cursor
        # off it, in which case keep their row.
        following = previous_key is None or previous_key == str(previous_index)
        target = index if following else previous_key
        if target is not None and 0 <= int(target) < len(tracks):
            table.move_cursor(row=int(target))

    def _selected_key(self):
        table = self.query_one("#queue-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value if row_key is not None else None

    def selected_track(self):
        """The Track dict for the currently highlighted row, or None."""
        key = self._selected_key()
        tracks = getattr(self, "_tracks", [])
        try:
            return tracks[int(key)]
        except (TypeError, ValueError, IndexError):
            return None
