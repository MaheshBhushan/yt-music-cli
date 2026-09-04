"""Now-playing pane: cover art, current track, progress bar and volume."""

import io
import urllib.request
from dataclasses import dataclass

from ytm import config as config_mod

from textual.containers import Container, Horizontal, Vertical
from textual.markup import escape
from textual.message import Message
from textual.widgets import ProgressBar, Static

# Imported at module level on purpose: textual-image probes the terminal for
# its cell size and graphics support when it loads, which only works before
# Textual takes the terminal over. Importing it inside compose() stalled the
# whole app for seconds. Only the TUI imports this module, never the CLI.
from textual_image.widget import (
    HalfcellImage, Image as AutoImage, SixelImage, TGPImage, UnicodeImage,
)

#: `[ui] art` values mapped onto textual-image widgets; None means no art.
#: "blocks" is the default: the pixel protocols are opt-in because Sixel in
#: Konsole drew nothing and froze repaints of the whole now-playing strip.
ART_RENDERERS = {
    "blocks": HalfcellImage,
    "auto": AutoImage,
    "sixel": SixelImage,
    "kitty": TGPImage,
    "ascii": UnicodeImage,
    "off": None,
}
DEFAULT_ART = "blocks"

#: how long to wait for a cover image before giving up on it
ART_TIMEOUT = 5.0

#: covers kept decoded in memory, so skipping back and forth does not refetch
ART_CACHE_SIZE = 20


