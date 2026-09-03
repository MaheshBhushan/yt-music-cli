"""Playlists pane: local and remote playlists side by side, plus a row to
create a new one.

Kept live by explicit refresh calls from the app (there is no
`playlists_changed` subscription wired up here yet -- the app re-fetches
after any playlist-mutating action it performs).
"""

from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static

from ytm.tui.widgets import SelectOnClickTable

#: row key of the "+ new playlist" entry
NEW_KEY = "__new__"
NEW_LABEL = "+ new playlist"


class PlaylistsPane(Vertical):
    """The user's playlists, each marked `(local)` or `(remote)`."""

    def compose(self):
        yield Static("PLAYLISTS", id="playlists-title")
        # shown only while naming a new playlist (see prompt_new)
        yield Input(placeholder="name for the new playlist, Enter to create", id="playlist-name")
        table = SelectOnClickTable(id="playlists-table", cursor_type="row", show_header=False)
        table.add_column("title")
        table.add_column("count")
        table.add_column("kind")
        yield table

    def set_playlists(self, data):
        """Render `data` (the `playlist_list` response payload)."""
        table = self.query_one("#playlists-table", DataTable)
        table.clear()
        self._titles = {}
        for playlist in (data or {}).get("playlists") or []:
            kind = "(local)" if playlist.get("local") else "(remote)"
            self._titles[playlist.get("playlist_id")] = playlist.get("title", "")
            table.add_row(
                f"▸ {playlist.get('title', '')}",
                str(playlist.get("track_count", 0)),
                kind,
                key=playlist.get("playlist_id"),
            )
        table.add_row(NEW_LABEL, "", "", key=NEW_KEY)

    def _selected_key(self):
        table = self.query_one("#playlists-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value if row_key is not None else None

    def selected_playlist_id(self):
        """The `playlist_id` of the highlighted row, or None (also for the
        "+ new playlist" row -- see `new_selected`)."""
        key = self._selected_key()
        return None if key == NEW_KEY else key

    def new_selected(self):
        """True when the highlighted row is the "+ new playlist" entry."""
        return self._selected_key() == NEW_KEY

    def title_of(self, playlist_id):
        return getattr(self, "_titles", {}).get(playlist_id, "")

    def prompt_new(self):
        """Show the name box and give it focus."""
        box = self.query_one("#playlist-name", Input)
        box.value = ""
        box.display = True
        box.focus()

    def close_prompt(self):
        box = self.query_one("#playlist-name", Input)
        box.display = False
        self.query_one("#playlists-table", DataTable).focus()
