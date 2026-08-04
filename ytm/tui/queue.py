"""Queue pane: shows the play queue, kept live by `queue_changed` events."""

from textual.containers import Vertical
from textual.widgets import DataTable, Static


class QueuePane(Vertical):
    """The current play queue, with the playing track marked."""

    def compose(self):
        yield Static("QUEUE", id="queue-title")
        table = DataTable(id="queue-table", cursor_type="row", show_header=False)
        table.add_column("#")
        table.add_column("track")
        yield table

    def set_queue(self, data):
        """Render `data` (the `queue_get`/`queue_changed` payload)."""
        table = self.query_one("#queue-table", DataTable)
        table.clear()
        tracks = (data or {}).get("tracks") or []
        index = (data or {}).get("index")
        for position, track in enumerate(tracks):
            marker = ">" if position == index else " "
            label = f"{track.get('title', '')} — {track.get('artist', '')}"
            table.add_row(f"{marker}{position + 1}.", label, key=track.get("video_id"))
