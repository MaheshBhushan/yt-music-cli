"""Tests for the Textual TUI, driven against a stub Client (no daemon).

Following this project's convention (see tests/test_mpris.py) of running
async scenarios via `asyncio.run()` inside plain sync test functions,
rather than depending on the pytest-asyncio plugin.
"""

import asyncio
import threading

from textual.widgets import DataTable, Static

from ytm.tui.app import LyricsFetched, YTMApp
from ytm.tui.backend import BackendError as ClientError
from ytm.tui.lyrics import (
    ACTIVE_MARKER,
    ACTIVE_STYLE,
    INACTIVE_STYLE,
    NO_LYRICS_TEXT,
    LyricsPane,
)
from ytm.tui.nowplaying import NowPlaying, queue_summary_layout, split_queue
from ytm.tui.playlists import PlaylistsPane
from ytm.tui.queue import QueuePane
from ytm.tui.search import SearchPane

TRACK = {
    "video_id": "abc123",
    "title": "Kaanave Kaanave",
    "artist": "Sid Sriram",
    "album": "Sarvam Thaala Mayam",
    "duration": "5:12",
    "duration_seconds": 312,
}


async def settle(pilot, delay=None):
    """Let background request workers finish, then drain the message loop."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause(delay) if delay else await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


class StubClient:
    """A fake daemon client: records every command sent, no socket at all."""

    def __init__(self):
        self.calls = []
        self._subs = []
        self.closed = False

    def on_event(self, callback):
        self._subs.append(callback)

    def push(self, event, data):
        for callback in list(self._subs):
            callback(event, data)

    def request(self, cmd, args=None):
        self.calls.append((cmd, args))
        if cmd == "search":
            return {"tracks": [TRACK]}
        if cmd == "queue_get":
            return {"tracks": [TRACK], "index": 0}
        if cmd == "volume":
            return {"volume": args["level"]}
        if cmd == "seek":
            return dict(args)
        if cmd == "shutdown":
            return {"stopping": True}
        if cmd == "playlist_list":
            return {
                "playlists": [
                    {"playlist_id": "remote-1", "title": "Liked Songs", "track_count": 412, "local": False},
                    {"playlist_id": "local-1", "title": "scratch", "track_count": 12, "local": True},
                ]
            }
        if cmd == "playlist_add":
            return {"playlist_id": args["playlist_id"], "added": len(args["video_ids"]), "track_count": 413}
        if cmd == "playlist_create":
            return {"playlist_id": "PLnew", "title": args["title"], "local": False}
        return {"paused": False, "volume": 60}

    def listen(self):
        # a real Client.listen() blocks forever; the stub just returns so
        # the app's background listener thread exits immediately
        return

    def close(self):
        self.closed = True


class RaisingClient(StubClient):
    """A stub whose `search` call raises, to exercise error rendering."""

    def request(self, cmd, args=None):
        self.calls.append((cmd, args))
        if cmd in ("search", "playlist_add"):
            raise ClientError("auth expired, run 'ytm auth'")
        return super().request(cmd, args)


async def _search(pilot, query="kaanave"):
    await pilot.click("#search-input")
    for char in query:
        await pilot.press(char)
    await pilot.press("enter")
    await settle(pilot)


def test_search_populates_table():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            assert table.row_count == 1
            assert ("search", {"query": "kaanave"}) in stub.calls

    asyncio.run(scenario())


def test_enter_in_search_input_plays_first_result():
    """Change A: submitting the search box plays the first result
    immediately, without also needing Enter on the results table."""

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            play_calls = [c for c in stub.calls if c[0] == "play"]
            assert len(play_calls) == 1
            assert play_calls[0][1]["video_id"] == "abc123"

    asyncio.run(scenario())


def test_enter_in_search_input_with_no_results_sends_no_play():
    async def scenario():
        class EmptySearchClient(StubClient):
            def request(self, cmd, args=None):
                if cmd == "search":
                    self.calls.append((cmd, args))
                    return {"tracks": []}
                return super().request(cmd, args)

        stub = EmptySearchClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            assert table.row_count == 0
            assert not any(c[0] == "play" for c in stub.calls)
            assert not app._exit

    asyncio.run(scenario())


def test_enter_on_selected_row_plays_that_row_not_the_first():
    """Enter on the results table plays the highlighted row -- distinct
    from the auto-play-first-result triggered by submitting the search
    box (Change A)."""

    TRACK_TWO = dict(TRACK, video_id="zzz999", title="something else")

    async def scenario():
        class TwoResultsClient(StubClient):
            def request(self, cmd, args=None):
                if cmd == "search":
                    self.calls.append((cmd, args))
                    return {"tracks": [TRACK, TRACK_TWO]}
                return super().request(cmd, args)

        stub = TwoResultsClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            table.focus()
            table.cursor_coordinate = (1, 0)
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)

            play_calls = [c for c in stub.calls if c[0] == "play"]
            # the first play came from submitting the search box (first
            # result); the second came from Enter on the selected row
            assert len(play_calls) == 2
            assert play_calls[0][1]["video_id"] == "abc123"
            assert play_calls[1][1]["video_id"] == "zzz999"

    asyncio.run(scenario())


def test_enqueue_key_sends_enqueue():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            table.focus()
            await settle(pilot)
            await pilot.press("q")
            await settle(pilot)
            enqueue_calls = [c for c in stub.calls if c[0] == "enqueue"]
            assert len(enqueue_calls) == 1
            assert enqueue_calls[0][1]["video_id"] == "abc123"

    asyncio.run(scenario())


def test_transport_keys_send_expected_commands():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await settle(pilot)

            await pilot.press("space")
            await settle(pilot)
            await pilot.press("n")
            await settle(pilot)
            await pilot.press("p")
            await settle(pilot)

            cmds = [c[0] for c in stub.calls]
            assert "toggle" in cmds
            assert "next" in cmds
            assert "prev" in cmds

    asyncio.run(scenario())


def test_seek_keys_send_plus_minus_five_seconds():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await settle(pilot)

            await pilot.press("left")
            await settle(pilot)
            await pilot.press("right")
            await settle(pilot)

            seeks = [c[1]["seconds"] for c in stub.calls if c[0] == "seek"]
            assert -5 in seeks
            assert 5 in seeks

    asyncio.run(scenario())


def test_volume_keys_change_volume():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await settle(pilot)
            start = app._volume

            await pilot.press("plus")
            await settle(pilot)
            assert app._volume == start + 5

            await pilot.press("minus")
            await settle(pilot)
            assert app._volume == start

    asyncio.run(scenario())


def test_position_event_moves_progress_bar_without_polling():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            calls_before = len(stub.calls)

            stub.push(
                "position",
                {"position": 42, "video_id": "abc123", "duration_seconds": 312},
            )
            await settle(pilot)

            now_playing = app.query_one(NowPlaying)
            progress_bar = now_playing.query_one("#now-playing-progress")
            assert progress_bar.progress == 42

            # push-driven: no new commands (in particular no `status` poll)
            # were sent to arrive at this update
            assert len(stub.calls) == calls_before

            # give any timers a chance to fire, then confirm nothing polled
            await settle(pilot, 0.3)
            assert len(stub.calls) == calls_before
            # one status fetch at startup (to seed the volume indicator) is
            # fine; a poll would keep sending more of them over time
            status_calls = [c for c in stub.calls if c[0] == "status"]
            assert len(status_calls) <= 1

    asyncio.run(scenario())


def test_e_uses_central_cleanup():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await settle(pilot)
            await pilot.press("e")
            await settle(pilot)
        assert not any(c[0] == "shutdown" for c in stub.calls)
        assert stub.closed

    asyncio.run(scenario())


def test_x_uses_the_same_central_cleanup():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await settle(pilot)
            await pilot.press("x")
            await settle(pilot)
        assert not any(c[0] == "shutdown" for c in stub.calls)
        assert stub.closed

    asyncio.run(scenario())


def test_client_error_renders_visibly_and_does_not_crash():
    async def scenario():
        stub = RaisingClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            banner = app.query_one("#error-banner")
            assert "auth expired" in str(banner.render())
            # the app is still alive and usable
            assert not app._exit

    asyncio.run(scenario())


def test_client_error_on_construction_renders_and_does_not_crash():
    async def scenario():
        class FailingClient:
            def __init__(self, *a, **k):
                raise ClientError("cannot reach the ytm daemon")

        # YTMApp catches a ClientError raised while constructing its own
        # Client() when none is injected; simulate that by monkeypatching
        # the Client symbol app.py resolves at construction time.
        import ytm.tui.app as app_module

        original = app_module.Backend
        app_module.Backend = FailingClient
        try:
            app = YTMApp(client=None)
        finally:
            app_module.Backend = original

        async with app.run_test() as pilot:
            await settle(pilot)
            banner = app.query_one("#error-banner")
            assert "cannot reach the ytm daemon" in str(banner.render())

    asyncio.run(scenario())


def test_playlists_pane_renders_local_and_remote_markers():
    from textual.widgets import DataTable as _DT

    from ytm.tui.playlists import PlaylistsPane

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(PlaylistsPane)
            table = pane.query_one("#playlists-table", _DT)
            assert table.row_count == 3  # two playlists plus "+ new playlist"
            rows = [
                tuple(table.get_row_at(i)) for i in range(table.row_count)
            ]
            assert any("(remote)" in r for r in rows)
            assert any("(local)" in r for r in rows)
            assert ("playlist_list", None) in stub.calls

    asyncio.run(scenario())


def test_add_to_playlist_key_sends_playlist_add():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            table.focus()
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
            assert len(add_calls) == 1
            assert add_calls[0][1]["video_ids"] == ["abc123"]

    asyncio.run(scenario())


def test_client_error_on_playlist_action_shows_banner_not_crash():
    async def scenario():
        stub = RaisingClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app._request("playlist_add", {"playlist_id": "x", "video_ids": ["y"]})
            await settle(pilot)
            banner = app.query_one("#error-banner")
            assert "auth expired" in str(banner.render())

    asyncio.run(scenario())


def test_smoke_render_layout(tmp_path):
    """Headless smoke run: the app starts up and lays out all panes,
    including the lyrics pane (Change B), at two terminal sizes."""

    async def scenario(size):
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            assert app.query_one(SearchPane) is not None
            assert app.query_one(QueuePane) is not None
            assert app.query_one(NowPlaying) is not None
            assert app.query_one("#playlists-pane") is not None
            assert app.query_one(LyricsPane) is not None
            svg = app.export_screenshot()
            assert "<svg" in svg
            assert len(svg) > 1000
            return svg

    svg_100x30 = asyncio.run(scenario((100, 30)))
    with open(tmp_path / "ytm_tui_smoke_100x30.svg", "w", encoding="utf-8") as fh:
        fh.write(svg_100x30)
    print(f"Smoke screenshot written to /tmp/ytm_tui_smoke_100x30.svg ({len(svg_100x30)} bytes)")

    svg_120x40 = asyncio.run(scenario((120, 40)))
    with open(tmp_path / "ytm_tui_smoke_120x40.svg", "w", encoding="utf-8") as fh:
        fh.write(svg_120x40)
    print(f"Smoke screenshot written to /tmp/ytm_tui_smoke_120x40.svg ({len(svg_120x40)} bytes)")


# -- lyrics pane (Change B) -------------------------------------------------


class LyricsStubClient(StubClient):
    """A stub whose `lyrics` command is scriptable per test."""

    def __init__(self, lyrics_response=None, lyrics_error=None, on_lyrics=None):
        super().__init__()
        self._lyrics_response = lyrics_response
        self._lyrics_error = lyrics_error
        self._on_lyrics = on_lyrics

    def request(self, cmd, args=None):
        if cmd == "lyrics":
            self.calls.append((cmd, args))
            if self._on_lyrics is not None:
                self._on_lyrics(args)
            if self._lyrics_error is not None:
                raise ClientError(self._lyrics_error)
            return self._lyrics_response
        return super().request(cmd, args)


def test_track_changed_fetches_and_renders_lyrics():
    async def scenario():
        stub = LyricsStubClient(
            lyrics_response={
                "video_id": "abc123",
                "lyrics": "la la la\nsecond line",
                "source": "test",
            }
        )
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            stub.push("track_changed", {"video_id": "abc123", "title": "Song"})
            await settle(pilot)

            lyrics_calls = [c for c in stub.calls if c[0] == "lyrics"]
            assert len(lyrics_calls) == 1
            assert lyrics_calls[0][1] == {"video_id": "abc123"}

            content = app.query_one("#lyrics-content")
            assert "la la la" in str(content.render())

    asyncio.run(scenario())


def test_track_changed_with_null_lyrics_renders_no_lyrics_available():
    async def scenario():
        stub = LyricsStubClient(
            lyrics_response={"video_id": "abc123", "lyrics": None, "source": None}
        )
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            stub.push("track_changed", {"video_id": "abc123", "title": "Song"})
            await settle(pilot)

            content = app.query_one("#lyrics-content")
            assert str(content.render()) == NO_LYRICS_TEXT

    asyncio.run(scenario())


def test_lyrics_client_error_renders_message_without_crashing():
    async def scenario():
        stub = LyricsStubClient(lyrics_error="lyrics service unavailable")
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            stub.push("track_changed", {"video_id": "abc123", "title": "Song"})
            await settle(pilot)

            content = app.query_one("#lyrics-content")
            assert "lyrics service unavailable" in str(content.render())
            assert not app._exit

    asyncio.run(scenario())


def test_slow_lyrics_fetch_does_not_block_ui():
    """The lyrics fetch runs on a background thread; the app keeps
    processing input (e.g. volume keys) while a slow request is in
    flight."""

    release = threading.Event()

    def slow_lyrics(_args):
        # block the *lyrics* worker thread only -- the UI/event loop must
        # remain free to handle other input while this is stuck
        release.wait(timeout=5)

    async def scenario():
        stub = LyricsStubClient(
            lyrics_response={"video_id": "abc123", "lyrics": "slow", "source": None},
            on_lyrics=slow_lyrics,
        )
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table", DataTable).focus()
            await settle(pilot)

            stub.push("track_changed", {"video_id": "abc123", "title": "Song"})
            await settle(pilot)

            # the lyrics request is now blocked on `release`; the app must
            # still respond to a keypress while it waits
            start_volume = app._volume
            await pilot.press("plus")
            await settle(pilot)
            assert app._volume == start_volume + 5

            lyrics_calls = [c for c in stub.calls if c[0] == "lyrics"]
            assert len(lyrics_calls) == 1

            content = app.query_one("#lyrics-content")
            # still showing nothing/old content -- the slow fetch hasn't
            # resolved yet, proving it didn't block to get here
            assert "slow" not in str(content.render())

            release.set()
            await pilot.pause(0.2)
            assert "slow" in str(content.render())

    asyncio.run(scenario())


# -- config-driven keybindings and themes ----------------------------------


def _config_with_keys(**overrides):
    keys = {"toggle": "space", "next": "n", "prev": "p", "search": "/", "quit": "q"}
    keys.update(overrides)
    return {
        "audio": {"control": "system", "volume": 70, "device": "auto"},
        "behaviour": {"autoplay_radio": True, "confirm_remote_delete": True},
        "ui": {"theme": "dark"},
        "keys": keys,
    }


def test_custom_toggle_key_is_the_key_actually_bound():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub, config=_config_with_keys(toggle="x"))
        async with app.run_test() as pilot:
            await settle(pilot)
            # move focus off the search input, which otherwise swallows
            # printable keys before they reach the app's bindings
            app.query_one("#queue-table", DataTable).focus()
            await settle(pilot)
            # the configured key fires the action
            await pilot.press("x")
            await settle(pilot)
            assert ("toggle", None) in stub.calls
            # the old default no longer triggers it
            await pilot.press("space")
            await settle(pilot)
            toggle_calls_after = [c for c in stub.calls if c[0] == "toggle"]
            assert len(toggle_calls_after) == 1

    asyncio.run(scenario())


def test_dark_and_light_theme_resolve_to_different_styles():
    async def scenario():
        stub = StubClient()
        dark_app = YTMApp(client=stub, config=_config_with_keys())
        async with dark_app.run_test() as pilot:
            await settle(pilot)
            dark_primary = dark_app.get_theme(dark_app.theme).primary

        light_config = _config_with_keys()
        light_config["ui"]["theme"] = "light"
        light_stub = StubClient()
        light_app = YTMApp(client=light_stub, config=light_config)
        async with light_app.run_test() as pilot:
            await settle(pilot)
            light_primary = light_app.get_theme(light_app.theme).primary

        assert dark_app.theme == "textual-dark"
        assert light_app.theme == "textual-light"
        assert dark_primary != light_primary

    asyncio.run(scenario())


def _dup_track(video_id, title):
    return {
        "video_id": video_id,
        "title": title,
        "artist": "Sid Sriram",
        "album": "Sarvam Thaala Mayam",
        "duration": "5:12",
        "duration_seconds": 312,
    }


class DupQueueClient(StubClient):
    """A stub whose queue contains the same video_id at two positions."""

    def request(self, cmd, args=None):
        self.calls.append((cmd, args))
        if cmd == "queue_get":
            return {
                "tracks": [
                    _dup_track("abc123", "first play"),
                    _dup_track("zzz999", "something else"),
                    _dup_track("abc123", "replayed"),
                ],
                "index": 0,
            }
        return super().request(cmd, args)


def test_queue_with_duplicate_video_id_renders_without_crashing():
    async def scenario():
        stub = DupQueueClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#queue-table", DataTable)
            assert table.row_count == 3
            svg = app.export_screenshot()
            assert svg

    asyncio.run(scenario())


def test_selecting_second_duplicate_queue_row_resolves_correct_track():
    async def scenario():
        stub = DupQueueClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(QueuePane)
            table = app.query_one("#queue-table", DataTable)
            table.focus()

            table.cursor_coordinate = (0, 0)
            await settle(pilot)
            first = pane.selected_track()
            assert first["video_id"] == "abc123"
            assert first["title"] == "first play"

            table.cursor_coordinate = (2, 0)
            await settle(pilot)
            second = pane.selected_track()
            assert second["video_id"] == "abc123"
            assert second["title"] == "replayed"

    asyncio.run(scenario())


class DupSearchClient(StubClient):
    """A stub whose search results repeat a video_id at two positions."""

    def request(self, cmd, args=None):
        self.calls.append((cmd, args))
        if cmd == "search":
            return {
                "tracks": [
                    _dup_track("abc123", "first result"),
                    _dup_track("abc123", "second result"),
                ]
            }
        return super().request(cmd, args)


def test_search_results_with_duplicate_video_id_renders_without_crashing():
    async def scenario():
        stub = DupSearchClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            assert table.row_count == 2

    asyncio.run(scenario())


def test_long_title_is_truncated_to_the_title_column_width():
    """A very long title must not push the other columns off screen: it
    should be truncated to the TITLE column's width with an ellipsis."""

    LONG_TITLE = "x" * 200

    async def scenario():
        class LongTitleClient(StubClient):
            def request(self, cmd, args=None):
                if cmd == "search":
                    self.calls.append((cmd, args))
                    return {"tracks": [dict(TRACK, title=LONG_TITLE)]}
                return super().request(cmd, args)

        stub = LongTitleClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            title_width = table.columns["TITLE"].width
            rendered = str(table.get_cell_at((0, 0)))
            assert len(rendered) <= title_width
            assert rendered.endswith("…")

    asyncio.run(scenario())


