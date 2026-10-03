"""The Textual TUI root app: composes the panes, owns the Client, and wires
daemon events to widget updates without ever polling.
"""

import asyncio
import collections
import os
import signal
import sys
import threading
import time
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import DataTable, Input, Static

from ytm import config as config_mod
from ytm import diagnostics, update
from ytm.lifecycle import daemon_call
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


#: the TUI keeps a per-run trace of the keys it received, focus moves,
#: backend requests with their timing, errors and resizes. A keyboard or
#: focus problem in one particular terminal cannot be reproduced from a bug
#: report alone; this is the report. See `ytm.diagnostics` for the bounded
#: per-run history and the YTM_TUI_LOG override.


def _trace(line, mode="a"):
    """Append one line to this run's trace; never let logging break the app.

    The file is per-run and bounded (see `ytm.diagnostics`), so a failed
    launch is still readable after the restart that recovered from it.
    YTM_TUI_LOG keeps its meaning: one explicit file, truncated each start.
    """
    diagnostics.write(line, mode=mode)


class RequestDone(Message):
    """A background request finished; `then` runs on the message loop.

    `owner` names the operation that may clear an error the request reports;
    `accept` may veto the completion entirely (a stale generation), and
    `on_error` lets the requester repair its own widget state (a preserved
    draft, say) without touching the shared banner rules.
    """

    def __init__(self, cmd, data, error, then, owner=None, accept=None, on_error=None):
        super().__init__()
        self.cmd = cmd
        self.data = data
        self.error = error
        self.then = then
        self.owner = owner
        self.accept = accept
        self.on_error = on_error


class ControlWork:
    """One player command queued for the single control worker (UI-10)."""

    __slots__ = ("args", "cmd", "owner", "then")

    def __init__(self, cmd, args, then, owner):
        self.cmd = cmd
        self.args = args
        self.then = then
        self.owner = owner


class ControlDone(Message):
    """A control command finished; its `then` runs on the message loop."""

    def __init__(self, work, data, error):
        super().__init__()
        self.work = work
        self.data = data
        self.error = error


