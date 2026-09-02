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
from ytm.tui.backend import Backend, BackendError
from ytm.tui.lyrics import LyricsPane
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


class RequestDone(Message):
    """A background request finished; `then` runs on the message loop."""

    def __init__(self, cmd, data, error, then):
        super().__init__()
        self.cmd = cmd
        self.data = data
        self.error = error
        self.then = then


class LyricsFetched(Message):
    """The result of a background `lyrics` request, handed back to the
    Textual message loop the same way `DaemonEvent` is."""

    def __init__(self, video_id, data, error):
        super().__init__()
        self.video_id = video_id
        self.data = data
        self.error = error


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
        Binding("left", "seek_back", "Seek -5s", priority=True),
        Binding("right", "seek_forward", "Seek +5s", priority=True),
        ("plus", "volume_up", "Vol +"),
        ("minus", "volume_down", "Vol -"),
        ("tab", "cycle_pane", "Cycle panes"),
        Binding("q", "quit_only", "Quit", priority=True),
        Binding("Q", "quit_and_shutdown", "Quit + stop player", priority=True),
    ]

    def __init__(self, client=None, config=None):
        super().__init__()
        self._client_error = None
        self._volume = 100
        self._config = config if config is not None else config_mod.load()
        self._bindings = BindingsMap(self._build_bindings(self._config["keys"]))
        self.theme = self._resolve_theme(self._config["ui"]["theme"])
        try:
            self.client = client if client is not None else Backend()
        except BackendError as exc:
            self.client = None
            self._client_error = str(exc)
        self._listener_thread = None
        self._lyrics_video_id = None

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
            Binding("left", "seek_back", "Seek -5s", priority=True),
            Binding("right", "seek_forward", "Seek +5s", priority=True),
            ("plus", "volume_up", "Vol +"),
            ("minus", "volume_down", "Vol -"),
            ("tab", "cycle_pane", "Cycle panes"),
            Binding(keys["quit"], "quit_only", "Quit", priority=True),
            Binding("Q", "quit_and_shutdown", "Quit + stop player", priority=True),
        ]

    # -- layout --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield SearchPane(id="search-pane")
        with Horizontal(id="middle-row"):
            yield QueuePane(id="queue-pane")
            yield PlaylistsPane(id="playlists-pane")
            yield LyricsPane(id="lyrics-pane")
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
        def seed(data):
            if data is not None and data.get("volume") is not None:
                self._volume = data["volume"]
                self.query_one(NowPlaying).set_volume(self._volume)

        self._request_async("status", then=seed)

    def _listen(self):
        try:
            self.client.listen()
        except BackendError:
            # the connection dropped or the daemon went away; nothing more
            # to do from a background thread than stop listening quietly
            pass

    def _dispatch_event(self, event, data):
        self.post_message(DaemonEvent(event, data))

    def on_daemon_event(self, message: DaemonEvent):
        self._apply_event(message.event, message.data)

    def _fetch_lyrics(self, video_id):
        """Kick off a background `lyrics` request for `video_id`.

        Mirrors `_listen`'s thread + `post_message` hand-off: the request
        blocks on a background thread so a slow/unresponsive daemon never
        freezes the UI, and the result is marshalled back onto Textual's
        own loop via `LyricsFetched`.
        """
        if video_id is None or self.client is None:
            return
        self._lyrics_video_id = video_id

        def worker():
            try:
                data = self.client.request("lyrics", {"video_id": video_id})
            except BackendError as exc:
                self.post_message(LyricsFetched(video_id, None, str(exc)))
            else:
                self.post_message(LyricsFetched(video_id, data, None))

        threading.Thread(target=worker, daemon=True).start()

    def on_lyrics_fetched(self, message: LyricsFetched):
        # a later track_changed may have superseded this in-flight request
        if message.video_id != self._lyrics_video_id:
            return
        pane = self.query_one(LyricsPane)
        if message.error is not None:
            pane.set_error(message.error)
            return
        pane.set_lyrics((message.data or {}).get("lyrics"))

    def _apply_event(self, event, data):
        now_playing = self.query_one(NowPlaying)
        if event == "track_changed":
            now_playing.on_track_changed(data)
            self._fetch_lyrics((data or {}).get("video_id"))
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
        """Send one *local* command (mpv over IPC, milliseconds) and surface
        a `BackendError` as a visible banner. Anything that goes to the
        network must use `_request_async` so the cursor never waits on it."""
        if self.client is None:
            return None
        try:
            data = self.client.request(cmd, args)
            self._clear_error()
            return data
        except BackendError as exc:
            self._show_error(str(exc))
            return None

    def _request_async(self, cmd, args=None, then=None):
        """Run a request on a worker thread; `then(data)` runs back on the
        message loop once it completes. Keeps search, lyrics and playlist
        calls -- the ones that hit YouTube -- off the UI thread."""
        if self.client is None:
            return

        def work():
            try:
                data = self.client.request(cmd, args)
            except BackendError as exc:
                self.post_message(RequestDone(cmd, None, str(exc), then))
            else:
                self.post_message(RequestDone(cmd, data, None, then))

        self.run_worker(work, thread=True, name=cmd, group="requests")

    def on_request_done(self, message: RequestDone):
        if message.error is not None:
            self._show_error(message.error)
            return
        self._clear_error()
        if message.then is not None:
            message.then(message.data)

    def _refresh_queue(self):
        self._request_async(
            "queue_get", then=lambda data: self.query_one(QueuePane).set_queue(data)
        )

    def _refresh_playlists(self):
        self._request_async(
            "playlist_list",
            then=lambda data: self.query_one(PlaylistsPane).set_playlists(data),
        )

    # -- search --------------------------------------------------------

    def on_input_submitted(self, message: Input.Submitted):
        if message.input.id != "search-input":
            return
        def show(data):
            tracks = (data or {}).get("tracks") or []
            self.query_one(SearchPane).set_results(tracks)
            if tracks:
                self._request("play", self._track_args(tracks[0]))

        self._request_async("search", {"query": message.value}, then=show)

    def on_data_table_row_selected(self, message: DataTable.RowSelected):
        if message.data_table.id != "search-results":
            return
        self.action_play_selected()

    # -- actions ---------------------------------------------------------

    def action_focus_search(self):
        self.query_one("#search-input", Input).focus()

    @staticmethod
    def _track_args(track):
        return {
            "video_id": track.get("video_id"),
            "title": track.get("title"),
            "artist": track.get("artist"),
            "album": track.get("album"),
            "duration": track.get("duration"),
            "duration_seconds": track.get("duration_seconds"),
        }

    def _selected_track_args(self):
        track = self.query_one(SearchPane).selected_track()
        if track is None:
            return None
        return self._track_args(track)

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
        self._request_async(
            "playlist_add",
            {
                "playlist_id": playlist_id,
                "video_ids": [track_args["video_id"]],
                "tracks": [track_args],
            },
            then=lambda data: self._refresh_playlists(),
        )

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