# -- s / e shortcuts ------------------------------------------------------------


def test_s_focuses_search_from_another_pane():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app.query_one("#queue-table").focus()
            await settle(pilot)
            assert app.focused.id != "search-input"
            await pilot.press("s")
            await settle(pilot)
            assert app.focused.id == "search-input"

    asyncio.run(scenario())


def test_h_hides_the_search_pane_and_s_brings_it_back():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            pane = app.query_one("#search-pane")
            await pilot.click("#search-input")
            await pilot.press("h")  # typing: `h` is a letter inside the box
            await settle(pilot)
            assert pane.display
            assert app.query_one("#search-input").value == "h"
            app.query_one("#queue-table").focus()
            await settle(pilot)
            await pilot.press("h")
            await settle(pilot)
            assert not pane.display
            assert app.focused.id == "queue-table"
            await pilot.press("s")
            await settle(pilot)
            assert pane.display
            assert app.focused.id == "search-input"
            app.query_one("#queue-table").focus()
            await pilot.press("h")
            await settle(pilot)
            assert not pane.display
            await pilot.press("h")  # toggles back too
            await settle(pilot)
            assert pane.display

    asyncio.run(scenario())


def test_hiding_the_search_pane_moves_focus_off_it():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.query_one("#search-results").focus()
            await settle(pilot)
            await pilot.press("h")
            await settle(pilot)
            assert not app.query_one("#search-pane").display
            assert app.focused.id == "queue-table"
            await pilot.press("tab")  # the focus chain skips the hidden pane
            await settle(pilot)
            assert app.focused.id != "search-input"

    asyncio.run(scenario())


def test_e_exits_without_stopping_playback():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app.query_one("#queue-table").focus()
            await settle(pilot)
            await pilot.press("e")
            await settle(pilot)
        assert stub.closed
        assert not any(c[0] == "shutdown" for c in stub.calls)

    asyncio.run(scenario())


def test_s_and_e_are_plain_letters_inside_the_search_box():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.click("#search-input")
            for char in "sesame":
                await pilot.press(char)
            await settle(pilot)
            assert app.query_one("#search-input").value == "sesame"
            assert app.is_running

    asyncio.run(scenario())


# -- album art --------------------------------------------------------------------------