class LyricsFetched(Message):
    """The result of a background `lyrics` request, handed back to the
    Textual message loop the same way `DaemonEvent` is.

    `generation` says which `_fetch_lyrics` call this answers: the video id
    alone cannot tell a stale request for a song from a newer one for the
    same song (A -> B -> A), and a late error from the first must not
    overwrite the lyrics the second already showed.
    """

    def __init__(self, generation, video_id, data, error):
        super().__init__()
        self.generation = generation
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

    BINDINGS: ClassVar = [
        ("/", "focus_search", "Search"),
        ("s", "focus_search", "Search"),
        ("h", "toggle_search", "Hide search"),
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
        Binding("down", "leave_search", "Results", show=False),
        ("e", "quit_only", "Exit"),
        Binding("ctrl+c", "quit_only", "Exit", priority=True, show=False),
        ("x", "quit_and_shutdown", "Exit + stop player"),
    ]

    def __init__(self, client=None, config=None):
        super().__init__()
        self._client_error = None
        self._volume = 100
        self._armed = None  # song chosen with A, waiting for a playlist
        self._return_to = None
        self._search_timer = None  # pending debounced live search
        #: rises on every search-box edit. A dispatched request may only
        #: render results, show an error or start playback while both the
        #: generation and the normalized query still match (UI-01).
        self._search_generation = 0
        self._search_query = ""  # the normalized query the input holds now
        self._submit_intent = None  # (generation, query) still allowed to autoplay
        self._results_query = None  # query whose results the table shows
        #: the operation that owns the banner error; unrelated successes must
        #: not clear it (UI-06)
        self._error_owner = None
        #: rises on every observed queue event or snapshot request, so an old
        #: response cannot repaint a newer queue (UI-14)
        self._queue_generation = 0
        #: rises on every playlist listing request (UI-07)
        self._playlists_generation = 0
        #: one drain worker executes control commands in submission order;
        #: `volume` values waiting behind a held call coalesce to the newest
        self._control_lock = threading.Lock()
        self._control_queue = collections.deque()
        self._control_running = False
        self._control_closed = False
        self._queued_volume = None
        self._config = config if config is not None else config_mod.load()
        self._bindings = BindingsMap(self._build_bindings(self._config["keys"]))
        self.theme = self._resolve_theme(self._config["ui"]["theme"])
        self.client = client
        self._backend_factory = Backend if client is None else None
        self._terminal_poll = None
        self._shutdown_event = threading.Event()
        self._listener_thread = None
        # results from background work are applied only while this is set;
        # `_begin_shutdown` clears it before the widgets go away
        self._accepting_results = True
        self._lyrics_video_id = None
        self._lyrics_generation = 0   # rises per `_fetch_lyrics`; see LyricsFetched
        # one lyrics fetch at a time, and at most one waiting behind it:
        # skipping through ten songs must not open ten connections, and only
        # the song now playing is worth asking about
        self._lyrics_lock = threading.Lock()
        self._lyrics_pending = None   # (generation, video_id) not yet started
        self._lyrics_thread = None
        self._queue_timer = None      # pending coalesced queue redraw
        self._pending_queue = None    # the payload it will render
        self._last_queue_render = 0.0

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
            Binding("ctrl+c", "quit_only", "Exit", priority=True, show=False),
            ("h", "toggle_search", "Hide search"),
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
            # the unshifted `+` key on most layouts; `=` is never typed in a
            # search, so it is safe to take everywhere too
            Binding("equals_sign", "volume_up", "Vol +", show=False, priority=True),
            ("tab", "cycle_pane", "Cycle panes"),
            Binding("escape", "focus_results", "Results", show=False),
            # Down from the search box goes to the lists, like Escape; the
            # tables handle their own Down first, so this only fires there
            Binding("down", "leave_search", "Results", show=False),
            # no priority on any letter key: while the search box has focus
            # every letter is text, so "queen" or "eels" can be searched
            (keys["quit"], "quit_only", "Exit"),
            ("x", "quit_and_shutdown", "Exit + stop player"),
        ]

    #: what the shortcut bar leads with while the search box has focus:
    #: every letter is text there, so the letter shortcuts are inert until
    #: Esc or Down hands focus to a list
    TYPING_HINT = "[@click=app.focus_results][b]Esc[/b]/[b]↓[/b] leave search, then:[/]  "

    @staticmethod
    def _shortcut_text(keys, typing=False):
        """One line naming every shortcut, in the order people reach for them.

        Each entry is also a mouse target: clicking it runs the same action
        the key would. With `typing` (the search box has focus) it opens
        with how to get out, because until then `l`, `q`, `a`... are letters.
        """
        def link(key, label, action):
            return f"[@click=app.{action}][b]{key}[/b] {label}[/]"

        search_key = f"{keys['search']} {'s' if keys['search'] != 's' else ''}".strip()
        return (YTMApp.TYPING_HINT if typing else "") + "  ".join([
            link(keys["quit"], "exit", "quit_only"),
            link(search_key, "search", "focus_search"),
            link(keys["toggle"], "play/pause", "toggle"),
            link(keys["next"], "next", "next"),
            link(keys["prev"], "prev", "prev"),
            link("q", "enqueue", "enqueue_selected"),
            link("u", "play next", "play_next_selected"),
            "[@click=app.seek_back][b]←[/b][/]/[@click=app.seek_forward][b]→[/b][/] seek",
            "[@click=app.volume_up][b]+[/b][/]/[@click=app.volume_down][b]-[/b][/] volume",
            link("l", "playlists", "focus_playlists"),
            link("a", "add to playlist", "add_to_playlist"),
            link("r", "refresh mixes", "refresh_mixes"),
            link("h", "hide search", "toggle_search"),
            link("Tab", "panes", "cycle_pane"),
            link("x", "exit+stop", "quit_and_shutdown"),
        ])

    def check_action(self, action, parameters):
        # the seek arrows are priority bindings; while the search box has
        # focus they must move the text cursor instead
        return not (action in ("seek_back", "seek_forward") and isinstance(self.focused, Input))

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
        yield Static(self._shortcut_text(self._config["keys"], typing=True), id="shortcut-bar")

    @staticmethod
    def _visible(widget):
        """Whether `widget` and every ancestor are displayed.

        A child of a hidden pane still reports its own `display` as True;
        focusing it would take the keyboard somewhere the user cannot see.
        """
        node = widget
        while node is not None:
            if not node.display:
                return False
            node = node.parent
        return True

    def on_resize(self, event):
        """Small terminals lose the results table, playlists and lyrics."""
        size = event.size
        _trace(f"resize {size.width}x{size.height}")
        compact = size.width < self.COMPACT_WIDTH or size.height < self.COMPACT_HEIGHT
        was_compact = self.screen.has_class("compact")
        self.screen.set_class(compact, "compact")
        if compact:
            if not was_compact:
                # entering the one-row layout: the queue shows unless a
                # playlist draft is open, which must stay visible (UI-09/12)
                prompt = self.query_one("#playlist-name", Input)
                self._show_playlists_in_compact(prompt.display)
        elif was_compact:
            # back to the full layout: both panes are on screen again
            self.query_one("#queue-pane").display = True
            self.query_one("#playlists-pane").display = True
        if compact and self.focused is not None and not self._visible(self.focused):
            self._focus_fallback()

    def _show_playlists_in_compact(self, show):
        """Swap the one middle row a compact layout has between the panes."""
        self.query_one("#playlists-pane").display = show
        self.query_one("#queue-pane").display = not show

    def _focus_fallback(self):
        """Focus the search box, or the queue when that pane is hidden."""
        search = self.query_one("#search-input", Input)
        if self._visible(search):
            search.focus()
        else:
            self.query_one("#queue-table", DataTable).focus()

    def on_mount(self):
        from ytm.update import installed_version

        _trace(f"ytm {installed_version()} started, size {self.size.width}x{self.size.height}", mode="w")
        if self._backend_factory is not None:
            try:
                self.client = self._backend_factory(stop_event=self._shutdown_event)
            except BackendError as exc:
                self._client_error = str(exc)
        if self._terminal_poll is not None:
            self.set_interval(0.2, self._check_terminal)
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
            self.run_worker(daemon_call(self._check_for_update), name="update-check", group="update")

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
        owner = getattr(self.client, "_owner", None)
        kwargs = ({"run": owner.run, "verify": lambda: update.installed_version_now(run=owner.run)}
                  if owner is not None else {})
        ok, text = update.upgrade(target=latest, **kwargs)
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
    #: queue redraws are coalesced to at most one per this long. Loading a
    #: playlist changes mpv's playlist once per track, and each change
    #: re-renders every row of the queue pane and the now-playing columns;
    #: a 100-track playlist used to redraw the whole table a hundred times.
    QUEUE_REDRAW_INTERVAL = 0.15
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
        if not self._accepts_results():
            return
        # events are flowing again: the reconnect that answers a lost
        # listener clears that error (UI-06)
        self._clear_error("daemon")
        self._apply_event(message.event, message.data)

    # -- lifecycle -------------------------------------------------------

    def _accepts_results(self):
        """Whether a background completion may still touch the widgets.

        Textual's shutdown clears `is_running` first, then removes the
        screens, and only then drains the messages still queued -- so a
        `RequestDone` or `LyricsFetched` that arrives during teardown would
        find no `#error-banner` or lyrics pane to update. Every handler for
        background work checks here before it queries a widget.
        """
        return self._accepting_results and self.is_running and not self._exit

    def _begin_shutdown(self):
        """Stop accepting results and release what the app owns. Idempotent,
        and reached from both quit keys and from the framework's own unmount,
        so a test's `run_test()` exit takes the same path as `e`/`x`.

        Cancelling a worker does not interrupt a blocking HTTP call; the
        client's 30 s request timeout ends those, and by then the result is
        simply refused here.
        """
        if not self._accepting_results:
            return
        self._accepting_results = False
        self._shutdown_event.set()
        self._lyrics_generation += 1
        with self._lyrics_lock:
            self._lyrics_pending = None
        # queued control commands are dropped; a command already executing
        # finishes but its completion is refused by `_accepts_results`
        self._control_closed = True
        with self._control_lock:
            self._control_queue.clear()
            self._queued_volume = None
        self.workers.cancel_group(self, "requests")
        self.workers.cancel_group(self, "control")
        self.workers.cancel_group(self, "update")
        self.workers.cancel_group(self, "art")
        if self.client is not None:
            self.client.close()
        if self._listener_thread is not None and self._listener_thread is not threading.current_thread():
            self._listener_thread.join(1.5)

    def on_unmount(self):
        self._begin_shutdown()

    def _check_terminal(self):
        if self._terminal_poll.poll(0):
            self.action_quit_only()

    async def run_async(self, **kwargs):
        """Route signals through Textual's normal teardown, with a final safety net."""
        loop = asyncio.get_running_loop()
        previous = {}
        terminal = None
        if threading.current_thread() is threading.main_thread():
            def interrupted(signum, frame):
                self._shutdown_event.set()
                loop.call_soon_threadsafe(self.exit)
            for name in ("SIGHUP", "SIGTERM", "SIGINT"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    previous[sig] = signal.signal(sig, interrupted)
        try:
            if os.name == "posix" and sys.stdin.isatty():
                import select
                import termios
                fd = sys.stdin.fileno()
                terminal = (fd, termios.tcgetattr(fd))
                self._terminal_poll = select.poll()
                self._terminal_poll.register(fd, select.POLLHUP | select.POLLERR | select.POLLNVAL)
            return await super().run_async(**kwargs)
        finally:
            try:
                self._begin_shutdown()
            finally:
                # Textual restores raw mode, cursor and alternate screen. The
                # saved attributes also cover failures during driver startup.
                if terminal is not None:
                    try:
                        termios.tcsetattr(terminal[0], termios.TCSANOW, terminal[1])
                    except termios.error:
                        pass  # the terminal may already have disappeared
                for sig, handler in previous.items():
                    signal.signal(sig, handler)

    # -- lyrics ----------------------------------------------------------

    def _fetch_lyrics(self, video_id):
        """Show `video_id`'s lyrics: reset the pane now, fetch in the background.

        The request blocks on a background thread so a slow/unresponsive
        daemon never freezes the UI, and the result is marshalled back onto
        Textual's own loop via `LyricsFetched`. One thread serves all lyrics
        requests, taking the newest one waiting: a burst of track changes
        fetches the first and the last, never the ones skipped in between.
        """
        self._lyrics_generation += 1
        generation = self._lyrics_generation
        self._lyrics_video_id = video_id
        self.query_one(LyricsPane).reset(video_id)
        if video_id is None or self.client is None or not self._accepts_results():
            return
        with self._lyrics_lock:
            self._lyrics_pending = (generation, video_id)
            if self._lyrics_thread is None:
                self._lyrics_thread = threading.Thread(
                    target=self._lyrics_worker, daemon=True, name="ytm-lyrics"
                )
                self._lyrics_thread.start()

    def _lyrics_worker(self):
        while True:
            with self._lyrics_lock:
                job = self._lyrics_pending
                self._lyrics_pending = None
                if job is None:
                    self._lyrics_thread = None
                    return
            generation, video_id = job
            _trace(f"request lyrics {video_id!r}")
            started = time.monotonic()
            try:
                data = self.client.request("lyrics", {"video_id": video_id})
            except BackendError as exc:
                _trace(f"request lyrics failed after {time.monotonic() - started:.1f}s: {exc}")
                self.post_message(LyricsFetched(generation, video_id, None, str(exc)))
            else:
                _trace(f"request lyrics done in {time.monotonic() - started:.1f}s")
                self.post_message(LyricsFetched(generation, video_id, data, None))

    def on_lyrics_fetched(self, message: LyricsFetched):
        # only the newest request owns the pane: a later track change (or a
        # return to the same track) supersedes anything still in flight
        if not self._accepts_results():
            return
        if message.generation != self._lyrics_generation or message.video_id != self._lyrics_video_id:
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
            if (data or {}).get("video_id") == self._lyrics_video_id:
                self.query_one(LyricsPane).set_position((data or {}).get("position") or 0)
        elif event == "state_changed":
            now_playing.on_state_changed(data)
            volume = (data or {}).get("volume")
            if volume is not None:
                self._volume = volume
        elif event == "queue_changed":
            self._queue_changed(data)
        elif event == "playback_error":
            self._show_error(
                "Could not play this track. Try another track or retry playback.",
                owner="playback",
            )
        elif event == "error":
            self._show_error((data or {}).get("error") or "daemon error", owner="daemon")

    # -- helpers ---------------------------------------------------------

    def on_key(self, event):
        # never consumes the key: this is the trace, the bindings do the work
        _trace(f"key {event.key!r} focus={getattr(self.focused, 'id', None)}")

    def _show_error(self, message, owner=None):
        """Show `message` literally on the banner; `owner` says who clears it.

        Backend strings can contain square brackets (playlist names, provider
        messages), so the text is rendered as a Rich `Text` rather than through
        the banner's markup parser. `owner` records the operation a later
        success must belong to before it may hide this error (UI-06); the
        newest error takes precedence and replaces whatever came before.
        """
        _trace(f"error {message!r}")
        self._error_owner = owner
        banner = self.query_one("#error-banner", Static)
        banner.update(Text(f"error: {message}"))
        banner.display = True

    def _clear_error(self, owner=None):
        """Hide the banner. With `owner`, only when that operation owns it.

        Called with no owner for an explicit dismissal or a state transition
        that resolves every error; unrelated polling and refreshes pass their
        own owner and leave somebody else's error alone.
        """
        if owner is not None and owner != self._error_owner:
            return
        self._error_owner = None
        banner = self.query_one("#error-banner", Static)
        banner.update("")
        banner.display = False

    #: commands whose success proves the player is answering again: they own
    #: one shared playback error, separate from search/playlist failures
    PLAYBACK_COMMANDS: ClassVar = frozenset({
        "play", "enqueue", "enqueue_next", "pause", "resume", "toggle", "next",
        "prev", "seek", "volume", "queue_play", "queue_clear", "queue_remove",
        "queue_move", "radio",
    })

    @classmethod
    def _owner_of(cls, cmd):
        return "playback" if cmd in cls.PLAYBACK_COMMANDS else cmd

    def _request(self, cmd, args=None):
        """Send one *local* read (a status query) and surface a `BackendError`
        as a visible banner. Control commands go through `_request_control`
        so a slow player can never freeze the event loop (UI-10); anything
        that goes to the network must use `_request_async`."""
        if self.client is None:
            return None
        owner = self._owner_of(cmd)
        try:
            data = self.client.request(cmd, args)
        except BackendError as exc:
            self._show_error(str(exc), owner=owner)
            return None
        self._clear_error(owner)
        return data

    def _request_async(self, cmd, args=None, then=None, *, owner=None, accept=None, on_error=None):
        """Run a request on a worker thread; `then(data)` runs back on the
        message loop once it completes. Keeps search and playlist calls --
        the ones that hit YouTube -- off the UI thread (lyrics have their
        own thread, see `_fetch_lyrics`).

        `accept` is checked on the message loop before anything (including an
        error) touches the UI; a stale callback is dropped whole. `owner`
        decides which later success may clear an error this request reports,
        and `on_error` lets the requester restore its own widget state.
        """
        if self.client is None or not self._accepts_results():
            return
        if owner is None:
            owner = self._owner_of(cmd)

        _trace(f"request {cmd} {args!r}")

        def work():
            started = time.monotonic()
            try:
                data = self.client.request(cmd, args)
            except BackendError as exc:
                _trace(f"request {cmd} failed after {time.monotonic() - started:.1f}s: {exc}")
                self.post_message(RequestDone(cmd, None, str(exc), then, owner, accept, on_error))
            else:
                _trace(f"request {cmd} done in {time.monotonic() - started:.1f}s")
                self.post_message(RequestDone(cmd, data, None, then, owner, accept, on_error))

        self.run_worker(daemon_call(work), name=cmd, group="requests")

    def on_request_done(self, message: RequestDone):
        if not self._accepts_results():
            return
        if message.accept is not None and not message.accept():
            return  # superseded: not even its error may touch the UI
        if message.error is not None:
            self._show_error(message.error, owner=message.owner)
            if message.on_error is not None:
                message.on_error(message.error)
            return
        self._clear_error(message.owner)
        if message.then is not None:
            message.then(message.data)

    # -- control commands (UI-10) ----------------------------------------

    def _request_control(self, cmd, args=None, then=None):
        """Run one player command off the UI thread, in submission order.

        A single drain worker executes queued commands one at a time, so
        play/pause/skip/queue mutations keep their order without ever
        blocking the keyboard. Rapid volume updates collapse into the newest
        queued level. The worker is a daemon thread driven by a Textual
        worker, so `quit` stays responsive and a blocked call cannot keep
        the process alive.
        """
        if self.client is None or not self._accepts_results():
            return
        work = ControlWork(cmd, args, then, self._owner_of(cmd))
        with self._control_lock:
            if cmd == "volume" and self._queued_volume is not None:
                # the newest level replaces the queued one, so stale volumes
                # do not pile up behind a held call
                self._queued_volume.args = args
                self._queued_volume.then = then
                return
            self._control_queue.append(work)
            if cmd == "volume":
                self._queued_volume = work
            start = not self._control_running
            if start:
                self._control_running = True
        if start:
            self.run_worker(
                daemon_call(self._drain_control),
                name="control", group="control", exit_on_error=False,
            )

    def _drain_control(self):
        """The control worker: FIFO, one request at a time, until empty."""
        while True:
            with self._control_lock:
                if self._control_closed or not self._control_queue:
                    self._control_running = False
                    return
                work = self._control_queue.popleft()
                if work is self._queued_volume:
                    self._queued_volume = None
            started = time.monotonic()
            _trace(f"control {work.cmd} {work.args!r}")
            try:
                data = self.client.request(work.cmd, work.args)
            except Exception as exc:
                # a control command must never take the whole app down:
                # anything the backend did not wrap is reported like a
                # BackendError, and the next command still runs
                error = exc if isinstance(exc, BackendError) else BackendError(
                    f"{type(exc).__name__}: {exc}"
                )
                _trace(f"control {work.cmd} failed after {time.monotonic() - started:.1f}s: {error}")
                self.post_message(ControlDone(work, None, str(error)))
            else:
                _trace(f"control {work.cmd} done in {time.monotonic() - started:.1f}s")
                self.post_message(ControlDone(work, data, None))

    def on_control_done(self, message: ControlDone):
        if not self._accepts_results():
            return
        work = message.work
        if message.error is not None:
            self._show_error(message.error, owner=work.owner)
            return
        self._clear_error(work.owner)
        if work.then is not None:
            work.then(message.data)

    def _set_queue(self, data):
        self.query_one(QueuePane).set_queue(data)
        self.query_one(NowPlaying).set_queue(data)

    def _queue_changed(self, data):
        """Render a pushed queue change, at most once per redraw interval.

        The first change is drawn straight away; a burst behind it is drawn
        once, when the interval is up, with whatever arrived last. Every push
        also rises the generation, so a snapshot requested earlier can no
        longer repaint the queue when its answer arrives (UI-14).
        """
        self._queue_generation += 1
        self._pending_queue = data
        if self._queue_timer is not None:
            return
        waited = time.monotonic() - self._last_queue_render
        if waited >= self.QUEUE_REDRAW_INTERVAL:
            self._flush_queue()
            return
        self._queue_timer = self.set_timer(
            self.QUEUE_REDRAW_INTERVAL - waited, self._flush_queue, name="queue-redraw"
        )

    def _flush_queue(self):
        self._queue_timer = None
        data, self._pending_queue = self._pending_queue, None
        if data is None or not self._accepts_results():
            return
        self._last_queue_render = time.monotonic()
        self._set_queue(data)

    def _queue_snapshot(self, generation, data):
        """Apply a requested queue snapshot unless a newer event was seen."""
        if generation != self._queue_generation:
            return
        # a push still waiting to be flushed was observed before this
        # snapshot was asked for, so the answer supersedes it
        self._pending_queue = None
        if self._queue_timer is not None:
            self._queue_timer.stop()
            self._queue_timer = None
        self._set_queue(data)

    def _refresh_queue(self):
        self._queue_generation += 1
        generation = self._queue_generation
        self._request_async(
            "queue_get",
            then=lambda data: self._queue_snapshot(generation, data),
            owner="queue",
            accept=lambda: self._queue_generation == generation,
        )

    def _apply_playlists(self, data):
        """Render a playlist listing, partial failures included (UI-07).

        Local playlists stay usable when the account or network part failed,
        and the failure is reported instead of a success message. A full
        success clears a previous listing error.
        """
        pane = self.query_one(PlaylistsPane)
        pane.set_playlists(data)
        error = (data or {}).get("error")
        if error:
            self._show_error(error, owner="playlists")
            return False
        self._clear_error("playlists")
        return True

    def _request_playlists(self, cmd, then=None):
        """Dispatch a listing request; only the newest may paint the pane."""
        self._playlists_generation += 1
        generation = self._playlists_generation
        self._request_async(
            cmd, then=then, owner="playlists",
            accept=lambda: self._playlists_generation == generation,
        )

    def _refresh_playlists(self):
        self._request_playlists("playlist_list", then=self._apply_playlists)

    # -- search --------------------------------------------------------

    def on_input_changed(self, message: Input.Changed):
        """Search as you type: results appear after a short pause in typing,
        no Enter needed. Enter still plays the first result.

        Every edit is a new generation (UI-01): once the input has moved on,
        an outstanding request may not render results, show an error or start
        playback -- even while the next request is still only debounced.
        """
        if message.input.id != "search-input":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None
        self._submit_intent = None
        self._search_generation += 1
        generation = self._search_generation
        query = message.value.strip()
        self._search_query = query
        if len(query) < self.SEARCH_MIN_CHARS:
            # nothing the table shows can belong to a query this short
            self._results_query = None
            self.query_one(SearchPane).set_results([])
            return
        self._search_timer = self.set_timer(
            self.SEARCH_DEBOUNCE, lambda: self._live_search(generation, query), name="live-search"
        )

    def _search_is_current(self, generation, query):
        """Whether `(generation, query)` still describes the search box."""
        return generation == self._search_generation and query == self._search_query

    def _live_search(self, generation, query):
        """Fetch results for `query`; only the current edit may show them."""
        self._search_timer = None
        if not self._search_is_current(generation, query):
            return
        self._request_async(
            "search", {"query": query},
            then=lambda data: self._show_search_results(generation, query, data),
            accept=lambda: self._search_is_current(generation, query),
        )

    def _show_search_results(self, generation, query, data):
        if not self._search_is_current(generation, query):
            return False  # stale: the user has edited since
        tracks = (data or {}).get("tracks") or []
        self.query_one(SearchPane).set_results(tracks)
        self._results_query = query
        return True

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
                self._request_control("play", self._track_args(tracks[0]))
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
        generation = self._search_generation
        # separate from display validity: an answer may still be worth showing
        # without keeping permission to start playback (UI-01)
        self._submit_intent = (generation, query)

        def show(data):
            if not self._show_search_results(generation, query, data):
                return
            if self._submit_intent == (generation, query):
                self._submit_intent = None
                play_first((data or {}).get("tracks") or [])

        self._request_async(
            "search", {"query": query}, then=show,
            accept=lambda: self._search_is_current(generation, query),
        )

    def on_data_table_row_selected(self, message: DataTable.RowSelected):
        """Enter or a mouse click on any of the three tables."""
        table_id = message.data_table.id
        _trace(f"selected {table_id} row={message.cursor_row} key={getattr(message.row_key, 'value', None)!r}")
        if table_id == "search-results":
            self.action_play_selected()
        elif table_id == "queue-table":
            args = self.query_one(QueuePane).play_args(message.row_key.value)
            if args is not None:
                self._request_control("queue_play", args)
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
                generation = self._queue_generation
                self._request_async(
                    "playlist_play", {"playlist_id": playlist_id},
                    then=lambda data: self._queue_snapshot(generation, data),
                    accept=lambda: self._queue_generation == generation,
                )

    def on_now_playing_seek_requested(self, message: NowPlaying.SeekRequested):
        self._request_control("seek", {"seconds": message.seconds, "absolute": True})

    # -- actions ---------------------------------------------------------

    def action_focus_search(self):
        self.screen.remove_class("search-hidden")  # `s` / `/` bring a hidden pane back
        self.query_one("#search-input", Input).focus()

    def action_toggle_search(self):
        """Hide the search box and results (or show them again) for a
        cleaner screen while just listening. Not remembered across runs."""
        hidden = not self.screen.has_class("search-hidden")
        self.screen.set_class(hidden, "search-hidden")
        pane = self.query_one("#search-pane")
        if hidden and self.focused is not None and pane in self.focused.ancestors_with_self:
            self.query_one("#queue-table", DataTable).focus()

    def action_focus_results(self):
        pane = self.query_one(PlaylistsPane)
        if pane.query_one("#playlist-name", Input).display:
            pane.close_prompt()  # Escape while naming a playlist cancels it
            return
        if self._armed is not None:
            self._disarm()  # Escape while choosing a playlist cancels the add
            return
        table = self.query_one("#search-results", DataTable)
        if not self._visible(table):
            # compact or hidden search: the results are not on screen, so the
            # queue is the list to hand focus to
            self.query_one("#queue-table", DataTable).focus()
            return
        table.focus()

    def on_descendant_focus(self, message):
        # remember which list the user was last in, so `a` adds the track
        # they were looking at even after `l` moved focus to playlists.
        # (Focus, not RowHighlighted: a queue refresh re-adds rows and fires
        # highlights the user never made.)
        widget_id = getattr(message.widget, "id", None)
        _trace(f"focus -> {widget_id}")
        if widget_id in ("search-results", "queue-table"):
            self._pick_pane = widget_id
        if widget_id not in ("playlists-table", "playlist-name"):
            # compact layouts show one middle pane; focus leaving the
            # playlists puts the queue back (UI-12)
            self._restore_compact_queue()
        self.query_one("#shortcut-bar", Static).update(
            self._shortcut_text(self._config["keys"], typing=widget_id == "search-input")
        )

    def _restore_compact_queue(self):
        """Put the queue back on the one middle row compact layouts have."""
        if not self.screen.has_class("compact"):
            return
        playlists_pane = self.query_one("#playlists-pane")
        if not playlists_pane.display:
            return
        playlists_pane.display = False
        self.query_one("#queue-pane").display = True

    def action_leave_search(self):
        """Down in the search box: hand focus to the lists, as Escape does.
        Anywhere else Down already moved a table cursor and never gets here."""
        if getattr(self.focused, "id", None) == "search-input":
            self.action_focus_results()

    def _create_playlist(self, title):
        """Create the playlist named in the prompt, keeping the draft safe.

        The prompt is disabled while the request is in flight, so a second
        Enter cannot create a duplicate. It only closes and clears on
        confirmed success; a failure leaves the exact text editable and
        focused (UI-09). A completion belongs to the prompt session it was
        submitted from, so a late answer can never erase a newer draft.
        """
        pane = self.query_one(PlaylistsPane)
        if pane.prompt_pending():
            return  # a create from this draft is already in flight
        session = pane.prompt_session()
        title = title.strip()
        if not title:
            self._show_error("a playlist needs a name", owner="playlist_create")
            return
        pane.set_prompt_pending(True)

        def created(data):
            if pane.prompt_session() == session:
                pane.close_prompt()
            self.notify(f"Created playlist {data.get('title') or title}")
            self._refresh_playlists()

        def failed(message):
            if pane.prompt_session() != session:
                return  # cancelled (or a newer draft took over): leave it be
            pane.set_prompt_pending(False)
            pane.focus_prompt()

        self._request_async(
            "playlist_create", {"title": title},
            then=created, on_error=failed, owner="playlist_create",
        )

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
        self._request_control("play", args)

    def action_enqueue_selected(self):
        args = self._selected_track_args()
        if args is None:
            return
        self._request_control("enqueue", args)

    def action_play_next_selected(self):
        """Put the highlighted song right after the one playing.

        The title is captured now, so the toast names the track that was
        actually enqueued even if the cursor moves before the answer; the
        toast itself only runs on success (UI-05).
        """
        args = self._selected_track_args()
        if args is None:
            return
        title = args.get("title") or "track"
        self._request_control(
            "enqueue_next", args,
            then=lambda data: self.notify(f"Up next: {title}", timeout=3),
        )

    def action_toggle(self):
        self._request_control("toggle")

    def action_next(self):
        self._request_control("next")

    def action_prev(self):
        self._request_control("prev")

    def action_seek_back(self):
        self._request_control("seek", {"seconds": -SEEK_STEP})

    def action_seek_forward(self):
        self._request_control("seek", {"seconds": SEEK_STEP})

    def action_volume_up(self):
        self._set_volume(self._volume + VOLUME_STEP)

    def action_volume_down(self):
        self._set_volume(self._volume - VOLUME_STEP)

    def _set_volume(self, level):
        """Apply `level` now and confirm it off the UI thread.

        `_volume` moves immediately so repeated presses accumulate even
        while the player is still answering the first one; the completion
        reconciles it with what the player actually reports. Rapid updates
        coalesce into one queued command, so the last level still wins.
        """
        level = max(0, min(100, level))
        self._volume = level
        self._request_control(
            "volume", {"level": level},
            then=lambda data: self._volume_applied(level, data),
        )

    def _volume_applied(self, level, data):
        if data is not None:
            self._volume = data.get("volume", level)

    def action_focus_playlists(self):
        self._focus_playlists()

    def _focus_playlists(self):
        """Focus the playlist table, switching compact layouts to it.

        A visible table inside the hidden playlists pane is not a valid
        destination (UI-12): in a compact terminal the one middle row is
        swapped from the queue to the playlists, and an action that cannot
        reach it at all keeps focus on a visible control and explains why.
        """
        table = self.query_one("#playlists-table", DataTable)
        if not self._visible(table) and self.screen.has_class("compact"):
            self._show_playlists_in_compact(True)
        if not self._visible(table):
            self._show_error(
                "playlists are not shown in this layout; enlarge the terminal to manage them",
                owner="layout",
            )
            return False
        table.focus()
        self._clear_error("layout")
        return True

    def action_refresh_mixes(self):
        """Ask YouTube for today's mixes again. Until then a mix plays the
        same tracklist every time, so what the pane showed is what you get.

        A partial answer (local playlists plus an account/network error) is
        rendered and reported, not announced as a full success (UI-07)."""
        def done(data):
            if self._apply_playlists(data):
                self.notify("Mixes refreshed", timeout=3)
        self._request_playlists("mixes_refresh", then=done)

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
            self._show_error("nothing to add: highlight a track or play one", owner="playlist_add")
            return
        previous = self.focused
        if not self._focus_playlists():
            return  # compact: explained, and the song is not armed for a
                    # pane the user cannot see
        self._armed = track_args
        self._return_to = previous
        pane.arm(track_args.get("title") or "track")

    def _in_playlists(self):
        return getattr(self.focused, "id", None) == "playlists-table"

    def _disarm(self):
        self._armed = None
        self.query_one(PlaylistsPane).disarm()
        target = getattr(self, "_return_to", None)
        self._return_to = None
        if target is not None and target.is_attached and self._visible(target):
            target.focus()

    def _add_armed_to_highlighted(self):
        """Add the armed song (or, with none armed, the last-picked one) to
        the playlist under the cursor."""
        pane = self.query_one(PlaylistsPane)
        playlist_id = pane.selected_playlist_id()
        if playlist_id is None:
            self._show_error("highlight a playlist first, then press a", owner="playlist_add")
            return
        track_args = self._armed or self._selected_track_args()
        if track_args is None:
            self._show_error("nothing to add: highlight a track or play one", owner="playlist_add")
            return

        def added(data):
            # the row's count is updated in place; re-listing the library
            # (and a track count for every playlist in it) to learn one
            # number that the add already reported is a whole round of
            # requests for nothing
            self.notify(f"Added {track_args.get('title') or 'track'} to {pane.title_of(playlist_id) or 'playlist'}")
            data = data or {}
            # `added` is the known net membership increase; it is absent (or
            # zero) for an idempotent like, so the count never grows just
            # because a request succeeded (UI-08)
            pane.set_count(playlist_id, data.get("track_count"), added=int(data.get("added") or 0))

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
        self.screen.focus_next()

    def action_quit_only(self):
        self._begin_shutdown()
        self.exit()

    def action_quit_and_shutdown(self):
        self.action_quit_only()

    def action_quit(self):
        self.action_quit_only()


def run():
    """Launch the TUI."""
    YTMApp().run()


if __name__ == "__main__":
    run()
