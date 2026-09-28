"""Queue pane: shows the play queue, kept live by `queue_changed` events."""

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ytm.tui.widgets import SelectOnClickTable


class QueuePane(Vertical):
    """The current play queue, with the playing track marked.

    Rows are keyed by the player's queue-entry id when the backend supplies
    one, so a deliberate selection follows the same entry when the queue is
    inserted into, reordered or reduced. Payloads without entry identity
    (older daemons, tests) fall back to the row position explicitly.
    """

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
        previous_row = table.cursor_row
        previous_playing = self._playing_key()
        table.clear()
        tracks = (data or {}).get("tracks") or []
        index = (data or {}).get("index")
        self._tracks = tracks
        self._index = index
        self._keys = []
        self._by_key = {}
        for position, track in enumerate(tracks):
            entry_id = track.get("entry_id")
            key = entry_id if entry_id is not None else f"position:{position}"
            self._keys.append(key)
            self._by_key[key] = track
            marker = ">" if position == index else " "
            label = f"{track.get('title', '')} — {track.get('artist', '')}"
            table.add_row(f"{marker}{position + 1}.", label, key=key)
        # `clear()` parks the cursor on row 0, so `a`/`q` would always mean the
        # first song. Follow the playing track unless the user moved the cursor
        # off it, in which case keep their entry selected. If that entry is
        # gone, keep the row nearest to where they were.
        following = previous_key is None or previous_key == previous_playing
        target = index if following else previous_key
        if following and target is not None:
            target = self._keys[target] if 0 <= target < len(self._keys) else None
        row = self._row_of(target)
        if row is None and previous_row is not None and tracks:
            row = min(previous_row, len(tracks) - 1)
        if row is not None:
            table.move_cursor(row=row)

    def _row_of(self, key):
        try:
            return self._keys.index(key)
        except (ValueError, AttributeError):
            return None

    def _playing_key(self):
        """The row key of the entry that was playing before this render."""
        index = getattr(self, "_index", None)
        keys = getattr(self, "_keys", [])
        if isinstance(index, int) and 0 <= index < len(keys):
            return keys[index]
        return None

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
        return getattr(self, "_by_key", {}).get(self._selected_key())

    def play_args(self, key=None):
        """Request arguments that play the entry at `key` right now.

        The stable entry id is preferred: the backend resolves it against
        mpv's playlist at execution time, so a queue mutation between the
        click and the command cannot play a different row. Payloads without
        entry identity fall back to the position in the current snapshot.
        """
        if key is None:
            key = self._selected_key()
        track = getattr(self, "_by_key", {}).get(key)
        if track is None:
            return None
        entry_id = track.get("entry_id")
        if entry_id is not None:
            return {"entry_id": entry_id}
        row = self._row_of(key)
        return None if row is None else {"index": row}