def _png_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_track_changed_fetches_cover_art_off_the_ui_thread_and_shows_it():
    from ytm.tui.nowplaying import AlbumArt

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        fetched = []

        async with app.run_test() as pilot:
            await settle(pilot)
            art = app.query_one(AlbumArt)
            art._fetcher = lambda url: (fetched.append(url), _png_bytes())[1]
            stub.push("track_changed", dict(TRACK, thumbnail="https://img.test/cover.jpg"))
            await settle(pilot)
            assert fetched == ["https://img.test/cover.jpg"]
            assert art.image is not None
            # the same cover again comes from the cache, no second fetch
            stub.push("track_changed", dict(TRACK, thumbnail="https://img.test/cover.jpg"))
            await settle(pilot)
            assert fetched == ["https://img.test/cover.jpg"]
            # the artist / album line is filled from the event
            line = app.query_one("#now-playing-artist").render()
            assert "Ilaiyaraaja" in str(line) or str(line) != ""

    asyncio.run(scenario())


def test_failed_cover_fetch_is_dropped_quietly():
    from ytm.tui.nowplaying import AlbumArt

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)

        def boom(url):
            raise OSError("no network")

        async with app.run_test() as pilot:
            await settle(pilot)
            app.query_one(AlbumArt)._fetcher = boom
            stub.push("track_changed", dict(TRACK, thumbnail="https://img.test/x.jpg"))
            await settle(pilot)
            assert app.query_one(AlbumArt).image is None
            assert app.is_running

    asyncio.run(scenario())


def test_art_off_hides_the_pane_and_fetches_nothing():
    from ytm import config as config_mod
    from ytm.tui.nowplaying import AlbumArt

    async def scenario():
        stub = StubClient()
        cfg = config_mod.load("/nonexistent")
        cfg["ui"]["art"] = "off"
        app = YTMApp(client=stub, config=cfg)
        async with app.run_test() as pilot:
            await settle(pilot)
            art = app.query_one(AlbumArt)
            art._fetcher = lambda url: (_ for _ in ()).throw(AssertionError("must not fetch"))
            stub.push("track_changed", dict(TRACK, thumbnail="https://img.test/cover.jpg"))
            await settle(pilot)
            assert art.display is False and art.image is None

    asyncio.run(scenario())


# -- mouse and focus ------------------------------------------------------------------


def test_enter_in_the_search_box_hands_focus_to_the_results_so_space_toggles():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.click("#search-input")
            for char in "am":
                await pilot.press(char)
            await pilot.press("enter")
            await settle(pilot)
            assert app.focused.id == "search-results"
            await pilot.press("space")
            await settle(pilot)
            assert ("toggle", None) in stub.calls
            assert app.query_one("#search-input").value == "am"

    asyncio.run(scenario())


def test_arrow_keys_move_the_text_cursor_inside_the_search_box():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.click("#search-input")
            for char in "ab":
                await pilot.press(char)
            await pilot.press("left")
            await pilot.press("x")
            await settle(pilot)
            assert app.query_one("#search-input").value == "axb"
            assert not any(c[0] == "seek" for c in stub.calls)
            # escape leaves the box; now the arrows seek
            await pilot.press("escape")
            await pilot.press("right")
            await settle(pilot)
            assert app.focused.id == "search-results"
            assert ("seek", {"seconds": 5}) in stub.calls

    asyncio.run(scenario())


def test_clicking_a_queue_row_jumps_to_it():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            queue = app.query_one(QueuePane)
            queue.set_queue({"tracks": [TRACK, dict(TRACK, video_id="second", title="Second")], "index": 0})
            await settle(pilot)
            await pilot.click("#queue-table", offset=(2, 1))
            await settle(pilot)
            assert [call for call in stub.calls if call[0] == "queue_play"] == [("queue_play", {"index": 1})]

    asyncio.run(scenario())


def test_clicking_a_playlist_plays_it():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.click("#playlists-table", offset=(2, 1))
            await settle(pilot)
            assert [call for call in stub.calls if call[0] == "playlist_play"] == [("playlist_play", {"playlist_id": "local-1"})]
            # Clicking the highlighted row again and keyboard Enter must each
            # trigger exactly one more request, not be swallowed or doubled.
            await pilot.click("#playlists-table", offset=(2, 1))
            await settle(pilot)
            assert len([call for call in stub.calls if call[0] == "playlist_play"]) == 2
            await pilot.press("enter")
            await settle(pilot)
            assert len([call for call in stub.calls if call[0] == "playlist_play"]) == 3

    asyncio.run(scenario())


def test_clicking_the_progress_bar_seeks_there():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            stub.push("position", {"position": 10, "video_id": "abc123", "duration_seconds": 200})
            await settle(pilot)
            bar = app.query_one("#now-playing-progress")
            assert bar.region.width > 0
            await pilot.click("#now-playing-progress", offset=(bar.region.width // 2, 0))
            await settle(pilot)
            seeks = [args for cmd, args in stub.calls if cmd == "seek"]
            assert seeks and seeks[-1]["absolute"] is True
            assert 90 <= seeks[-1]["seconds"] <= 110

    asyncio.run(scenario())


def test_clicking_the_shortcut_bar_runs_the_action():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("escape")  # drop the "leave search" lead-in shown while typing
            await settle(pilot)
            # "e exit  / s search  space play/pause": `space` starts at column 20,
            # plus the bar's one cell of padding
            await pilot.click("#shortcut-bar", offset=(22, 0))
            await settle(pilot)
            assert ("toggle", None) in stub.calls

    asyncio.run(scenario())


def test_error_banner_only_takes_a_row_while_there_is_an_error():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            banner = app.query_one("#error-banner")
            assert banner.display is False
            app._show_error("boom")
            await settle(pilot)
            assert banner.display is True
            app._clear_error()
            assert banner.display is False

    asyncio.run(scenario())


def test_now_playing_text_sits_at_the_bottom_of_the_strip():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            strip = app.query_one("#now-playing").region
            bar = app.query_one("#now-playing-bar").region
            shortcut = app.query_one("#shortcut-bar").region
            assert bar.y + bar.height == strip.y + strip.height
            assert strip.y + strip.height == shortcut.y

    asyncio.run(scenario())


def test_startup_focuses_the_queue_when_something_is_already_loaded():
    class Playing(StubClient):
        def request(self, cmd, args=None):
            if cmd == "status":
                self.calls.append((cmd, args))
                return {"current": TRACK, "paused": True, "volume": 70, "index": 0, "count": 1, "position": 3.0}
            return super().request(cmd, args)

    async def scenario():
        stub = Playing()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.focused.id == "queue-table"
            await pilot.press("space")
            await settle(pilot)
            assert ("toggle", None) in stub.calls

    asyncio.run(scenario())


def test_listener_drop_shows_a_banner_and_reconnects():
    class Flaky(StubClient):
        def __init__(self):
            super().__init__()
            self.listens = 0
            self._closed = False

        def listen(self):
            self.listens += 1
            if self.listens == 1:
                raise ClientError("lost the connection to mpv")

        def close(self):
            self._closed = True
            super().close()

    async def scenario():
        stub = Flaky()
        app = YTMApp(client=stub)
        app.LISTEN_RETRY = 0.05
        shown = []
        # startup requests clear the banner again, so record instead
        app._show_error = lambda message, owner=None: shown.append(message)
        async with app.run_test() as pilot:
            await settle(pilot, 0.3)
            assert stub.listens == 2
            assert any("player events lost" in m for m in shown)

    asyncio.run(scenario())


# -- playlists: add from the queue, create new ----------------------------------------


def test_add_to_playlist_takes_the_highlighted_queue_row():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app.query_one(QueuePane).set_queue(
                {"tracks": [TRACK, dict(TRACK, video_id="q2", title="Second")], "index": 0})
            await settle(pilot)
            queue = app.query_one("#queue-table", DataTable)
            queue.focus()
            await pilot.press("down")  # highlight "Second"
            await pilot.press("l")     # move to playlists, cursor on the first one
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            adds = [c for c in stub.calls if c[0] == "playlist_add"]
            assert adds and adds[0][1]["video_ids"] == ["q2"]
            assert adds[0][1]["playlist_id"] == "remote-1"

    asyncio.run(scenario())


def test_add_to_playlist_falls_back_to_the_playing_track():
    class Playing(StubClient):
        def request(self, cmd, args=None):
            if cmd == "status":
                self.calls.append((cmd, args))
                return {"current": TRACK, "paused": False, "volume": 70, "index": 0, "count": 1, "position": 3.0}
            return super().request(cmd, args)

    async def scenario():
        stub = Playing()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            adds = [c for c in stub.calls if c[0] == "playlist_add"]
            assert adds and adds[0][1]["video_ids"] == ["abc123"]

    asyncio.run(scenario())


def test_new_playlist_row_prompts_for_a_name_and_creates_it():
    from textual.widgets import Input as _Input

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#playlists-table", DataTable)
            table.focus()
            table.move_cursor(row=table.row_count - 1)  # the "+ new playlist" row is last
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            box = app.query_one("#playlist-name", _Input)
            assert box.display is True and app.focused is box
            for ch in "Road Trip":
                await pilot.press(ch if ch != " " else "space")
            await pilot.press("enter")
            await settle(pilot)
            assert ("playlist_create", {"title": "Road Trip"}) in stub.calls
            assert box.display is False
            # the list was refreshed afterwards
            assert [c[0] for c in stub.calls].count("playlist_list") >= 2

    asyncio.run(scenario())


def test_escape_cancels_the_new_playlist_prompt():
    from textual.widgets import Input as _Input

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app.query_one(PlaylistsPane).prompt_new()
            await settle(pilot)
            await pilot.press("escape")
            await settle(pilot)
            assert app.query_one("#playlist-name", _Input).display is False
            assert not any(c[0] == "playlist_create" for c in stub.calls)

    asyncio.run(scenario())


def test_add_to_playlist_updates_the_count_and_keeps_the_cursor():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            app.query_one("#search-results", DataTable).focus()
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            table = app.query_one("#playlists-table", DataTable)
            table.move_cursor(row=1)  # "scratch", the local one
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
            assert add_calls[0][1]["playlist_id"] == "local-1"
            assert table.cursor_row == 1
            assert app.query_one(PlaylistsPane).selected_playlist_id() == "local-1"

    asyncio.run(scenario())


def test_playlists_pane_set_count_updates_one_row():
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(PlaylistsPane)
            pane.set_count("remote-1", 413)
            table = app.query_one("#playlists-table", DataTable)
            assert str(table.get_cell("remote-1", "count")) == "413"
            pane.set_count("nope", 1)  # unknown id is ignored, not an error
            pane.set_count("remote-1", None)
            assert str(table.get_cell("remote-1", "count")) == "413"

    asyncio.run(scenario())


def _queue(n, index):
    tracks = [dict(TRACK, video_id=f"q{i}", title=f"Song {i}") for i in range(n)]
    return {"tracks": tracks, "index": index}


def test_split_queue_at_first_track_has_nothing_played():
    tracks = _queue(5, 0)["tracks"]
    played, up_next = split_queue(tracks, 0)
    assert played == []
    assert [t["title"] for t in up_next] == ["Song 1", "Song 2", "Song 3"]


def test_split_queue_at_last_track_has_nothing_up_next():
    tracks = _queue(5, 4)["tracks"]
    played, up_next = split_queue(tracks, 4)
    assert [t["title"] for t in played] == ["Song 2", "Song 3"]
    assert up_next == []


def test_split_queue_on_an_empty_queue():
    assert split_queue([], None) == ([], [])
    assert split_queue([], 0) == ([], [])


def test_split_queue_in_the_middle():
    tracks = _queue(6, 3)["tracks"]
    played, up_next = split_queue(tracks, 3)
    assert [t["title"] for t in played] == ["Song 1", "Song 2"]
    assert [t["title"] for t in up_next] == ["Song 4", "Song 5"]


def test_queue_summary_layout_tightens_and_expands():
    very_narrow = queue_summary_layout(width=18, height=4)
    assert very_narrow.column_width == 9

    tight = queue_summary_layout(width=40, height=4)
    assert tight.column_width == 20
    assert tight.track_count == 1
    assert tight.show_artist is False

    roomy = queue_summary_layout(width=100, height=10)
    assert roomy.column_width == 40  # capped so the two columns stay together
    assert roomy.track_count == 6
    assert roomy.show_artist is True


def test_queue_summary_layout_uses_configurable_max_width():
    capped = queue_summary_layout(width=160, height=8, max_width=24)
    assert capped.column_width == 24

    uncapped = queue_summary_layout(width=160, height=8, max_width=0)
    assert uncapped.column_width == 80


def test_now_playing_queue_uses_space_for_artist_details():
    track = dict(TRACK, title="Long Way Home", artist="Norah Jones")

    tight = NowPlaying._render_column("UP NEXT", [track], queue_summary_layout(40, 4))
    assert tight == "UP NEXT\nLong Way Home"

    roomy = NowPlaying._render_column("UP NEXT", [track], queue_summary_layout(100, 8))
    assert roomy == "UP NEXT\nLong Way Home — Norah Jones"


def test_now_playing_queue_rerenders_after_terminal_resize():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 30)) as pilot:
            await settle(pilot)
            now_playing = app.query_one(NowPlaying)
            now_playing.set_queue(_queue(8, 3))
            await pilot.pause()

            up_next = app.query_one("#now-playing-upnext", Static)
            assert "Song 4 — Sid Sriram" in up_next.content

            await pilot.resize_terminal(80, 20)
            await pilot.pause()

            assert up_next.content == "UP NEXT\nSong 4"

    asyncio.run(scenario())


