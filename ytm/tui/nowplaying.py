"""Now-playing pane: cover art, current track, progress bar and volume."""

import io
import urllib.request

from textual.containers import Container, Horizontal, Vertical
from textual.widgets import ProgressBar, Static

# Imported at module level on purpose: textual-image probes the terminal for
# its cell size and graphics support when it loads, which only works before
# Textual takes the terminal over. Importing it inside compose() stalled the
# whole app for seconds. Only the TUI imports this module, never the CLI.
from textual_image.widget import HalfcellImage, Image as AutoImage

#: `[ui] art` values mapped onto textual-image widgets; None means no art
ART_RENDERERS = {"auto": AutoImage, "blocks": HalfcellImage, "off": None}

#: how long to wait for a cover image before giving up on it
ART_TIMEOUT = 5.0

#: covers kept decoded in memory, so skipping back and forth does not refetch
ART_CACHE_SIZE = 20


def _format_time(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


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

    def __init__(self, *args, fetcher=fetch_bytes, renderer="auto", **kwargs):
        super().__init__(*args, **kwargs)
        self._fetcher = fetcher
        self._renderer = ART_RENDERERS.get(renderer, AutoImage)
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
    """Track line, seek progress and volume, driven purely by player events."""

    def __init__(self, *args, art="auto", **kwargs):
        super().__init__(*args, **kwargs)
        self._art = art
        self._duration_seconds = 0
        self._video_id = None
        self._title = "nothing playing"
        self._paused = False

    def compose(self):
        with Horizontal():
            yield AlbumArt(id="now-playing-art", renderer=self._art)
            with Vertical(id="now-playing-text"):
                yield Static("nothing playing", id="now-playing-track")
                yield Static("", id="now-playing-artist")
                with Horizontal(id="now-playing-bar"):
                    yield ProgressBar(id="now-playing-progress", show_eta=False)
                    yield Static("0:00 / 0:00", id="now-playing-time")
                    yield Static("vol 100", id="now-playing-volume")

    def _render_track_line(self):
        icon = "||" if self._paused else ">"
        self.query_one("#now-playing-track", Static).update(f"{icon} {self._title}")

    def on_track_changed(self, data):
        data = data or {}
        self._title = data.get("title") or "Unknown Title"
        self._video_id = data.get("video_id")
        self._render_track_line()
        artist = data.get("artist") or ""
        album = data.get("album") or ""
        self.query_one("#now-playing-artist", Static).update(
            " · ".join(part for part in (artist, album) if part)
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
        self._duration_seconds = duration
        bar = self.query_one("#now-playing-progress", ProgressBar)
        bar.update(total=duration or None, progress=position)
        self.query_one("#now-playing-time", Static).update(
            f"{_format_time(position)} / {_format_time(duration)}"
        )

    def set_volume(self, level):
        self.query_one("#now-playing-volume", Static).update(f"vol {int(level)}")
