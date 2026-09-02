"""Playlists pane: lists local and remote playlists side by side.

Kept live by explicit refresh calls from the app (there is no
`playlists_changed` subscription wired up here yet -- the app re-fetches
after any playlist-mutating action it performs).
"""

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ytm.tui.widgets import SelectOnClickTable


class PlaylistsPane(Vertical):
    """The user's playlists, each marked `(local)` or `(remote)`."""

    def compose(self):
        yield Static("PLAYLISTS", id="playlists-title")
        table = SelectOnClickTable(id="playlists-table", cursor_type="row", show_header=False)
        table.add_column("title")
        table.add_column("count")
        table.add_column("kind")
        yield table

    def set_playlists(self, data):
        """Render `data` (the `playlist_list` response payload)."""
        table = self.query_one("#playlists-table", DataTable)
        table.clear()
        playlists = (data or {}).get("playlists") or []
        for playlist in playlists:
            kind = "(local)" if playlist.get("local") else "(remote)"
            table.add_row(
                f"▸ {playlist.get('title', '')}",
                str(playlist.get("track_count", 0)),
                kind,
                key=playlist.get("playlist_id"),
            )

    def selected_playlist_id(self):
        """The `playlist_id` of the highlighted row, or None."""
        table = self.query_one("#playlists-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value if row_key is not None else None