def test_queue_cursor_follows_the_playing_track_across_refreshes():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(QueuePane)
            table = app.query_one("#queue-table", DataTable)
            pane.set_queue(_queue(5, 2))
            assert table.cursor_row == 2 and pane.selected_track()["title"] == "Song 2"
            # the track advances: a refresh must not park the cursor on row 0
            pane.set_queue(_queue(5, 3))
            assert table.cursor_row == 3 and pane.selected_track()["title"] == "Song 3"

    asyncio.run(scenario())


def test_queue_cursor_moved_by_the_user_stays_put_on_refresh():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(QueuePane)
            table = app.query_one("#queue-table", DataTable)
            pane.set_queue(_queue(5, 1))
            table.move_cursor(row=4)
            pane.set_queue(_queue(5, 2))  # playback moved on, the user's pick did not
            assert table.cursor_row == 4 and pane.selected_track()["title"] == "Song 4"

    asyncio.run(scenario())


def test_add_to_playlist_after_a_search_takes_the_search_result_not_the_queue():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            # a queue with a different song highlighted, as after startup
            app.query_one(QueuePane).set_queue(_queue(3, 0))
            await _search(pilot)  # Enter plays the first result and refreshes the queue
            await settle(pilot)
            assert isinstance(app.focused, DataTable) and app.focused.id == "search-results"
            await pilot.press("l")
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
            assert add_calls[-1][1]["video_ids"] == [TRACK["video_id"]]

    asyncio.run(scenario())


def test_add_flow_arm_song_pick_playlist_confirm():
    """A on a song → playlists pane with the song shown, ↓ to pick, A to add,
    focus comes back to where the user was."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await _search(pilot)
            results = app.query_one("#search-results", DataTable)
            results.focus()
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            assert app.focused.id == "playlists-table"
            header = str(app.query_one("#playlists-title").render())
            assert "adding" in header and TRACK["title"] in header
            assert not [c for c in stub.calls if c[0] == "playlist_add"]  # nothing sent yet
            await pilot.press("down")  # "scratch"
            await pilot.press("a")
            await settle(pilot)
            add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
            assert len(add_calls) == 1
            assert add_calls[0][1]["playlist_id"] == "local-1"
            assert add_calls[0][1]["video_ids"] == [TRACK["video_id"]]
            assert str(app.query_one("#playlists-title").render()) == "PLAYLISTS"
            assert app.focused is results

    asyncio.run(scenario())


def test_add_flow_enter_also_confirms():
    async def scenario():
        for confirm in ("enter",):
            stub = StubClient()
            app = YTMApp(client=stub)
            async with app.run_test() as pilot:
                await _search(pilot)
                app.query_one("#search-results", DataTable).focus()
                await settle(pilot)
                await pilot.press("a")
                await settle(pilot)
                await pilot.press(confirm)
                await settle(pilot)
                add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
                assert len(add_calls) == 1, confirm
                assert add_calls[0][1]["playlist_id"] == "remote-1"
                # Enter with a song armed must not start playing the playlist
                assert not [c for c in stub.calls if c[0] == "playlist_play"]

    asyncio.run(scenario())


def test_add_flow_escape_cancels_and_returns_focus():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await _search(pilot)
            results = app.query_one("#search-results", DataTable)
            results.focus()
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            await pilot.press("escape")
            await settle(pilot)
            assert app.focused is results
            assert str(app.query_one("#playlists-title").render()) == "PLAYLISTS"
            await pilot.press("l")
            await pilot.press("enter")  # with nothing armed, Enter plays the playlist again
            await settle(pilot)
            assert not [c for c in stub.calls if c[0] == "playlist_add"]
            assert [c for c in stub.calls if c[0] == "playlist_play"]

    asyncio.run(scenario())


def test_live_search_shows_results_without_enter():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await pilot.click("#search-input")
            for char in "kaanave":
                await pilot.press(char)
            await settle(pilot, delay=app.SEARCH_DEBOUNCE + 0.2)
            searches = [c for c in stub.calls if c[0] == "search"]
            assert len(searches) == 1  # one search for the whole burst, not one per key
            assert searches[0][1]["query"] == "kaanave"
            table = app.query_one("#search-results", DataTable)
            assert table.row_count == 1
            # results only: nothing starts playing until Enter
            assert not [c for c in stub.calls if c[0] == "play"]
            assert isinstance(app.focused, type(app.query_one("#search-input")))

    asyncio.run(scenario())


def test_live_search_waits_for_two_characters():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await pilot.click("#search-input")
            await pilot.press("k")
            await settle(pilot, delay=app.SEARCH_DEBOUNCE + 0.2)
            assert not [c for c in stub.calls if c[0] == "search"]

    asyncio.run(scenario())


def test_enter_after_live_results_plays_without_searching_again():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#search-input")
            for char in "kaanave":
                await pilot.press(char)
            await settle(pilot, delay=app.SEARCH_DEBOUNCE + 0.2)
            await pilot.press("enter")
            await settle(pilot)
            assert len([c for c in stub.calls if c[0] == "search"]) == 1
            plays = [c for c in stub.calls if c[0] == "play"]
            assert plays and plays[-1][1]["video_id"] == TRACK["video_id"]
            assert app.focused.id == "search-results"

    asyncio.run(scenario())


def test_stale_live_search_reply_cannot_overwrite_newer_results():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app._search_generation = 5
            app._search_query = "new"
            newer = [dict(TRACK, title="Newer")]
            app._show_search_results(5, "new", {"tracks": newer})
            app._show_search_results(4, "old", {"tracks": [dict(TRACK, title="Older")] * 3})
            pane = app.query_one(SearchPane)
            assert [t["title"] for t in pane._tracks] == ["Newer"]
            assert app._results_query == "new"

    asyncio.run(scenario())


def test_tui_toasts_when_a_newer_version_exists(monkeypatch):
    from ytm import update as update_mod

    monkeypatch.setattr(update_mod, "check", lambda **k: {
        "installed": "0.2.0", "latest": "0.3.0", "newer": True, "checked_at": 0, "cached": False})
    upgraded = []
    monkeypatch.setattr(update_mod, "upgrade", lambda **k: (upgraded.append(1), (True, ""))[1])

    async def scenario():
        app = YTMApp(client=StubClient())
        toasts = []
        monkeypatch.setattr(app, "notify", lambda message, **kw: toasts.append((kw.get("title"), message)))
        async with app.run_test() as pilot:
            await settle(pilot, delay=0.3)
        assert any("0.3.0" in m and "ytm update" in m for _, m in toasts), toasts
        assert not upgraded  # auto is off by default: tell, don't install

    asyncio.run(scenario())


def test_tui_auto_update_runs_the_upgrade(monkeypatch):
    from ytm import config as config_mod
    from ytm import update as update_mod

    monkeypatch.setattr(update_mod, "check", lambda **k: {
        "installed": "0.2.0", "latest": "0.3.0", "newer": True, "checked_at": 0, "cached": False})
    upgraded = []
    monkeypatch.setattr(update_mod, "upgrade", lambda **k: (upgraded.append(1), (True, "ok"))[1])
    cfg = config_mod.load("/nonexistent/config.toml")
    cfg["update"]["auto"] = True

    async def scenario():
        app = YTMApp(client=StubClient(), config=cfg)
        toasts = []
        monkeypatch.setattr(app, "notify", lambda message, **kw: toasts.append(message))
        async with app.run_test() as pilot:
            await settle(pilot, delay=0.3)
        assert upgraded and any("Updated ytm to 0.3.0" in m for m in toasts), toasts

    asyncio.run(scenario())


def test_play_next_key_sends_enqueue_next():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            app.query_one("#search-results", DataTable).focus()
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            calls = [c for c in stub.calls if c[0] == "enqueue_next"]
            assert len(calls) == 1 and calls[0][1]["video_id"] == "abc123"
            assert "play next" in str(app.query_one("#shortcut-bar").render())

    asyncio.run(scenario())


def test_volume_keys_work_while_the_search_box_has_focus():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#search-input").focus()
            await settle(pilot)
            start = app._volume
            await pilot.press("plus")
            await settle(pilot)
            assert app._volume == start + 5
            assert app.query_one("#search-input").value == ""

    asyncio.run(scenario())


def test_volume_label_follows_the_up_next_column_on_the_heading_row():
    """The volume is read right after the PLAYED / UP NEXT columns, not
    squeezed after the clock on the progress row nor off at the far edge."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("state_changed", {"paused": False, "volume": 55})
            await settle(pilot)
            label = app.query_one("#now-playing-volume")
            upnext = app.query_one("#now-playing-upnext")
            bar = app.query_one("#now-playing-bar")
            assert str(label.render()) == "vol 55"
            assert label.region.y == upnext.region.y  # the heading row
            assert label.region.x == upnext.region.right + 2  # right after the column
            assert label.region.y < bar.region.y

    asyncio.run(scenario())


