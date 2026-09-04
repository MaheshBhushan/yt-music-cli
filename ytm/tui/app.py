"""The Textual TUI root app: composes the panes, owns the Client, and wires
daemon events to widget updates without ever polling.
"""

import sys
import threading
import time

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Input, Static

from ytm import config as config_mod
from ytm import update
from ytm.tui.backend import Backend, BackendError
from ytm.tui.lyrics import LyricsPane
from ytm.tui.nowplaying import DEFAULT_ART, NowPlaying
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

    #: below either of these the layout drops to the essentials (see `.compact`
    #: in app.tcss): search box, queue, now-playing strip and shortcut bar
    COMPACT_WIDTH = 100
    COMPACT_HEIGHT = 24

    BINDINGS = [
        ("/", "focus_search", "Search"),
        ("s", "focus_search", "Search"),
        ("q", "enqueue_selected", "Enqueue"),
        ("u", "play_next_selected", "Up next"),
        ("l", "focus_playlists", "Playlists"),
        ("a", "add_to_playlist", "Add to playlist"),
        ("r", "refresh_mixes", "Refresh mixes"),
        ("space", "toggle", "Play/Pause"),
        ("n", "next", "Next"),
        ("p", "prev", "Prev"),
        Binding("left", "seek_back", "Seek -5s", priority=True),
        Binding("right", "seek_forward", "Seek +5s", priority=True),
        # priority: volume from anywhere, even while typing a search
        Binding("plus", "volume_up", "Vol +", priority=True),
        Binding("minus", "volume_down", "Vol -", priority=True),
        ("tab", "cycle_pane", "Cycle panes"),
        Binding("escape", "focus_results", "Results", show=False),
        ("e", "quit_only", "Exit"),
        ("x", "quit_and_shutdown", "Exit + stop player"),
    ]

    def __init__(self, client=None, config=None):
        super().__init__()
        self._client_error = None
        self._volume = 100
        self._armed = None  # song chosen with A, waiting for a playlist
        self._return_to = None
        self._search_timer = None  # pending debounced live search
        self._search_seq = 0  # rises per search; late replies for older ones are dropped
        self._results_query = None  # query the results table currently shows
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

        Any other binding (`q`, `u`, `s`, `+`, `-`, `Tab`, `l`, `a`, arrows, `x`) keeps
        its hardcoded default -- only `toggle`, `next`, `prev`, `search` and
        `quit` are user-configurable.
        """
        return [
            (keys["search"], "focus_search", "Search"),
            # `s`/`e` are plain (non-priority) bindings: they act from any
            # pane but stay ordinary letters while the search box has focus
            ("s", "focus_search", "Search"),
            ("q", "enqueue_selected", "Enqueue"),
            ("u", "play_next_selected", "Up next"),
            ("l", "focus_playlists", "Playlists"),
            ("a", "add_to_playlist", "Add to playlist"),
            ("r", "refresh_mixes", "Refresh mixes"),
            (keys["toggle"], "toggle", "Play/Pause"),
            (keys["next"], "next", "Next"),
            (keys["prev"], "prev", "Prev"),
            # priority so they seek from any pane, but `check_action` hands
            # them back to the search box while it has focus
            Binding("left", "seek_back", "Seek -5s", priority=True),
            Binding("right", "seek_forward", "Seek +5s", priority=True),
            # priority: volume from anywhere, even while typing a search
            Binding("plus", "volume_up", "Vol +", priority=True),
            Binding("minus", "volume_down", "Vol -", priority=True),
            ("tab", "cycle_pane", "Cycle panes"),
            Binding("escape", "focus_results", "Results", show=False),
            # no priority on any letter key: while the search box has focus
            # every letter is text, so "queen" or "eels" can be searched
            (keys["quit"], "quit_only", "Exit"),
            ("x", "quit_and_shutdown", "Exit + stop player"),
        ]

    @staticmethod
    def _shortcut_text(keys):
        """One line naming every shortcut, in the order people reach for them.

        Each entry is also a mouse target: clicking it runs the same action
        the key would.
        """
        def link(key, label, action):
            return f"[@click=app.{action}][b]{key}[/b] {label}[/]"

        search_key = f"{keys['search']} {'s' if keys['search'] != 's' else ''}".strip()
        return "  ".join([
            link(keys["quit"], "exit", "quit_only"),
            link(search_key, "search", "focus_search"),
            link(keys["toggle"], "play/pause", "toggle"),
            link(keys["next"], "next", "next"),
            link(keys["prev"], "prev", "prev"),
            link("q", "enqueue", "enqueue_selected"),
            link("u", "play next", "play_next_selected"),
            f"[@click=app.seek_back][b]←[/b][/]/[@click=app.seek_forward][b]→[/b][/] seek",
            f"[@click=app.volume_up][b]+[/b][/]/[@click=app.volume_down][b]-[/b][/] volume",
            link("l", "playlists", "focus_playlists"),
            link("a", "add to playlist", "add_to_playlist"),
            link("r", "refresh mixes", "refresh_mixes"),
            link("Tab", "panes", "cycle_pane"),
            link("x", "exit+stop", "quit_and_shutdown"),
        ])

    def check_action(self, action, parameters):
        # the seek arrows are priority bindings; while the search box has
        # focus they must move the text cursor instead
        if action in ("seek_back", "seek_forward") and isinstance(self.focused, Input):
            return False
        return True

    # -- layout --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield SearchPane(id="search-pane")
        with Horizontal(id="middle-row"):
            yield QueuePane(id="queue-pane")
            yield PlaylistsPane(id="playlists-pane")
            yield LyricsPane(id="lyrics-pane")
        yield NowPlaying(
            id="now-playing",
            art=self._config["ui"].get("art", DEFAULT_ART),
            queue_column_width=(self._config.get("tui") or config_mod.DEFAULTS["tui"])[
                "queue_column_width"
            ],
        )
        yield Static("", id="error-banner")
        # the shortcut bar: every key, always, whatever has focus (Textual's
        # Footer hides letter keys while the search box is focused)
        yield Static(self._shortcut_text(self._config["keys"]), id="shortcut-bar")

    def on_resize(self, event):
        """Small terminals lose the results table, playlists and lyrics."""
        size = event.size
        compact = size.width < self.COMPACT_WIDTH or size.height < self.COMPACT_HEIGHT
        self.screen.set_class(compact, "compact")
        if compact and self.focused is not None and not self.focused.display:
            self.query_one("#search-input", Input).focus()

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
        if self._update_setting("check"):
            self.run_worker(self._check_for_update, thread=True, name="update-check", group="update")

    def _update_setting(self, key):
        # configs built by hand (tests, old files) may lack the section
        return (self._config.get("update") or {}).get(key, config_mod.DEFAULTS["update"][key])

    def _check_for_update(self):
        """Once-a-day PyPI check off the UI thread; a toast if there is news.
        With `[update] auto = true` the upgrade runs right here too."""
        info = update.check()
        if not info["newer"]:
            return
        latest = info["latest"]
        if not self._update_setting("auto"):
            self.call_from_thread(
                self.notify,
                f"ytm {latest} is available (you have {info['installed']}). Run: ytm update",
                title="Update available", timeout=12,
            )
            return
        ok, text = update.upgrade()
        if ok:
            self.call_from_thread(
                self.notify, f"Updated ytm to {latest}. Restart to use it.",
                title="Updated", timeout=12,
            )
        else:
            self.call_from_thread(
                self.notify, f"Auto-update failed: {text.splitlines()[0] if text else 'unknown error'}",
                title="Update", severity="warning", timeout=12,
            )

    def _seed_volume(self):
        """One-time `status` fetch at startup -- not a poll, never repeated.

        Seeds the volume indicator and picks the starting focus: when mpv
        already has a track loaded the queue gets it, so space/arrows drive
        playback straight away; an empty player starts in the search box.
        """
        def seed(data):
            if data is None:
                return
            if data.get("volume") is not None:
                self._volume = data["volume"]
                self.query_one(NowPlaying).set_volume(self._volume)
            if data.get("current"):
                self.query_one("#queue-table", DataTable).focus()

        self._request_async("status", then=seed)

    #: seconds between reconnect attempts after the event connection drops
    LISTEN_RETRY = 1.0
    #: live search: wait this long after the last keystroke before asking YouTube
    SEARCH_DEBOUNCE = 0.35
    #: and only once the query is at least this long
    SEARCH_MIN_CHARS = 2

    def _listen(self):
        """Run the event listener until the app closes, reconnecting if the
        connection to mpv drops (mpv restarted, socket hiccup). Each drop is
        reported on the banner, so a stale pane is never silent."""
        while not getattr(self.client, "_closed", False):
            try:
                self.client.listen()
                return
            except BackendError as exc:
                if getattr(self.client, "_closed", False):
                    return
                self._dispatch_event("error", {"error": f"player events lost ({exc}); reconnecting"})
                time.sleep(self.LISTEN_RETRY)

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
            self._set_queue(data)
        elif event == "error":
            self._show_error((data or {}).get("error") or "daemon error")

    # -- helpers ---------------------------------------------------------

    def _show_error(self, message):
        banner = self.query_one("#error-banner", Static)
        banner.update(f"error: {message}")
        banner.display = True

    def _clear_error(self):
        banner = self.query_one("#error-banner", Static)
        banner.update("")
        banner.display = False

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

    def _set_queue(self, data):
        self.query_one(QueuePane).set_queue(data)
        self.query_one(NowPlaying).set_queue(data)

    def _refresh_queue(self):
        self._request_async("queue_get", then=self._set_queue)

    def _refresh_playlists(self):
        def show(data):
            self.query_one(PlaylistsPane).set_playlists(data)
            if data and data.get("error"):
                self._show_error(data["error"])

        self._request_async("playlist_list", then=show)

    # -- search --------------------------------------------------------

    def on_input_changed(self, message: Input.Changed):
        """Search as you type: results appear after a short pause in typing,
        no Enter needed. Enter still plays the first result."""
        if message.input.id != "search-input":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None
        query = message.value.strip()
        if len(query) < self.SEARCH_MIN_CHARS:
            return
        self._search_timer = self.set_timer(
            self.SEARCH_DEBOUNCE, lambda: self._live_search(query), name="live-search"
        )

    def _live_search(self, query):
        """Fetch results for `query`; a slower, older search can never
        overwrite a newer one (the sequence number guards that)."""
        self._search_timer = None
        self._search_seq += 1
        seq = self._search_seq
        self._request_async(
            "search", {"query": query},
            then=lambda data: self._show_search_results(seq, query, data),
        )

    def _show_search_results(self, seq, query, data):
        if seq != self._search_seq:
            return  # stale: the user has typed more since
        tracks = (data or {}).get("tracks") or []
        self.query_one(SearchPane).set_results(tracks)
        self._results_query = query

    def on_input_submitted(self, message: Input.Submitted):
        if message.input.id == "playlist-name":
            self._create_playlist(message.value)
            return
        if message.input.id != "search-input":
            return
        query = message.value.strip()
        if not query:
            return

        def play_first(tracks):
            if tracks:
                self._request("play", self._track_args(tracks[0]))
            # hand focus to the results: from here space toggles, arrows
            # seek and Enter/click plays, instead of typing into the box
            self.action_focus_results()

        if self._results_query == query:
            # the live search already fetched these; don't ask YouTube again
            play_first(getattr(self.query_one(SearchPane), "_tracks", []))
            return
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None
        self._search_seq += 1
        seq = self._search_seq

        def show(data):
            self._show_search_results(seq, query, data)
            if seq == self._search_seq:
                play_first((data or {}).get("tracks") or [])

        self._request_async("search", {"query": query}, then=show)

    def on_data_table_row_selected(self, message: DataTable.RowSelected):
        """Enter or a mouse click on any of the three tables."""
        table_id = message.data_table.id
        if table_id == "search-results":
            self.action_play_selected()
        elif table_id == "queue-table":
            self._request("queue_play", {"index": message.cursor_row})
        elif table_id == "playlists-table":
            pane = self.query_one(PlaylistsPane)
            if pane.new_selected():
                pane.prompt_new()
                return
            if self._armed is not None:
                self._add_armed_to_highlighted()  # Enter drops the armed song in
                return
            playlist_id = pane.selected_playlist_id()
            if playlist_id is not None:
                self._request_async(
                    "playlist_play", {"playlist_id": playlist_id}, then=self._set_queue,
                )

    def on_now_playing_seek_requested(self, message: NowPlaying.SeekRequested):
        self._request("seek", {"seconds": message.seconds, "absolute": True})

    # -- actions ---------------------------------------------------------

    def action_focus_search(self):
        self.query_one("#search-input", Input).focus()

    def action_focus_results(self):
        pane = self.query_one(PlaylistsPane)
        if pane.query_one("#playlist-name", Input).display:
            pane.close_prompt()  # Escape while naming a playlist cancels it
            return
        if self._armed is not None:
            self._disarm()  # Escape while choosing a playlist cancels the add
            return
        table = self.query_one("#search-results", DataTable)
        if not table.display:
            # compact layout: the results table is hidden, so the queue is
            # the list to hand focus to
            self.query_one("#queue-table", DataTable).focus()
            return
        table.focus()

    def on_descendant_focus(self, message):
        # remember which list the user was last in, so `a` adds the track
        # they were looking at even after `l` moved focus to playlists.
        # (Focus, not RowHighlighted: a queue refresh re-adds rows and fires
        # highlights the user never made.)
        widget_id = getattr(message.widget, "id", None)
        if widget_id in ("search-results", "queue-table"):
            self._pick_pane = widget_id

    def _create_playlist(self, title):
        pane = self.query_one(PlaylistsPane)
        pane.close_prompt()
        if not title.strip():
            return

        def created(data):
            self.notify(f"Created playlist {data.get('title') or title}")
            self._refresh_playlists()

        self._request_async("playlist_create", {"title": title.strip()}, then=created)

    @staticmethod
    def _track_args(track):
        return {
            "video_id": track.get("video_id"),
            "title": track.get("title"),
            "artist": track.get("artist"),
            "album": track.get("album"),
            "duration": track.get("duration"),
            "duration_seconds": track.get("duration_seconds"),
            "thumbnail": track.get("thumbnail"),
        }

    def _selected_track_args(self):
        """The track the user means: the highlighted row of the list they were
        last in (queue or search results), else the search selection, else
        the queue cursor (which follows the playing track until moved), else
        whatever is playing."""
        panes = {"search-results": SearchPane, "queue-table": QueuePane}
        order = [getattr(self, "_pick_pane", None), "search-results", "queue-table"]
        for pane_id in order:
            if pane_id in panes:
                track = self.query_one(panes[pane_id]).selected_track()
                if track is not None:
                    return self._track_args(track)
        status = self._request("status") or {}
        current = status.get("current")
        return self._track_args(current) if current else None

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

    def action_play_next_selected(self):
        """Put the highlighted song right after the one playing."""
        args = self._selected_track_args()
        if args is None:
            return
        self._request("enqueue_next", args)
        self.notify(f"Up next: {args.get('title') or 'track'}", timeout=3)

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

    def action_refresh_mixes(self):
        """Ask YouTube for today's mixes again. Until then a mix plays the
        same tracklist every time, so what the pane showed is what you get."""
        def done(data):
            self.query_one(PlaylistsPane).set_playlists(data)
            self.notify("Mixes refreshed", timeout=3)
        self._request_async("mixes_refresh", then=done)

    def action_add_to_playlist(self):
        """Two-step add: `a` on a song arms it and jumps to the playlists pane;
        up/down picks a playlist; `a` (or Enter) drops it in. Escape cancels.
        `a` in the playlists pane with nothing armed adds the song the user
        was last on, so the old one-key flow still works."""
        pane = self.query_one(PlaylistsPane)
        if self._in_playlists():
            if pane.new_selected():
                pane.prompt_new()
                return
            self._add_armed_to_highlighted()
            return
        track_args = self._selected_track_args()
        if track_args is None:
            self._show_error("nothing to add: highlight a track or play one")
            return
        self._armed = track_args
        self._return_to = self.focused
        pane.arm(track_args.get("title") or "track")
        self.query_one("#playlists-table", DataTable).focus()

    def _in_playlists(self):
        return getattr(self.focused, "id", None) == "playlists-table"

    def _disarm(self):
        self._armed = None
        self.query_one(PlaylistsPane).disarm()
        target = getattr(self, "_return_to", None)
        self._return_to = None
        if target is not None and target.is_attached:
            target.focus()

    def _add_armed_to_highlighted(self):
        """Add the armed song (or, with none armed, the last-picked one) to
        the playlist under the cursor."""
        pane = self.query_one(PlaylistsPane)
        playlist_id = pane.selected_playlist_id()
        if playlist_id is None:
            self._show_error("highlight a playlist first, then press a")
            return
        track_args = self._armed or self._selected_track_args()
        if track_args is None:
            self._show_error("nothing to add: highlight a track or play one")
            return

        def added(data):
            self.notify(f"Added {track_args.get('title') or 'track'} to {pane.title_of(playlist_id) or 'playlist'}")
            pane.set_count(playlist_id, (data or {}).get("track_count"))
            self._refresh_playlists()

        self._request_async(
            "playlist_add",
            {
                "playlist_id": playlist_id,
                "video_ids": [track_args["video_id"]],
                "tracks": [track_args],
            },
            then=added,
        )
        if self._armed is not None:
            self._disarm()

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
