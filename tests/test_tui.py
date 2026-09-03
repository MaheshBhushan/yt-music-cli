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
            await pilot.press("e")
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
    from ytm.tui.nowplaying import AlbumArt
    from ytm import config as config_mod

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
        async with app.run_test() as pilot:
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
        async with app.run_test() as pilot:
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
            assert ("queue_play", {"index": 1}) in stub.calls

    asyncio.run(scenario())


def test_clicking_a_playlist_plays_it():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.click("#playlists-table", offset=(2, 1))
            await settle(pilot)
            assert ("playlist_play", {"playlist_id": "local-1"}) in stub.calls

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
            return  # second attempt "runs" and returns

        def close(self):
            self._closed = True
            super().close()

    async def scenario():
        stub = Flaky()
        app = YTMApp(client=stub)
        app.LISTEN_RETRY = 0.05
        shown = []
        app._show_error = shown.append  # startup requests clear the banner again, so record instead
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
            await pilot.press("P")     # move to playlists, cursor on the first one
            await settle(pilot)
            await pilot.press("A")
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
            await pilot.press("P")
            await settle(pilot)
            await pilot.press("A")
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
            await pilot.press("P")
            await settle(pilot)
            table = app.query_one("#playlists-table", DataTable)
            table.move_cursor(row=1)  # "scratch", the local one
            await settle(pilot)
            await pilot.press("A")
            await settle(pilot)
            add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
            assert add_calls[0][1]["playlist_id"] == "local-1"
            # the refresh re-lists (stub says 12) but must not move the highlight
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
        async with app.run_test() as pilot:
            await settle(pilot)
            # a queue with a different song highlighted, as after startup
            app.query_one(QueuePane).set_queue(_queue(3, 0))
            await _search(pilot)  # Enter plays the first result and refreshes the queue
            await settle(pilot)
            assert isinstance(app.focused, DataTable) and app.focused.id == "search-results"
            await pilot.press("P")
            await settle(pilot)
            await pilot.press("A")
            await settle(pilot)
            add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
            assert add_calls[-1][1]["video_ids"] == [TRACK["video_id"]]

    asyncio.run(scenario())