def test_equals_key_raises_the_volume_like_plus():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await pilot.click("#search-input")
            await settle(pilot)
            start = app._volume
            await pilot.press("equals_sign")
            await settle(pilot)
            assert app._volume == start + 5
            assert app.query_one("#search-input").value == ""

    asyncio.run(scenario())


def test_playlist_count_after_an_add_never_drops_below_what_was_shown():
    """YouTube's count lags the add by a few seconds; the row must not
    show 412 after adding to a list that showed 412."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(PlaylistsPane)
            table = app.query_one("#playlists-table", DataTable)
            pane.set_count("remote-1", 412, added=1)  # stale count from YouTube
            assert str(table.get_cell("remote-1", "count")) == "413"
            pane.set_count("remote-1", 420, added=1)  # a fresh, larger count wins
            assert str(table.get_cell("remote-1", "count")) == "420"
            pane.set_count("remote-1", None, added=1)  # count lookup failed
            assert str(table.get_cell("remote-1", "count")) == "421"
            pane.set_count("nope", 5, added=1)  # unknown playlist: no crash

    asyncio.run(scenario())


def test_queue_summary_refresh_before_the_pane_has_a_size_does_not_crash():
    """on_resize can fire before layout gives the strip a width; the label
    reservation must not subtract from None (TypeError seen in 0.5.14)."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(NowPlaying)
            pane._refresh_queue_summary(width=None, height=0)
            pane._refresh_queue_summary(width=0, height=None)

    asyncio.run(scenario())


def test_adding_a_track_does_not_re_list_every_playlist():
    """The row's count is updated in place. Re-listing the library -- and a
    track count for each playlist in it -- to learn one number the add
    already reported is a round of requests for nothing."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            app.query_one("#search-results", DataTable).focus()
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            listings_before = len([c for c in stub.calls if c[0] == "playlist_list"])
            await pilot.press("a")
            await settle(pilot)
            assert [c[0] for c in stub.calls].count("playlist_add") == 1
            assert len([c for c in stub.calls if c[0] == "playlist_list"]) == listings_before
            # the count still moves, from what the add itself answered
            table = app.query_one("#playlists-table", DataTable)
            assert str(table.get_cell("remote-1", "count")) == "413"

    asyncio.run(scenario())


def test_creating_a_playlist_still_re_lists_them():
    """A new row can only come from a listing."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app._create_playlist("Road Trip")
            await settle(pilot)
            assert len([c for c in stub.calls if c[0] == "playlist_list"]) >= 2

    asyncio.run(scenario())


def test_a_burst_of_queue_changes_redraws_once():
    """Loading a playlist changes mpv's playlist once per track it holds;
    each change used to redraw every row of the queue and the strip."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            drawn = []
            app._set_queue = lambda data: drawn.append(data)
            for n in range(1, 21):
                app._apply_event("queue_changed", _queue(n, 0))
            assert len(drawn) == 1  # the first; the other nineteen coalesce
            await pilot.pause(app.QUEUE_REDRAW_INTERVAL + 0.1)
            assert len(drawn) == 2
            assert len(drawn[-1]["tracks"]) == 20  # and it is the latest state

    asyncio.run(scenario())


def test_down_from_the_search_box_reaches_the_lists_and_then_l_plays_a_playlist():
    """At startup the search box has focus, so `l` is a letter there: Down
    (or Esc) must lead out, and the bar must say so."""
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.focused.id == "search-input"
            assert "Esc" in str(app.query_one("#shortcut-bar").render())
            await pilot.press("down")
            await settle(pilot)
            assert app.focused.id in ("search-results", "queue-table")
            assert "leave search" not in str(app.query_one("#shortcut-bar").render())
            assert app.query_one("#search-input").value == ""
            await pilot.press("l", "enter")
            await settle(pilot)
            assert ("playlist_play", {"playlist_id": "remote-1"}) in stub.calls
            # back in the search box, Down no longer plays anything and the hint returns
            await pilot.press("slash")
            await settle(pilot)
            assert "leave search" in str(app.query_one("#shortcut-bar").render())

    asyncio.run(scenario())


def test_ytm_tui_log_records_keys_focus_requests_and_errors(tmp_path, monkeypatch):
    log = tmp_path / "tui.log"
    monkeypatch.setenv("YTM_TUI_LOG", str(log))

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("escape", "l", "enter")
            await settle(pilot)
            app._show_error("boom")

    asyncio.run(scenario())
    text = log.read_text()
    assert "key 'l' focus=search-results" in text
    assert "focus -> playlists-table" in text
    assert "selected playlists-table row=0 key='remote-1'" in text
    assert "request playlist_play {'playlist_id': 'remote-1'}" in text
    assert "request playlist_play done in" in text
    assert "error 'boom'" in text
    assert "resize 120x40" in text
    assert text.splitlines()[0].split(" ", 1)[1].startswith("ytm ") and "started" in text.splitlines()[0]
    # a second run starts the file over: it is always the last run
    asyncio.run(scenario())
    assert log.read_text().count("started, size") == 1


def test_synced_lyrics_follow_position_seeks_gaps_and_track_clear():
    async def scenario():
        stub = LyricsStubClient(lyrics_response={"lyrics": [
            {"text": "[literal] first", "start_time": 1000, "end_time": 2500},
            {"text": "second", "start_time": 3000, "end_time": 5000},
        ]})
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            stub.push("track_changed", TRACK)
            await settle(pilot)
            pane = app.query_one(LyricsPane)
            assert pane._active is None
            for position, expected in [(1.2, 0), (3.1, 1), (1.4, 0), (2.7, None)]:
                stub.push("position", {"video_id": "abc123", "position": position})
                await settle(pilot)
                assert pane._active == expected
            assert "[literal]" in str(app.query_one("#lyrics-content").render())
            stub.push("track_changed", None)
            await settle(pilot)
            assert not pane._lines
            assert NO_LYRICS_TEXT in str(app.query_one("#lyrics-content").render())
    asyncio.run(scenario())


def test_lyrics_arriving_after_position_use_latest_time():
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test(size=(120, 40)):
            pane = app.query_one(LyricsPane)
            pane.set_position(4)
            pane.set_lyrics([{"text": "late", "start_time": 3000, "end_time": 5000}])
            assert pane._active == 0
            pane.set_lyrics("plain [words]")
            assert pane._active is None
            assert not pane._lines
    asyncio.run(scenario())


# -- lifecycle: completions that outlive the UI (LYR-01) ----------------------


class HoldingClient(LyricsStubClient):
    """A stub that blocks the named commands on an Event until released,
    then answers (or raises, for those in `fail`)."""

    def __init__(self, hold, fail=(), **kwargs):
        super().__init__(**kwargs)
        self.release = threading.Event()
        self._hold = set(hold)
        self._fail = set(fail)
        self.started = threading.Event()

    def request(self, cmd, args=None):
        if cmd in self._hold:
            self.calls.append((cmd, args))
            self.started.set()
            self.release.wait(timeout=5)
            if cmd in self._fail:
                raise ClientError(f"{cmd} failed late")
            if cmd == "lyrics":
                return self._lyrics_response
        return super().request(cmd, args)


def _spy_widget_access(app):
    """Count the banner/lyrics updates the app performs after the spy is set."""
    counts = {"error": 0, "clear": 0}
    real_show, real_clear = app._show_error, app._clear_error

    def show(message):
        counts["error"] += 1
        real_show(message)

    def clear():
        counts["clear"] += 1
        real_clear()

    app._show_error, app._clear_error = show, clear
    return counts


STARTUP_REQUESTS = {"queue_get", "playlist_list", "status"}


def _exit_with_pending_startup_request(fail):
    async def scenario():
        # every startup request is held, so nothing can legitimately clear
        # the banner between installing the spy and leaving the context
        stub = HoldingClient(hold=STARTUP_REQUESTS, fail=STARTUP_REQUESTS if fail else ())
        app = YTMApp(client=stub)
        counts = None
        try:
            async with app.run_test():
                deadline = asyncio.get_running_loop().time() + 5
                while len([c for c in stub.calls if c[0] in STARTUP_REQUESTS]) < 3:
                    assert asyncio.get_running_loop().time() < deadline, stub.calls
                    await asyncio.sleep(0.01)
                counts = _spy_widget_access(app)
                # leave with the request still held: the framework begins
                # teardown, then the request completes into a dead screen
            stub.release.set()
            await asyncio.sleep(0.2)
        finally:
            stub.release.set()
        assert counts == {"error": 0, "clear": 0}
        assert not app._accepts_results()
        assert stub.closed

    asyncio.run(scenario())


def test_exit_with_a_pending_request_that_succeeds_touches_no_widget():
    _exit_with_pending_startup_request(fail=False)


def test_exit_with_a_pending_request_that_fails_touches_no_widget():
    _exit_with_pending_startup_request(fail=True)


def test_quit_key_with_a_pending_lyrics_fetch_drops_the_late_result():
    async def scenario():
        stub = HoldingClient(hold={"lyrics"}, lyrics_response={"lyrics": "late words", "source": None})
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one("#queue-table", DataTable).focus()
                stub.push("track_changed", TRACK)
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                counts = _spy_widget_access(app)
                await pilot.press("e")
                assert not app._accepts_results()
                stub.release.set()
                await pilot.pause(0.2)
            await asyncio.sleep(0.1)
        finally:
            stub.release.set()
        assert counts == {"error": 0, "clear": 0}
        assert stub.closed
        assert app._lyrics_pending is None

    asyncio.run(scenario())


def test_errors_are_still_shown_while_the_app_runs():
    async def scenario():
        stub = RaisingClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            banner = app.query_one("#error-banner", Static)
            assert banner.display
            assert "auth expired" in str(banner.render())
            assert app._accepts_results()

    asyncio.run(scenario())


# -- request generations (LYR-03) ---------------------------------------------


def test_returning_to_a_track_drops_the_first_requests_late_error():
    """A (slow, then fails) -> B -> A: the second A request owns the pane."""
    first_a = threading.Event()
    seen = []

    class Client(LyricsStubClient):
        def request(self, cmd, args=None):
            if cmd != "lyrics":
                return super().request(cmd, args)
            self.calls.append((cmd, args))
            seen.append(args["video_id"])
            if seen.count("abc123") == 1 and args["video_id"] == "abc123":
                first_a.wait(timeout=5)
                raise ClientError("old failure")
            return {"lyrics": f"words of {args['video_id']}", "source": None}

    async def scenario():
        stub = Client()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                stub.push("track_changed", TRACK)
                await pilot.pause(0.05)
                stub.push("track_changed", {"video_id": "bbb", "title": "B"})
                await pilot.pause(0.05)
                stub.push("track_changed", TRACK)
                await pilot.pause(0.05)
                first_a.set()
                await pilot.pause(0.3)
                content = str(app.query_one("#lyrics-content").render())
                assert "old failure" not in content
                assert "words of abc123" in content
                # the skipped B was never asked for: one worker, newest pending wins
                assert seen == ["abc123", "abc123"]
        finally:
            first_a.set()

    asyncio.run(scenario())


def test_obsolete_lyrics_results_cannot_change_the_pane():
    async def scenario():
        stub = LyricsStubClient(lyrics_response={"lyrics": [
            {"text": "one", "start_time": 1000, "end_time": 2000},
        ], "source": None})
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            await settle(pilot)
            stub.push("position", {"video_id": "abc123", "position": 1.5})
            await settle(pilot)
            pane = app.query_one(LyricsPane)
            assert pane._active == 0
            current = app._lyrics_generation

            def snapshot():
                return (str(app.query_one("#lyrics-title").render()), pane.lyrics_text().plain, pane._active)

            before = snapshot()
            # an old error for the same song, and an old success after a newer error
            app.post_message(LyricsFetched(current - 1, "abc123", None, "old failure"))
            await settle(pilot)
            assert snapshot() == before
            stub._lyrics_error = "fresh failure"
            stub.push("track_changed", TRACK)
            await settle(pilot)
            assert "fresh failure" in str(app.query_one("#lyrics-content").render())
            app.post_message(LyricsFetched(current, "abc123", {"lyrics": "stale success"}, None))
            await settle(pilot)
            assert "fresh failure" in str(app.query_one("#lyrics-content").render())
            # A -> B: A's result arrives for the wrong song
            stub.push("track_changed", {"video_id": "bbb", "title": "B"})
            await settle(pilot)
            app.post_message(LyricsFetched(app._lyrics_generation, "abc123", {"lyrics": "for A"}, None))
            await settle(pilot)
            assert "for A" not in str(app.query_one("#lyrics-content").render())
            # A -> nothing playing
            stub.push("track_changed", None)
            await settle(pilot)
            app.post_message(LyricsFetched(app._lyrics_generation - 1, "bbb", {"lyrics": "for B"}, None))
            await settle(pilot)
            assert NO_LYRICS_TEXT in str(app.query_one("#lyrics-content").render())

    asyncio.run(scenario())


# -- visible sync behaviour: spans, boundaries, viewport (LYR-04/05/07) -------


def _timed(lines):
    return {"lyrics": [{"text": text, "start_time": start, "end_time": end} for text, start, end in lines],
            "source": None}


def _active_visible(pane):
    """Whether the highlighted line's rows lie inside the scroll viewport."""
    scroll = pane.query_one("#lyrics-scroll")
    first, last = pane.active_row_span()
    top = scroll.scroll_offset.y
    return top <= first and last < top + scroll.content_size.height


