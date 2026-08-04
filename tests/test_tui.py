"""Tests for the Textual TUI, driven against a stub Client (no daemon).

Following this project's convention (see tests/test_mpris.py) of running
async scenarios via `asyncio.run()` inside plain sync test functions,
rather than depending on the pytest-asyncio plugin.
"""

import asyncio

from textual.widgets import DataTable

from ytm.client import ClientError
from ytm.tui.app import YTMApp
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
    await pilot.pause()


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


def test_enter_plays_selected_row():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            table.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            play_calls = [c for c in stub.calls if c[0] == "play"]
            assert len(play_calls) == 1
            assert play_calls[0][1]["video_id"] == "abc123"

    asyncio.run(scenario())


def test_enqueue_key_sends_enqueue():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await _search(pilot)
            table = app.query_one("#search-results", DataTable)
            table.focus()
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
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
            await pilot.pause()

            await pilot.press("space")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

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
            await pilot.pause()

            await pilot.press("left")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()

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
            await pilot.pause()
            start = app._volume

            await pilot.press("plus")
            await pilot.pause()
            assert app._volume == start + 5

            await pilot.press("minus")
            await pilot.pause()
            assert app._volume == start

    asyncio.run(scenario())


def test_position_event_moves_progress_bar_without_polling():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await pilot.pause()
            calls_before = len(stub.calls)

            stub.push(
                "position",
                {"position": 42, "video_id": "abc123", "duration_seconds": 312},
            )
            await pilot.pause()

            now_playing = app.query_one(NowPlaying)
            progress_bar = now_playing.query_one("#now-playing-progress")
            assert progress_bar.progress == 42

            # push-driven: no new commands (in particular no `status` poll)
            # were sent to arrive at this update
            assert len(stub.calls) == calls_before

            # give any timers a chance to fire, then confirm nothing polled
            await pilot.pause(0.3)
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
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        assert not any(c[0] == "shutdown" for c in stub.calls)
        assert stub.closed

    asyncio.run(scenario())


def test_uppercase_q_quits_and_shuts_down_daemon():
    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            app.query_one("#queue-table").focus()
            await pilot.pause()
            await pilot.press("Q")
            await pilot.pause()
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

        original = app_module.Client
        app_module.Client = FailingClient
        try:
            app = YTMApp(client=None)
        finally:
            app_module.Client = original

        async with app.run_test() as pilot:
            await pilot.pause()
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
            await pilot.pause()
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
            await pilot.pause()
            await pilot.press("P")
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            add_calls = [c for c in stub.calls if c[0] == "playlist_add"]
            assert len(add_calls) == 1
            assert add_calls[0][1]["video_ids"] == ["abc123"]

    asyncio.run(scenario())


def test_client_error_on_playlist_action_shows_banner_not_crash():
    async def scenario():
        stub = RaisingClient()
        app = YTMApp(client=stub)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._request("playlist_add", {"playlist_id": "x", "video_ids": ["y"]})
            await pilot.pause()
            banner = app.query_one("#error-banner")
            assert "auth expired" in str(banner.render())

    asyncio.run(scenario())


def test_smoke_render_layout():
    """Headless smoke run: the app starts up and lays out all panes."""

    async def scenario():
        stub = StubClient()
        app = YTMApp(client=stub)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.query_one(SearchPane) is not None
            assert app.query_one(QueuePane) is not None
            assert app.query_one(NowPlaying) is not None
            assert app.query_one("#playlists-pane") is not None
            svg = app.export_screenshot()
            assert "<svg" in svg
            assert len(svg) > 1000
            return svg

    svg = asyncio.run(scenario())
    with open("/tmp/ytm_tui_smoke.svg", "w") as fh:
        fh.write(svg)

    print(f"Smoke screenshot written to /tmp/ytm_tui_smoke.svg ({len(svg)} bytes)")


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
            await pilot.pause()
            # move focus off the search input, which otherwise swallows
            # printable keys before they reach the app's bindings
            app.query_one("#queue-table", DataTable).focus()
            await pilot.pause()
            # the configured key fires the action
            await pilot.press("x")
            await pilot.pause()
            assert ("toggle", None) in stub.calls
            # the old default no longer triggers it
            await pilot.press("space")
            await pilot.pause()
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
