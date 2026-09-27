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
        # Textual dispatches handlers through the MRO itself. We call the
        # parent explicitly, so suppress its second automatic invocation.
        event.prevent_default()
        meta = event.style.meta
        row = meta.get("row", -1)
        column = meta.get("column", -1)
        on_a_row = row >= 0 and column >= 0
        # DataTable compares the whole coordinate, even in row-cursor mode.
        already_selected = on_a_row and (row, column) == self.cursor_coordinate
        await super()._on_click(event)
        if on_a_row and not already_selected and self.show_cursor and self.cursor_type == "row":
            self._post_selected_message()