def test_rendered_spans_mark_only_the_active_line():
    async def scenario():
        stub = LyricsStubClient(lyrics_response=_timed([("first", 1000, 2500), ("second", 3000, 5000)]))
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            await settle(pilot)
            pane = app.query_one(LyricsPane)
            stub.push("position", {"video_id": "abc123", "position": 3.2})
            await settle(pilot)
            text = pane.lyrics_text()
            styles = [str(span.style) for span in text.spans if span.end - span.start > 1]
            assert styles == [INACTIVE_STYLE, ACTIVE_STYLE]
            assert text.plain.splitlines() == ["  first", ACTIVE_MARKER + "second"]
            # what the Static actually shows is that same Text
            shown = app.query_one("#lyrics-content").render()
            assert shown.plain == text.plain
            # Textual's Style keeps the attributes (its str() drops `reverse`)
            assert [(bool(span.style.dim), bool(span.style.bold), bool(span.style.reverse)) for span in shown.spans] == [
                (True, False, False), (False, True, True)]

    asyncio.run(scenario())


def test_half_open_interval_boundaries():
    async def scenario():
        stub = LyricsStubClient(lyrics_response=_timed([("first", 1000, 2500)]))
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            await settle(pilot)
            pane = app.query_one(LyricsPane)
            for position, expected in [(0.999, None), (1.0, 0), (2.499, 0), (2.5, None)]:
                stub.push("position", {"video_id": "abc123", "position": position})
                await settle(pilot)
                assert pane._active == expected, position

    asyncio.run(scenario())


def test_positions_of_another_video_and_pauses_leave_the_highlight_alone():
    async def scenario():
        stub = LyricsStubClient(lyrics_response=_timed([("first", 1000, 2500), ("second", 3000, 5000)]))
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            await settle(pilot)
            pane = app.query_one(LyricsPane)
            stub.push("position", {"video_id": "abc123", "position": 1.5})
            await settle(pilot)
            stub.push("position", {"video_id": "other", "position": 3.5})
            await settle(pilot)
            assert pane._active == 0 and pane._position == 1.5
            stub.push("state_changed", {"paused": True, "volume": 50})
            await pilot.pause(0.2)
            assert pane._active == 0

    asyncio.run(scenario())


def test_lyrics_arriving_while_a_fetch_is_held_highlight_the_current_position():
    stub = HoldingClient(hold={"lyrics"}, lyrics_response=_timed([("first", 1000, 2500), ("second", 3000, 5000)]))

    async def scenario():
        app = YTMApp(client=stub)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await settle(pilot)
                stub.push("track_changed", TRACK)
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                stub.push("position", {"video_id": "abc123", "position": 3.5})
                await pilot.pause(0.05)
                pane = app.query_one(LyricsPane)
                assert pane._active is None
                stub.release.set()
                await pilot.pause(0.3)
                assert pane._active == 1
                assert [str(span.style) for span in pane.lyrics_text().spans] == [INACTIVE_STYLE, ACTIVE_STYLE]
        finally:
            stub.release.set()

    asyncio.run(scenario())


LONG_LINES = [(f"long line {i} " + "la " * 12, i * 1000, i * 1000 + 900) for i in range(40)]
WIDE_LINES = [(f"{i} " + "ラララ " * 8, i * 1000, i * 1000 + 900) for i in range(40)]


def _run_resize_scenario(lines, sizes):
    async def scenario():
        stub = LyricsStubClient(lyrics_response=_timed(lines))
        app = YTMApp(client=stub)
        async with app.run_test(size=sizes[0]) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            await settle(pilot)
            pane = app.query_one(LyricsPane)
            scroll = pane.query_one("#lyrics-scroll")
            # a forward seek to a late line, then back: both must be in view
            for position in (30.1, 12.2):
                stub.push("position", {"video_id": "abc123", "position": position})
                await settle(pilot)
                assert scroll.scroll_offset.y > 0
                assert _active_visible(pane), position
            # paused: no further position events; only the terminal changes
            for size in sizes[1:]:
                await pilot.resize_terminal(*size)
                await settle(pilot, 0.1)
                if pane.display:
                    assert scroll.content_size.height > 0
                    assert _active_visible(pane), size
                assert pane._active == 12

    asyncio.run(scenario())


def test_resizing_narrower_and_wider_keeps_the_active_line_visible():
    _run_resize_scenario(LONG_LINES, [(160, 30), (110, 30), (200, 26), (120, 40)])


def test_resizing_with_wide_glyphs_keeps_the_active_line_visible():
    _run_resize_scenario(WIDE_LINES, [(160, 30), (110, 30), (200, 30)])


def test_leaving_compact_mode_recenters_with_the_real_width():
    _run_resize_scenario(LONG_LINES, [(160, 30), (90, 30), (160, 30), (110, 30)])


def test_malformed_timing_records_cannot_crash_playback_handlers():
    async def scenario():
        stub = LyricsStubClient(lyrics_response={"lyrics": [
            {"text": "missing end", "start_time": 1000},
            {"text": "fine", "start_time": 2000, "end_time": 3000},
            {"text": None, "start_time": 3000, "end_time": 4000},
        ], "source": None})
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            await settle(pilot)
            pane = app.query_one(LyricsPane)
            for position, expected in [(0, None), (1.5, None), (2.0, 0), (3.5, None)]:
                stub.push("position", {"video_id": "abc123", "position": position})
                await settle(pilot)
                assert pane._active == expected
            assert not app._exit
            # nothing valid at all reads as no lyrics, and the title is plain
            stub._lyrics_response = {"lyrics": [{"text": "x", "start_time": 5, "end_time": 5}], "source": None}
            stub.push("track_changed", {"video_id": "bbb", "title": "B"})
            await settle(pilot)
            assert NO_LYRICS_TEXT in str(app.query_one("#lyrics-content").render())
            assert str(app.query_one("#lyrics-title").render()) == "LYRICS"

    asyncio.run(scenario())


# -- bounded background work (LYR-06) -----------------------------------------


def test_rapid_track_changes_use_one_fetch_at_a_time_and_skip_stale_tracks():
    release = threading.Event()
    lock = threading.Lock()
    active = {"now": 0, "max": 0}

    class Client(LyricsStubClient):
        def request(self, cmd, args=None):
            if cmd != "lyrics":
                return super().request(cmd, args)
            self.calls.append((cmd, args))
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            try:
                release.wait(timeout=5)
            finally:
                with lock:
                    active["now"] -= 1
            return {"lyrics": f"words of {args['video_id']}", "source": None}

    async def scenario():
        stub = Client()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one("#queue-table", DataTable).focus()
                for i in range(6):
                    stub.push("track_changed", {"video_id": f"v{i}", "title": f"T{i}"})
                    await pilot.pause(0.02)
                # transport keys stay responsive while the fetch is held
                start_volume = app._volume
                await pilot.press("plus")
                await settle(pilot)
                assert app._volume == start_volume + 5
                release.set()
                await pilot.pause(0.4)
                asked = [c[1]["video_id"] for c in stub.calls if c[0] == "lyrics"]
                assert asked == ["v0", "v5"]
                assert active["max"] == 1
                assert "words of v5" in str(app.query_one("#lyrics-content").render())
                assert app._lyrics_thread is None or not app._lyrics_thread.is_alive() or app._lyrics_pending is None
        finally:
            release.set()

    asyncio.run(scenario())


# -- clock updates (LYR-09) ---------------------------------------------------


def test_subsecond_positions_reach_lyrics_but_rewrite_the_clock_once_per_second():
    async def scenario():
        stub = LyricsStubClient(lyrics_response=_timed([("first", 3000, 3600), ("second", 3600, 5000)]))
        app = YTMApp(client=stub)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            stub.push("track_changed", TRACK)
            await settle(pilot)
            clock = app.query_one("#now-playing-time", Static)
            writes = []
            real_update = clock.update
            clock.update = lambda text: (writes.append(str(text)), real_update(text))
            pane = app.query_one(LyricsPane)
            seen = []
            for position in (3.1, 3.5, 3.9, 4.0):
                stub.push("position", {"video_id": "abc123", "position": position, "duration_seconds": 312})
                await settle(pilot)
                seen.append(pane._active)
            assert seen == [0, 0, 1, 1]
            assert writes == ["0:03 / 5:12", "0:04 / 5:12"]
            # a backward seek and a duration-only change both redraw
            stub.push("position", {"video_id": "abc123", "position": 1.0, "duration_seconds": 312})
            stub.push("position", {"video_id": "abc123", "position": 1.0, "duration_seconds": 400})
            await settle(pilot)
            assert writes[-2:] == ["0:01 / 5:12", "0:01 / 6:40"]

    asyncio.run(scenario())


