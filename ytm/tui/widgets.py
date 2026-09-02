"""Small shared widgets for the TUI."""

from textual.widgets import DataTable


class SelectOnClickTable(DataTable):
    """A DataTable where one click on a row selects it.

    Textual's DataTable needs two clicks: the first moves the cursor, only a
    click on the already-highlighted row posts `RowSelected`. For a list of
    songs that reads as "clicking does nothing", so the first click selects
    too. Keyboard behaviour (arrows highlight, Enter selects) is unchanged.
    """

    async def _on_click(self, event):
        meta = event.style.meta
        row = meta.get("row", -1)
        on_a_row = "column" in meta and row >= 0
        already_selected = on_a_row and row == self.cursor_row  # super() posts it
        await super()._on_click(event)
        if on_a_row and not already_selected and self.show_cursor and self.cursor_type == "row":
            self._post_selected_message()
