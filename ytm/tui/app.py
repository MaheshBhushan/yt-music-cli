"""The Textual TUI root app: composes the panes, owns the Client, and wires
daemon events to widget updates without ever polling.
"""

import sys
import threading

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Input, DataTable, Static

from ytm import config as config_mod
from ytm.client import Client, ClientError
from ytm.tui.nowplaying import NowPlaying
from ytm.tui.playlists import PlaylistsPane
from ytm.tui.queue import QueuePane
from ytm.tui.search import SearchPane

SEEK_STEP = 5
VOLUME_STEP = 5

#: `[ui] theme` values mapped onto Textual's built-in theme names
THEMES = {
    "dark": "textual-dark",
    "light": "textual-light",
}


class DaemonEvent(Message):
    """A daemon event, marshalled onto the app's message loop.

    `Client.on_event` callbacks may run on a background listener thread;
    `post_message` is the thread-safe hand-off into Textual's own loop.
    """

    def __init__(self, event, data):
        super().__init__()
        self.event = event
        self.data = data


class YTMApp(App):
    """ytm's full-screen player: search, queue, playlists (later) and
    now-playing, all driven by pushed daemon events."""

    CSS_PATH = "app.tcss"

    BINDINGS = [
        ("/", "focus_search", "Search"),
        ("a", "enqueue_selected", "Enqueue"),
        ("P", "focus_playlists", "Playlists"),
        ("A", "add_to_playlist", "Add to playlist"),
        ("space", "toggle", "Play/Pause"),
        ("n", "next", "Next"),
        ("p", "prev", "Prev"),
        ("left", "seek_back", "Seek -5s"),
        ("right", "seek_forward", "Seek +5s"),
        ("plus", "volume_up", "Vol +"),
        ("minus", "volume_down", "Vol -"),
        ("tab", "cycle_pane", "Cycle panes"),
        Binding("q", "quit_only", "Quit", priority=True),
        Binding("Q", "quit_and_shutdown", "Quit + stop daemon", priority=True),
    ]

    def __init__(self, client=None, config=None):
        super().__init__()
        self._client_error = None
        self._volume = 100
        self._config = config if config is not None else config_mod.load()
        self._bindings = BindingsMap(self._build_bindings(self._config["keys"]))
        self.theme = self._resolve_theme(self._config["ui"]["theme"])
        try:
            self.client = client if client is not None else Client()
        except ClientError as exc:
            self.client = None
            self._client_error = str(exc)
        self._listener_thread = None

    @staticmethod
    def _resolve_theme(name):
        """The Textual theme name for `[ui] theme`, defaulting to dark."""
        if name not in THEMES:
            print(
                f"ytm: config warning: unknown ui.theme '{name}'; using 'dark'",
                file=sys.stderr,
            )
            return THEMES["dark"]
        return THEMES[name]

    @staticmethod
    def _build_bindings(keys):
        """The BINDINGS table with the five customisable keys from config.

        Any other binding (`a`, `+`, `-`, `Tab`, `P`, `A`, arrows, `Q`) keeps
        its hardcoded default -- only `toggle`, `next`, `prev`, `search` and
        `quit` are user-configurable.
        """
        return [
            (keys["search"], "focus_search", "Search"),
            ("a", "enqueue_selected", "Enqueue"),
            ("P", "focus_playlists", "Playlists"),
            ("A", "add_to_playlist", "Add to playlist"),
            (keys["toggle"], "toggle", "Play/Pause"),
            (keys["next"], "next", "Next"),
            (keys["prev"], "prev", "Prev"),
            ("left", "seek_back", "Seek -5s"),
            ("right", "seek_forward", "Seek +5s"),
            ("plus", "volume_up", "Vol +"),
            ("minus", "volume_down", "Vol -"),
            ("tab", "cycle_pane", "Cycle panes"),
            Binding(keys["quit"], "quit_only", "Quit", priority=True),
            Binding("Q", "quit_and_shutdown", "Quit + stop daemon", priority=True),
        ]

    # -- layout --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield SearchPane(id="search-pane")
        with Horizontal(id="middle-row"):
            yield QueuePane(id="queue-pane")
            yield PlaylistsPane(id="playlists-pane")
        yield NowPlaying(id="now-playing")
        yield Static("", id="error-banner")

    def on_mount(self):
        if self._client_error is not None:
            self._show_error(self._client_error)
            return
        self.client.on_event(self._dispatch_event)
        self._listener_thread = threading.Thread(
            target=self._listen, daemon=True
        )
        self._listener_thread.start()
        self._refresh_queue()
        self._refresh_playlists()
        self._seed_volume()
        self.query_one("#search-input", Input).focus()

    def _seed_volume(self):
        """One-time `status` fetch at startup, purely to seed the volume
        indicator -- not a poll, never repeated."""
        data = self._request("status")
        if data is not None and data.get("volume") is not None:
            self._volume = data["volume"]
            self.query_one(NowPlaying).set_volume(self._volume)

    def _listen(self):
        try:
            self.client.listen()
        except ClientError:
            # the connection dropped or the daemon went away; nothing more
            # to do from a background thread than stop listening quietly
            pass

    def _dispatch_event(self, event, data):
        self.post_message(DaemonEvent(event, data))

    def on_daemon_event(self, message: DaemonEvent):
        self._apply_event(message.event, message.data)

    def _apply_event(self, event, data):
        now_playing = self.query_one(NowPlaying)
        if event == "track_changed":
            now_playing.on_track_changed(data)
        elif event == "position":
            now_playing.on_position(data)
        elif event == "state_changed":
            now_playing.on_state_changed(data)
            volume = (data or {}).get("volume")
            if volume is not None:
                self._volume = volume
        elif event == "queue_changed":
            self.query_one(QueuePane).set_queue(data)
        elif event == "error":
            self._show_error((data or {}).get("error") or "daemon error")

    # -- helpers ---------------------------------------------------------

    def _show_error(self, message):
        self.query_one("#error-banner", Static).update(f"error: {message}")

    def _clear_error(self):
        self.query_one("#error-banner", Static).update("")

    def _request(self, cmd, args=None):
        """Send one command, surfacing a `ClientError` as a visible banner."""
        if self.client is None:
            return None
        try:
            data = self.client.request(cmd, args)
            self._clear_error()
            return data
        except ClientError as exc:
            self._show_error(str(exc))
            return None

    def _refresh_queue(self):
        data = self._request("queue_get")
        if data is not None:
            self.query_one(QueuePane).set_queue(data)

    def _refresh_playlists(self):
        data = self._request("playlist_list")
        if data is not None:
            self.query_one(PlaylistsPane).set_playlists(data)

    # -- search --------------------------------------------------------

    def on_input_submitted(self, message: Input.Submitted):
        if message.input.id != "search-input":
            return
        data = self._request("search", {"query": message.value})
        if data is not None:
            self.query_one(SearchPane).set_results(data.get("tracks") or [])

    def on_data_table_row_selected(self, message: DataTable.RowSelected):
        if message.data_table.id != "search-results":
            return
        self.action_play_selected()

    # -- actions ---------------------------------------------------------

    def action_focus_search(self):
        self.query_one("#search-input", Input).focus()

    def _selected_track_args(self):
        track = self.query_one(SearchPane).selected_track()
        if track is None:
            return None
        return {
            "video_id": track.get("video_id"),
            "title": track.get("title"),
            "artist": track.get("artist"),
            "album": track.get("album"),
            "duration": track.get("duration"),
            "duration_seconds": track.get("duration_seconds"),
        }

    def action_play_selected(self):
        args = self._selected_track_args()
        if args is None:
            return
        self._request("play", args)

    def action_enqueue_selected(self):
        args = self._selected_track_args()
        if args is None:
            return
        self._request("enqueue", args)

    def action_toggle(self):
        self._request("toggle")

    def action_next(self):
        self._request("next")

    def action_prev(self):
        self._request("prev")

    def action_seek_back(self):
        self._request("seek", {"seconds": -SEEK_STEP})

    def action_seek_forward(self):
        self._request("seek", {"seconds": SEEK_STEP})

    def action_volume_up(self):
        level = max(0, min(100, self._volume + VOLUME_STEP))
        data = self._request("volume", {"level": level})
        if data is not None:
            self._volume = data.get("volume", level)

    def action_volume_down(self):
        level = max(0, min(100, self._volume - VOLUME_STEP))
        data = self._request("volume", {"level": level})
        if data is not None:
            self._volume = data.get("volume", level)

    def action_focus_playlists(self):
        self.query_one("#playlists-table", DataTable).focus()

    def action_add_to_playlist(self):
        track_args = self._selected_track_args()
        if track_args is None:
            return
        playlist_id = self.query_one(PlaylistsPane).selected_playlist_id()
        if playlist_id is None:
            return
        self._request(
            "playlist_add",
            {
                "playlist_id": playlist_id,
                "video_ids": [track_args["video_id"]],
                "tracks": [track_args],
            },
        )
        self._refresh_playlists()

    def action_cycle_pane(self):
        self.focus_next()

    def action_quit_only(self):
        if self.client is not None:
            self.client.close()
        self.exit()

    def action_quit_and_shutdown(self):
        if self.client is not None:
            self._request("shutdown")
            self.client.close()
        self.exit()


def run():
    """Launch the TUI."""
    YTMApp().run()


if __name__ == "__main__":
    run()