def test_playback_error_event_shows_a_safe_actionable_banner():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app._apply_event("playback_error", {"video_id": "v1"})
            await settle(pilot)
            banner = app.query_one("#error-banner")
            assert "Could not play this track" in str(banner.render())
            assert "v1" not in str(banner.render())  # no track/secret data on the banner

    asyncio.run(scenario())


# -- UI review regressions: intent, identity, feedback and freshness -------------
#
# One test per confirmed finding (UI-01 .. UI-15; the widget-level finders live
# in tests/test_tui_widgets.py and the backend contracts in
# tests/test_tui_backend.py). Each drives the user-visible state the review's
# probe captured, with the response held until the conflicting action happens.


class GatedSearchClient(StubClient):
    """A search for `hold_query` blocks until `release`; others answer at once."""

    def __init__(self, hold_query, fail=False):
        super().__init__()
        self.hold_query = hold_query
        self.fail = fail
        self.started = threading.Event()
        self.release = threading.Event()

    def request(self, cmd, args=None):
        if cmd == "search" and (args or {}).get("query") == self.hold_query:
            self.calls.append((cmd, args))
            self.started.set()
            self.release.wait(timeout=5)
            if self.fail:
                raise ClientError("obsolete search failure")
            return {"tracks": [TRACK]}
        return super().request(cmd, args)


def test_search_edit_invalidates_pending_results_and_autoplay():
    """UI-01: clearing the box while a submitted search is in flight must not
    restore results nor start playback for the abandoned query."""

    async def scenario():
        stub = GatedSearchClient(hold_query="ab")
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.click("#search-input")
                await pilot.press("a", "b")
                await pilot.press("enter")  # submits with a play-first intent
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                # the user clears the box before the search answers
                await pilot.press("backspace", "backspace")
                await pilot.pause()
                stub.release.set()
                await pilot.pause(0.3)
                assert app.query_one("#search-results", DataTable).row_count == 0
                assert not [c for c in stub.calls if c[0] == "play"]
        finally:
            stub.release.set()

    asyncio.run(scenario())


def test_search_shortened_below_the_live_minimum_drops_the_reply():
    """UI-01: a one-character edit invalidates a live search already running."""

    async def scenario():
        stub = GatedSearchClient(hold_query="ab")
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.click("#search-input")
                await pilot.press("a", "b")
                await pilot.pause(app.SEARCH_DEBOUNCE + 0.1)  # live search dispatched
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                await pilot.press("backspace")  # "a": below the live minimum
                stub.release.set()
                await pilot.pause(0.3)
                assert app.query_one("#search-results", DataTable).row_count == 0
        finally:
            stub.release.set()

    asyncio.run(scenario())


def test_search_typing_during_debounce_replaces_the_pending_query():
    """UI-01: an edit inside the debounce window replaces the pending query
    and the abandoned answer may not flash on screen."""

    async def scenario():
        stub = GatedSearchClient(hold_query="ab")
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.click("#search-input")
                await pilot.press("a", "b")
                await pilot.pause(app.SEARCH_DEBOUNCE + 0.1)  # "ab" dispatched, held
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                await pilot.press("c")  # "abc": inside the debounce window
                stub.release.set()  # the abandoned answer arrives now
                await pilot.pause(0.1)
                assert app.query_one("#search-results", DataTable).row_count == 0
                await pilot.pause(app.SEARCH_DEBOUNCE + 0.2)
                await settle(pilot)
                searches = [args["query"] for cmd, args in stub.calls if cmd == "search"]
                assert searches == ["ab", "abc"]
                assert app.query_one("#search-results", DataTable).row_count == 1
        finally:
            stub.release.set()

    asyncio.run(scenario())


def test_obsolete_search_error_cannot_replace_current_results():
    """UI-01/UI-06: a late failure from an abandoned query is dropped whole."""

    async def scenario():
        stub = GatedSearchClient(hold_query="ab", fail=True)
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.click("#search-input")
                await pilot.press("a", "b")
                await pilot.pause(app.SEARCH_DEBOUNCE + 0.1)
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                await pilot.press("c")  # "abc": a newer search
                await pilot.pause(app.SEARCH_DEBOUNCE + 0.2)
                assert app.query_one("#search-results", DataTable).row_count == 1
                stub.release.set()
                await pilot.pause(0.3)
                banner = app.query_one("#error-banner")
                assert not banner.display
                assert app.query_one("#search-results", DataTable).row_count == 1
        finally:
            stub.release.set()

    asyncio.run(scenario())


# -- UI-03: queue rows are entries, not positions --------------------------------


def test_queue_selection_tracks_entry_across_insertions():
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(QueuePane)
            table = app.query_one("#queue-table", DataTable)
            tracks = [dict(TRACK, video_id=name, title=name, entry_id=i)
                      for i, name in enumerate(("A", "B", "C"))]
            pane.set_queue({"tracks": tracks, "index": 0})
            table.move_cursor(row=2)
            assert pane.selected_track()["video_id"] == "C"
            assert pane.play_args() == {"entry_id": 2}

            # an insertion before the selection moves the row, not the entry
            inserted = [dict(TRACK, video_id="X", title="X", entry_id=99)] + tracks
            pane.set_queue({"tracks": inserted, "index": 1})
            assert table.cursor_row == 3
            assert pane.selected_track()["video_id"] == "C"
            assert pane.play_args() == {"entry_id": 2}

            # a reorder moves the selected entry too
            reordered = [inserted[3], inserted[0], inserted[1], inserted[2]]
            pane.set_queue({"tracks": reordered, "index": 2})  # A still playing
            assert pane.selected_track()["video_id"] == "C"
            assert pane.play_args() == {"entry_id": 2}

            # two queued copies of one media id are still distinct entries
            dupes = [dict(TRACK, video_id="D", title="D", entry_id=1),
                     dict(TRACK, video_id="D", title="D", entry_id=2)]
            pane.set_queue({"tracks": dupes, "index": 0})
            table.move_cursor(row=1)
            assert pane.play_args() == {"entry_id": 2}

            # deleting the selected entry keeps a documented nearby fallback
            pane.set_queue({"tracks": tracks, "index": 0})
            table.move_cursor(row=2)
            pane.set_queue({"tracks": tracks[:2], "index": 0})
            assert table.cursor_row == 1
            assert pane.play_args() is not None

    asyncio.run(scenario())


