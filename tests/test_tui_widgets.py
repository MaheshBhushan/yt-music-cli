"""Mouse gestures, layout refreshes and now-playing state, headless.

These tests cover the widget-level defects from the UI review: a layout-only
search refresh must not move the selection (UI-02), a track transition must
not inherit the previous duration (UI-04), queue labels must fit their cell
budget (UI-11), and a pending cover fetch must not leave the old art next to
the new title (UI-15).
"""

import asyncio

from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from ytm.tui.nowplaying import (
    AlbumArt,
    NowPlaying,
    QueueSummaryLayout,
    queue_track_label,
)
from ytm.tui.search import SearchPane
from ytm.tui.widgets import SelectOnClickTable

TRACK = {
    "video_id": "abc123",
    "title": "Kaanave Kaanave",
    "artist": "Sid Sriram",
    "album": "Sarvam Thaala Mayam",
    "duration": "5:12",
    "duration_seconds": 312,
}


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


# -- UI-02: a layout refresh keeps the highlighted search result -----------------


class SearchPaneApp(App):
    def compose(self) -> ComposeResult:
        yield SearchPane()


def test_search_resize_preserves_selected_result():
    async def scenario():
        app = SearchPaneApp()
        async with app.run_test(size=(120, 40)) as pilot:
            pane = app.query_one(SearchPane)
            table = app.query_one("#search-results", DataTable)
            pane.set_results([dict(TRACK, title=f"track {i}") for i in range(14)])
            table.focus()
            table.move_cursor(row=3)
            await pilot.pause()

            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert table.cursor_row == 3
            assert pane.selected_track()["title"] == "track 3"

            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            assert table.cursor_row == 3
            assert pane.selected_track()["title"] == "track 3"
            assert app.focused is table  # resize must not steal focus

    asyncio.run(scenario())


def test_search_scrolled_selection_survives_a_layout_refresh():
    async def scenario():
        app = SearchPaneApp()
        async with app.run_test(size=(100, 24)) as pilot:
            pane = app.query_one(SearchPane)
            table = app.query_one("#search-results", DataTable)
            pane.set_results([dict(TRACK, title=f"track {i}") for i in range(60)])
            table.focus()
            await pilot.pause()
            table.scroll_y = table.max_scroll_y
            await pilot.pause()
            selected_row = table.cursor_row
            scroll_before = table.scroll_offset.y
            pane._apply_column_widths()
            await pilot.pause()
            assert table.cursor_row == selected_row
            assert table.scroll_offset.y == scroll_before

    asyncio.run(scenario())


def test_empty_and_shrunken_search_results_clamp_the_cursor():
    async def scenario():
        app = SearchPaneApp()
        async with app.run_test(size=(120, 40)) as _pilot:
            pane = app.query_one(SearchPane)
            table = app.query_one("#search-results", DataTable)
            assert pane.selected_track() is None
            pane._apply_column_widths()  # no results yet: no crash
            pane.set_results([dict(TRACK, title=f"track {i}") for i in range(5)])
            table.move_cursor(row=4)
            # a legitimate replacement may pick row 0, but must stay in range
            pane.set_results([dict(TRACK, title="only")])
            assert table.row_count == 1
            assert 0 <= table.cursor_row < 1
            assert pane.selected_track()["title"] == "only"
            pane._apply_column_widths()
            assert table.cursor_row == 0
            pane.set_results([])
            assert pane.selected_track() is None

    asyncio.run(scenario())


# -- UI-04: a new entry starts with clean timing ---------------------------------


class NowPlayingApp(App):
    def __init__(self):
        super().__init__()
        self.seeks = []

    def compose(self) -> ComposeResult:
        yield NowPlaying()

    def on_now_playing_seek_requested(self, message):
        self.seeks.append(message.seconds)


class FakeClick:
    def __init__(self, x, y):
        self.screen_x = x
        self.screen_y = y