def _format_time(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


#: title width in each played/up-next column
QUEUE_COLUMN_WIDTH = 16
QUEUE_COLUMN_MIN_WIDTH = 12
#: wider than this and UP NEXT drifts to the far edge of a wide terminal
DEFAULT_QUEUE_COLUMN_MAX_WIDTH = config_mod.DEFAULTS["tui"]["queue_column_width"]
QUEUE_ARTIST_MIN_WIDTH = 28
QUEUE_DEFAULT_HEIGHT = 8
QUEUE_RESERVED_ROWS = 4


@dataclass(frozen=True)
class QueueSummaryLayout:
    column_width: int
    track_count: int
    show_artist: bool


def _truncate(title, width=QUEUE_COLUMN_WIDTH):
    return title if len(title) <= width else title[:width - 1] + "…"


def queue_summary_layout(width=None, height=None, max_width=DEFAULT_QUEUE_COLUMN_MAX_WIDTH):
    """Return the played/up-next column shape for the available cells."""
    width = width or QUEUE_COLUMN_WIDTH * 2
    height = height or QUEUE_DEFAULT_HEIGHT
    half_width = max(1, width // 2)
    column_width = (
        max(QUEUE_COLUMN_MIN_WIDTH, half_width)
        if width >= QUEUE_COLUMN_MIN_WIDTH * 2
        else half_width
    )
    if max_width:
        column_width = min(max_width, column_width)
    track_count = max(1, height - QUEUE_RESERVED_ROWS)
    show_artist = column_width >= QUEUE_ARTIST_MIN_WIDTH and track_count > 1
    return QueueSummaryLayout(column_width, track_count, show_artist)


def queue_track_label(track, layout):
    title = track.get("title") or "Unknown Title"
    artist = (track.get("artist") or "").strip()
    if layout.show_artist and artist:
        return _truncate(f"{title} — {artist}", layout.column_width)  # same dash as the queue pane
    return _truncate(title, layout.column_width)


def split_queue(tracks, index, before=2, after=3):
    """Split `tracks` around `index` into the last `before` played tracks
    and the next `after` up-next tracks. Falls back to an empty split when
    there is no valid `index`."""
    tracks = tracks or []
    if index is None or not (0 <= index < len(tracks)):
        return [], []
    played = tracks[max(0, index - before):index]
    up_next = tracks[index + 1:index + 1 + after]
    return played, up_next


def fetch_bytes(url, timeout=ART_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


class AlbumArt(Container):
    """Cover art for the current track, rendered with textual-image.

    Everything slow happens off the UI thread: the image library is imported
    lazily on the first fetch (Pillow alone costs a few hundred ms), the
    download runs on a worker thread, and only the finished, decoded image
    is handed back to the message loop. A fetch that fails or arrives after
    the track has changed again is simply dropped.
    """

    def __init__(self, *args, fetcher=fetch_bytes, renderer=DEFAULT_ART, **kwargs):
        super().__init__(*args, **kwargs)
        self._fetcher = fetcher
        self._renderer = ART_RENDERERS.get(renderer, ART_RENDERERS[DEFAULT_ART])
        self._url = None
        self._cache = {}
        self._image_widget = None
        if self._renderer is None:
            self.display = False

    def compose(self):
        # mounted empty so it already has a laid-out size when the first
        # cover arrives
        if self._renderer is not None:
            self._image_widget = self._renderer(None, id="now-playing-image")
            yield self._image_widget

    @property
    def image(self):
        return self._image_widget.image if self._image_widget is not None else None

    def show(self, url):
        """Display the cover at `url`; blank when there is none."""
        self._url = url or None
        if self._renderer is None:
            return
        if not url:
            self._set_image(None)
            return
        if url in self._cache:
            self._set_image(self._cache[url])
            return

        def work():
            try:
                from PIL import Image as PILImage

                raw = self._fetcher(url)
                image = PILImage.open(io.BytesIO(raw))
                image.load()
            except Exception:
                image = None
            self.app.call_from_thread(self._arrived, url, image)

        self.app.run_worker(work, thread=True, name=f"art:{url}", group="art")

    def _arrived(self, url, image):
        if image is not None:
            if len(self._cache) >= ART_CACHE_SIZE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[url] = image
        if url != self._url:
            return  # superseded by a later track
        self._set_image(image)

    def _set_image(self, image):
        if self._image_widget is not None:
            self._image_widget.image = image


class NowPlaying(Vertical):
    """Track line, seek progress and volume, driven purely by player events.

    Mouse: clicking anywhere on the progress bar asks the app to seek there
    (`SeekRequested`).
    """

    class SeekRequested(Message):
        """The user clicked the progress bar at `seconds` into the track."""

        def __init__(self, seconds):
            super().__init__()
            self.seconds = seconds

    def __init__(
        self, *args, art=DEFAULT_ART, queue_column_width=DEFAULT_QUEUE_COLUMN_MAX_WIDTH, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self._art = art
        self._queue_column_width = queue_column_width
        self._duration_seconds = 0
        self._position = 0
        self._video_id = None
        self._title = "nothing playing"
        self._paused = False
        self._queue_tracks = []
        self._queue_index = None

    def compose(self):
        with Horizontal():
            yield AlbumArt(id="now-playing-art", renderer=self._art)
            # bottom-aligned (see app.tcss) so the text sits level with the
            # foot of the cover, right above the shortcut bar
            with Vertical(id="now-playing-text"):
                # fills the dead space above the title/artist with what
                # just played and what plays next
                with Horizontal(id="now-playing-queue"):
                    yield Static("", id="now-playing-played")
                    yield Static("", id="now-playing-upnext")
                yield Static("nothing playing", id="now-playing-track")
                yield Static("", id="now-playing-artist")
                with Horizontal(id="now-playing-bar"):
                    yield ProgressBar(
                        id="now-playing-progress", show_eta=False, show_percentage=False
                    )
                    yield Static("0:00 / 0:00", id="now-playing-time")
                    yield Static("vol 100", id="now-playing-volume")

    def _render_track_line(self):
        icon = "||" if self._paused else ">"
        self.query_one("#now-playing-track", Static).update(f"{icon} {escape(self._title)}")

    def on_click(self, event):
        bar = self.query_one("#now-playing-progress", ProgressBar)
        region = bar.region
        if not region.contains(event.screen_x, event.screen_y):
            return
        if not self._duration_seconds or region.width <= 0:
            return
        fraction = min(1.0, max(0.0, (event.screen_x - region.x) / region.width))
        self.post_message(self.SeekRequested(fraction * self._duration_seconds))

    def on_track_changed(self, data):
        data = data or {}
        self._title = data.get("title") or "Unknown Title"
        self._video_id = data.get("video_id")
        self._render_track_line()
        artist = data.get("artist") or ""
        album = data.get("album") or ""
        self.query_one("#now-playing-artist", Static).update(
            escape(" · ".join(part for part in (artist, album) if part))
        )
        self.query_one(AlbumArt).show(data.get("thumbnail"))

    def on_state_changed(self, data):
        self._paused = bool((data or {}).get("paused"))
        self._render_track_line()
        volume = (data or {}).get("volume")
        if volume is not None:
            self.set_volume(volume)

    def on_position(self, data):
        data = data or {}
        position = data.get("position") or 0
        duration = data.get("duration_seconds") or self._duration_seconds
        self._position = position
        self._duration_seconds = duration
        bar = self.query_one("#now-playing-progress", ProgressBar)
        bar.update(total=duration or None, progress=position)
        self.query_one("#now-playing-time", Static).update(
            f"{_format_time(position)} / {_format_time(duration)}"
        )

    def set_volume(self, level):
        self.query_one("#now-playing-volume", Static).update(f"vol {int(level)}")

    def on_resize(self):
        self.call_after_refresh(self._refresh_queue_summary)

    def set_queue(self, data):
        """Refresh the played/up-next columns from a `queue_get`/
        `queue_changed` payload (same shape as `QueuePane.set_queue`)."""
        data = data or {}
        self._queue_tracks = data.get("tracks") or []
        self._queue_index = data.get("index")
        self._refresh_queue_summary()

    def _refresh_queue_summary(self, width=None, height=None):
        text_width = self.query_one("#now-playing-text").size.width
        width = text_width or width
        height = height or self.size.height
        layout = queue_summary_layout(width, height, self._queue_column_width)
        self.query_one("#now-playing-played", Static).styles.width = layout.column_width
        self.query_one("#now-playing-upnext", Static).styles.width = layout.column_width
        played, up_next = split_queue(
            self._queue_tracks,
            self._queue_index,
            before=layout.track_count,
            after=layout.track_count,
        )
        self.query_one("#now-playing-played", Static).update(
            self._render_column("PLAYED", played, layout)
        )
        self.query_one("#now-playing-upnext", Static).update(
            self._render_column("UP NEXT", up_next, layout)
        )

    @staticmethod
    def _render_column(heading, tracks, layout=None):
        layout = layout or queue_summary_layout()
        lines = [heading] + [
            escape(queue_track_label(track, layout)) for track in tracks
        ]
        return "\n".join(lines)
