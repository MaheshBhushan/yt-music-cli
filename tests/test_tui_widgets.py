"""A mouse gesture must emit one selection, including across columns."""

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from ytm.tui.widgets import SelectOnClickTable


def test_row_clicks_across_columns_and_header_dispatch_once():
    class TableApp(App):
        def __init__(self):
            super().__init__()
            self.rows = []
            self.headers = []

        def compose(self) -> ComposeResult:
            yield SelectOnClickTable(id="table", cursor_type="row")

        def on_mount(self):
            table = self.query_one(DataTable)
            table.add_column("One", width=10)
            table.add_column("Two", width=10)
            table.add_row("a", "b")
            table.add_row("c", "d")

        def on_data_table_row_selected(self, event):
            self.rows.append(event.cursor_row)

        def on_data_table_header_selected(self, event):
            self.headers.append(event.column_index)

    async def scenario():
        app = TableApp()
        async with app.run_test() as pilot:
            await pilot.click("#table", offset=(14, 2))
            await pilot.pause()
            assert app.rows == [1]
            # Same row, different column: parent doesn't emit selection here.
            await pilot.click("#table", offset=(2, 2))
            await pilot.pause()
            assert app.rows == [1, 1]
            await pilot.click("#table", offset=(2, 0))
            await pilot.pause()
            assert app.headers == [0]
            assert app.rows == [1, 1]

    asyncio.run(scenario())