def test_track_transition_resets_unknown_duration_and_seek():
    async def scenario():
        app = NowPlayingApp()
        async with app.run_test(size=(120, 40)) as pilot:
            pane = app.query_one(NowPlaying)
            bar = pane.query_one("#now-playing-progress")
            clock = pane.query_one("#now-playing-time")

            # a known-duration entry with its position
            pane.on_track_changed(dict(TRACK, entry_id=1))
            pane.on_position({
                "entry_id": 1, "video_id": "abc123",
                "position": 123, "duration_seconds": 240,
            })
            assert bar.total == 240
            assert str(clock.render()) == "2:03 / 4:00"

            # the next entry has no duration yet: nothing may be inherited
            pane.on_track_changed(dict(
                TRACK, entry_id=2, video_id="unknown", title="Next", duration_seconds=0,
            ))
            assert pane._duration_seconds == 0
            assert bar.total is None
            assert str(clock.render()) == "0:00 / 0:00"

            # a late position from the old entry is ignored, including its seek
            pane.on_position({
                "entry_id": 1, "video_id": "abc123",
                "position": 200, "duration_seconds": 240,
            })
            assert bar.progress == 0 and pane._duration_seconds == 0
            pane.on_click(FakeClick(bar.region.x + bar.region.width // 2, bar.region.y))
            await pilot.pause()
            assert app.seeks == []

            # the new entry's own update brings a duration and re-enables seek
            pane.on_position({
                "entry_id": 2, "video_id": "unknown",
                "position": 5, "duration_seconds": 180,
            })
            assert bar.total == 180 and bar.progress == 5
            assert str(clock.render()) == "0:05 / 3:00"
            pane.on_click(FakeClick(bar.region.x + bar.region.width // 2, bar.region.y))
            await pilot.pause()
            assert len(app.seeks) == 1 and 80 <= app.seeks[0] <= 100

            # redundant metadata for the same entry keeps the known clock
            pane.on_track_changed(dict(TRACK, entry_id=2, video_id="unknown", title="Next"))
            assert pane._duration_seconds == 180
            assert str(clock.render()) == "0:05 / 3:00"

            # an idle player clears the clock and the seek range
            pane.on_track_changed(None)
            assert pane._duration_seconds == 0 and bar.total is None
            assert str(clock.render()) == "0:00 / 0:00"

    asyncio.run(scenario())


# -- UI-11: queue labels fit in terminal cells -----------------------------------


def test_queue_label_truncates_by_display_cells():
    layout = QueueSummaryLayout(column_width=12, track_count=1, show_artist=False)
    samples = [
        "界" * 20,          # wide glyphs: 2 cells each
        "abc",
        "",
        "e\u0301" * 10,     # combining accents
        "\U0001f3b5" * 10,  # emoji
        "mixed 界 text",
        "A\u200bB",         # a zero-width space
    ]
    for title in samples:
        label = queue_track_label({"title": title, "artist": ""}, layout)
        assert cell_len(label) <= 12, repr(title)

    for width in (0, 1, 2, 5, 12):
        narrow = QueueSummaryLayout(column_width=width, track_count=1, show_artist=False)
        label = queue_track_label({"title": "界界界"}, narrow)
        assert cell_len(label) <= width
    assert queue_track_label({"title": "abc"}, QueueSummaryLayout(0, 1, False)) == ""


# -- UI-15: a pending cover never leaves the previous track's art ----------------


class _FakeImageWidget:
    image = "cover A"


class _RecordingApp:
    """Stands in for the Textual app: worker coroutines are closed unrun."""

    def __init__(self):
        self.workers = []

    def run_worker(self, coro, **kwargs):
        self.workers.append(kwargs.get("name"))
        coro.close()

    def call_from_thread(self, *args, **kwargs):
        raise AssertionError("no fetch may finish in this test")


def test_pending_cover_replaces_previous_art_with_placeholder():
    from unittest.mock import patch

    art = AlbumArt(renderer="blocks")
    art._image_widget = _FakeImageWidget()
    fake_app = _RecordingApp()

    with patch.object(AlbumArt, "app", new_callable=lambda: property(lambda self: fake_app)):
        art._cache["https://img/D"] = "cover D"
        art.show("https://img/D")
        assert art.image == "cover D"  # cached art swaps immediately

        art._image_widget.image = "cover A"
        art.show("https://img/B")
        assert art.image is None  # the old cover is gone while B downloads
        art.show("https://img/B")
        assert art.image is None
        assert fake_app.workers == ["art:https://img/B"]  # no duplicate fetch

        art.show("https://img/C")
        assert art.image is None
        assert fake_app.workers == ["art:https://img/B", "art:https://img/C"]

        # a failed download leaves the placeholder for the current track
        art._arrived("https://img/C", None)
        assert art.image is None

        # a late B must not overwrite the newer C
        art._arrived("https://img/B", "cover B")
        assert art.image is None
        art._arrived("https://img/C", "cover C")
        assert art.image == "cover C"


def test_art_disabled_fetches_nothing():
    from unittest.mock import patch

    art = AlbumArt(renderer="off")
    fake_app = _RecordingApp()
    with patch.object(AlbumArt, "app", new_callable=lambda: property(lambda self: fake_app)):
        art.show("https://img/B")
    assert art.display is False
    assert art.image is None
    assert fake_app.workers == []
