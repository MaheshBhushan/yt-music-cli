"""Tests for the Textual TUI, driven against a stub Client (no daemon).

Following this project's convention (see tests/test_mpris.py) of running
async scenarios via `asyncio.run()` inside plain sync test functions,
rather than depending on the pytest-asyncio plugin.
"""

import asyncio

from textual.widgets import DataTable

from ytm.tui.backend import BackendError as ClientError
from ytm.tui.app import YTMApp
from ytm.tui.lyrics import LyricsPane, NO_LYRICS_TEXT
from ytm.tui.nowplaying import NowPlaying
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
            return {"playlist_id": args["playlist_id"], "added": len(args["video_ids"])}
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
            await pilot.press("a")
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


def test_lowercase_q_quits_without_shutdown():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await settle(pilot)
            await pilot.press("q")
            await settle(pilot)
        assert not any(c[0] == "shutdown" for c in stub.calls)
        assert stub.closed

    asyncio.run(scenario())


def test_uppercase_q_quits_and_shuts_down_daemon():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await settle(pilot)
            await pilot.press("Q")
            await settle(pilot)
        assert any(c[0] == "shutdown" for c in stub.calls)
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
            assert table.row_count == 2
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
            await pilot.press("P")
            await settle(pilot)
            await pilot.press("A")
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


def test_smoke_render_layout():
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
    with open("/tmp/ytm_tui_smoke_100x30.svg", "w") as fh:
        fh.write(svg_100x30)
    print(f"Smoke screenshot written to /tmp/ytm_tui_smoke_100x30.svg ({len(svg_100x30)} bytes)")

    svg_120x40 = asyncio.run(scenario((120, 40)))
    with open("/tmp/ytm_tui_smoke_120x40.svg", "w") as fh:
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

    import threading
    import time

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
        "audio": {"volume": 70, "device": "auto"},
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
        async with dark_app.run_test():
            dark_primary = dark_app.get_theme(dark_app.theme).primary

        light_config = _config_with_keys()
        light_config["ui"]["theme"] = "light"
        light_stub = StubClient()
        light_app = YTMApp(client=light_stub, config=light_config)
        async with light_app.run_test():
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