def test_queue_play_by_entry_id_ignores_a_stale_position():
    """UI-03/UI-14: a click always targets the entry the user saw."""

    class WithEntryIds(StubClient):
        def request(self, cmd, args=None):
            if cmd == "queue_get":
                self.calls.append((cmd, args))
                return {
                    "tracks": [dict(TRACK, title="A", entry_id=1),
                               dict(TRACK, video_id="b", title="B", entry_id=2)],
                    "index": 0,
                }
            return super().request(cmd, args)

    async def scenario():
        stub = WithEntryIds()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(QueuePane)
            table = app.query_one("#queue-table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await settle(pilot)
            assert pane.play_args() == {"entry_id": 2}
            await pilot.press("enter")
            await settle(pilot)
            queue_plays = [c for c in stub.calls if c[0] == "queue_play"]
            assert queue_plays == [("queue_play", {"entry_id": 2})]

    asyncio.run(scenario())


# -- UI-05: no success toast for a failed enqueue -------------------------------


def test_failed_enqueue_next_has_no_success_notification():
    class Flaky(StubClient):
        def __init__(self):
            super().__init__()
            self.fail = True

        def request(self, cmd, args=None):
            if cmd == "enqueue_next":
                self.calls.append((cmd, args))
                if self.fail:
                    raise ClientError("enqueue_next failed")
                return {"paused": False, "volume": 60}
            return super().request(cmd, args)

    async def scenario():
        stub = Flaky()
        app = YTMApp(client=stub)
        toasts = []
        app.notify = lambda message, **kw: toasts.append(str(message))
        async with app.run_test() as pilot:
            await _search(pilot)
            app.query_one("#search-results", DataTable).focus()
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            assert toasts == []
            assert "enqueue_next failed" in str(app.query_one("#error-banner").render())
            # the same key succeeds afterwards: exactly one toast, naming the
            # track that was actually enqueued
            stub.fail = False
            await pilot.press("u")
            await settle(pilot)
            assert toasts == ["Up next: Kaanave Kaanave"]

    asyncio.run(scenario())


# -- UI-06: errors belong to the operation that failed ---------------------------


def test_unrelated_success_preserves_owned_error():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            app._apply_event("playback_error", {"video_id": "v"})
            await settle(pilot)
            banner = app.query_one("#error-banner")
            assert banner.display
            assert "Could not play this track" in str(banner.render())

            # a successful live search is unrelated work: the error stays
            await pilot.click("#search-input")
            await pilot.press("a", "b")
            await pilot.pause(app.SEARCH_DEBOUNCE + 0.2)
            assert app.query_one("#search-results", DataTable).row_count == 1
            assert banner.display
            assert "Could not play this track" in str(banner.render())

            # a successful player command is the matching retry: it clears
            app.query_one("#queue-table").focus()
            await pilot.press("space")
            await settle(pilot)
            assert not banner.display

            # an explicit dismissal also works
            app._show_error("later failure")
            assert banner.display
            app._clear_error()
            assert not banner.display

    asyncio.run(scenario())


# -- UI-07: a partial playlist refresh is not a success -------------------------


def test_mix_refresh_reports_partial_failure():
    class Partial(StubClient):
        def __init__(self):
            super().__init__()
            self.error = None

        def request(self, cmd, args=None):
            if cmd == "mixes_refresh":
                self.calls.append((cmd, args))
                return {
                    "playlists": [{"playlist_id": "local-1", "title": "scratch",
                                   "track_count": 12, "local": True}],
                    "error": self.error,
                }
            return super().request(cmd, args)

    async def scenario():
        stub = Partial()
        app = YTMApp(client=stub)
        toasts = []
        app.notify = lambda message, **kw: toasts.append(str(message))
        async with app.run_test() as pilot:
            await settle(pilot)
            app.query_one("#queue-table").focus()
            stub.error = "account unavailable: run ytm login"
            await pilot.press("r")
            await settle(pilot)
            banner = app.query_one("#error-banner")
            assert "account unavailable" in str(banner.render())
            assert toasts == []  # no full-success announcement
            table = app.query_one("#playlists-table", DataTable)
            assert table.row_count >= 1  # local playlists stay usable
            assert str(table.get_cell("local-1", "count")) == "12"

            # an empty but successful refresh is still a success
            stub.error = None
            await pilot.press("r")
            await settle(pilot)
            assert toasts == ["Mixes refreshed"]
            assert not banner.display

    asyncio.run(scenario())


def test_stale_playlist_refresh_cannot_replace_a_newer_view():
    class Gated(StubClient):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls_started = 0

        def request(self, cmd, args=None):
            if cmd == "mixes_refresh":
                self.calls.append((cmd, args))
                self.calls_started += 1
                if self.calls_started == 1:
                    self.started.set()
                    self.release.wait(timeout=5)
                    return {"playlists": [{"playlist_id": "old", "title": "Old mix",
                                           "track_count": 1}]}
                return {"playlists": [{"playlist_id": "new", "title": "New mix",
                                       "track_count": 2}]}
            return super().request(cmd, args)

    async def scenario():
        stub = Gated()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                app.action_refresh_mixes()
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                app.action_refresh_mixes()  # newer request answered first
                await pilot.pause(0.3)
                stub.release.set()
                await pilot.pause(0.3)
                table = app.query_one("#playlists-table", DataTable)
                assert table.row_count == 2  # New mix + "+ new playlist"
                assert str(table.get_cell("new", "count")) == "2"
                assert "old" not in {str(key.value) for key in table.rows}
        finally:
            stub.release.set()

    asyncio.run(scenario())


# -- UI-08: counts only grow for a justified net addition ------------------------


def test_playlist_count_respects_zero_net_addition():
    class ZeroNet(StubClient):
        def request(self, cmd, args=None):
            if cmd == "playlist_add":
                self.calls.append((cmd, args))
                return {"playlist_id": args["playlist_id"], "added": 0, "track_count": 10}
            return super().request(cmd, args)

    async def scenario():
        app = YTMApp(client=ZeroNet())
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(PlaylistsPane)
            table = app.query_one("#playlists-table", DataTable)
            pane.set_playlists({"playlists": [
                {"playlist_id": "remote-1", "title": "Liked Songs", "track_count": 10},
            ]})
            table.move_cursor(row=0)
            app._armed = dict(TRACK)
            app._add_armed_to_highlighted()
            await settle(pilot)
            assert str(table.get_cell("remote-1", "count")) == "10"

            # an unknown net (no `added` field) does not invent an increment
            pane.set_count("remote-1", 10, added=0)
            assert str(table.get_cell("remote-1", "count")) == "10"

            # a known net still moves the row optimistically
            pane.set_count("remote-1", None, added=1)
            assert str(table.get_cell("remote-1", "count")) == "11"

    asyncio.run(scenario())


# -- UI-09: a playlist draft survives a failed create ----------------------------


class GatedCreateClient(StubClient):
    """playlist_create blocks until `release`; every call is recorded."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def request(self, cmd, args=None):
        if cmd == "playlist_create":
            self.calls.append((cmd, args))
            self.started.set()
            self.release.wait(timeout=5)
            return {"playlist_id": "PLnew", "title": args["title"], "local": False}
        return super().request(cmd, args)


def test_failed_playlist_create_preserves_draft():
    from textual.widgets import Input as _Input

    class Failing(StubClient):
        def request(self, cmd, args=None):
            if cmd == "playlist_create":
                self.calls.append((cmd, args))
                raise ClientError("could not create the playlist")
            return super().request(cmd, args)

    async def scenario():
        app = YTMApp(client=Failing())
        async with app.run_test() as pilot:
            await settle(pilot)
            pane = app.query_one(PlaylistsPane)
            pane.prompt_new()
            box = app.query_one("#playlist-name", _Input)
            box.value = "Road Trip 界"
            await pilot.press("enter")
            await settle(pilot)
            assert box.display and box.value == "Road Trip 界"
            assert not box.disabled
            assert app.focused is box
            assert "could not create" in str(app.query_one("#error-banner").render())

    asyncio.run(scenario())


def test_playlist_create_ignores_a_second_submit_and_keeps_a_newer_draft():
    from textual.widgets import Input as _Input

    async def scenario():
        stub = GatedCreateClient()
        app = YTMApp(client=stub)
        toasts = []
        app.notify = lambda message, **kw: toasts.append(str(message))
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                pane = app.query_one(PlaylistsPane)
                box = app.query_one("#playlist-name", _Input)
                session = pane.prompt_new()
                box.value = "First"
                app._create_playlist("First")
                app._create_playlist("First")  # a second Enter while pending
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                assert len([c for c in stub.calls if c[0] == "playlist_create"]) == 1

                # cancel the draft and start a newer one; the late success
                # must not erase it or close its prompt
                pane.close_prompt()
                pane.prompt_new()
                box.value = "Second"
                stub.release.set()
                await pilot.pause(0.4)
                assert box.display and box.value == "Second"
                assert toasts == ["Created playlist First"]
                assert session != pane.prompt_session()
        finally:
            stub.release.set()

    asyncio.run(scenario())


# -- UI-10: control commands run off the UI thread, in order ---------------------


def test_pending_control_request_does_not_block_input():
    class Gated(StubClient):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def request(self, cmd, args=None):
            if cmd == "toggle":
                self.calls.append((cmd, args))
                self.started.set()
                # untimed: a synchronous implementation could never return
                # from the key press while the player is silent
                self.release.wait()
            return super().request(cmd, args)

    async def scenario():
        stub = Gated()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one("#queue-table", DataTable).focus()
                await settle(pilot)
                await pilot.press("space")
                # the press returned while the player is still silent
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                assert not stub.release.is_set()
                # and the UI processed another event before the release
                await pilot.press("tab")
                await pilot.pause()
                assert app.focused.id != "queue-table"
                stub.release.set()
                await settle(pilot)
                assert ("toggle", None) in stub.calls
        finally:
            stub.release.set()

    asyncio.run(scenario())


def test_control_requests_keep_their_order():
    class Gated(StubClient):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.order = []

        def request(self, cmd, args=None):
            if cmd in ("toggle", "next", "prev"):
                self.calls.append((cmd, args))
                self.order.append(cmd)
                if cmd == "toggle":
                    self.started.set()
                    self.release.wait()
                return {"paused": False, "volume": 60}
            return super().request(cmd, args)

    async def scenario():
        stub = Gated()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one("#queue-table", DataTable).focus()
                await settle(pilot)
                await pilot.press("space")
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                await pilot.press("n")
                await pilot.press("p")
                stub.release.set()
                await settle(pilot)
                assert stub.order == ["toggle", "next", "prev"]
        finally:
            stub.release.set()

    asyncio.run(scenario())


def test_rapid_volume_updates_coalesce_to_the_final_level():
    class Gated(StubClient):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.levels = []

        def request(self, cmd, args=None):
            if cmd == "volume":
                self.calls.append((cmd, args))
                self.levels.append(args["level"])
                if len(self.levels) == 1:
                    self.started.set()
                    self.release.wait()
                return {"volume": args["level"]}
            return super().request(cmd, args)

    async def scenario():
        stub = Gated()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                start = app._volume
                app.action_volume_up()  # the first call is held by the stub
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                for _ in range(4):
                    app.action_volume_up()
                await pilot.pause()
                assert app._volume == start + 25
                stub.release.set()
                await settle(pilot)
                assert stub.levels == [start + 5, start + 25]
                assert app._volume == start + 25
        finally:
            stub.release.set()

    asyncio.run(scenario())


def test_quit_with_a_pending_control_request_touches_no_widget():
    class Gated(StubClient):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def request(self, cmd, args=None):
            if cmd == "toggle":
                self.calls.append((cmd, args))
                self.started.set()
                self.release.wait()
            return super().request(cmd, args)

    async def scenario():
        stub = Gated()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                app.query_one("#queue-table", DataTable).focus()
                await pilot.press("space")
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                counts = _spy_widget_access(app)
                await pilot.press("e")
                assert not app._accepts_results()
                stub.release.set()
                await pilot.pause(0.2)
            await asyncio.sleep(0.1)
        finally:
            stub.release.set()
        assert counts == {"error": 0, "clear": 0}
        assert stub.closed

    asyncio.run(scenario())


# -- UI-12: compact layouts never focus an invisible pane ------------------------


def test_compact_playlist_action_keeps_focus_visible():
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            assert app.screen.has_class("compact")
            assert app.focused.id == "search-input"

            # leave the search box (a letter key would be text there), then
            # `l`: in compact it switches the middle row to the playlists, and
            # focus must land on a widget the user can see
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            table = app.query_one("#playlists-table", DataTable)
            assert app.focused is table
            assert app._visible(app.focused)
            assert app.query_one("#playlists-pane").display

            # leaving the playlists puts the queue back and keeps focus visible
            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#queue-pane").display
            assert not app.query_one("#playlists-pane").display
            assert app.focused.id == "queue-table"
            assert app._visible(app.focused)

            # add-to-playlist from the visible queue works and stays visible
            app.query_one(QueuePane).set_queue(_queue(3, 0))
            app.query_one("#queue-table", DataTable).focus()
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            assert app.focused is table and app._visible(table)
            assert "adding" in str(app.query_one("#playlists-title").render())
            await pilot.press("enter")
            await settle(pilot)
            adds = [c for c in app.client.calls if c[0] == "playlist_add"]
            assert adds and adds[0][1]["video_ids"] == ["q0"]

            # growing the terminal restores the full layout
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert app.query_one("#queue-pane").display
            assert app.query_one("#playlists-pane").display
            assert app._visible(app.focused)

    asyncio.run(scenario())


# -- UI-13: backend metadata is displayed literally ------------------------------


def test_error_banner_displays_metadata_literally():
    async def scenario():
        app = YTMApp(client=StubClient())
        async with app.run_test() as pilot:
            await settle(pilot)
            banner = app.query_one("#error-banner")
            for message in (
                "[bold]literal title[/bold]",
                "[link=app.quit]click me[/link]\nsecond line",
                "playlist [2026]",
            ):
                app._show_error(message)
                rendered = banner.render()
                assert str(rendered) == f"error: {message}"
                assert rendered.spans == []  # no markup was parsed
                assert banner.display
                assert banner.styles.color is not None  # the CSS styling stays

    asyncio.run(scenario())


# -- UI-14: stale queue snapshots cannot repaint a newer queue -------------------


def test_old_queue_refresh_cannot_replace_new_event():
    class HeldQueue(StubClient):
        def __init__(self):
            super().__init__()
            self.hold = False
            self.started = threading.Event()
            self.release = threading.Event()

        def request(self, cmd, args=None):
            if cmd == "queue_get" and self.hold:
                self.calls.append((cmd, args))
                self.started.set()
                self.release.wait(timeout=5)
                return {"tracks": [dict(TRACK, title="old queue")], "index": 0}
            return super().request(cmd, args)

    async def scenario():
        stub = HeldQueue()
        app = YTMApp(client=stub)
        try:
            async with app.run_test() as pilot:
                await settle(pilot)
                stub.hold = True
                app._refresh_queue()  # a snapshot the user is waiting on
                await asyncio.get_running_loop().run_in_executor(None, stub.started.wait, 5)
                # a newer queue event arrives before that snapshot returns
                app._apply_event(
                    "queue_changed",
                    {"tracks": [dict(TRACK, title="new queue")], "index": 0},
                )
                await pilot.pause()
                stub.release.set()
                await settle(pilot)
                pane = app.query_one(QueuePane)
                assert pane._tracks[0]["title"] == "new queue"
                assert pane.selected_track()["title"] == "new queue"
                # the visible action also targets the newest queue
                assert pane._selected_key() == pane._keys[0]
                assert pane.play_args() is not None
        finally:
            stub.release.set()

    asyncio.run(scenario())
