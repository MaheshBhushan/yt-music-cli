"""Now-playing pane: current track, progress bar and volume indicator."""

from textual.containers import Horizontal, Vertical
from textual.widgets import ProgressBar, Static


def _format_time(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


class NowPlaying(Vertical):
    """Track line, seek progress and volume, driven purely by daemon events."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._duration_seconds = 0
        self._video_id = None
        self._title = "nothing playing"
        self._paused = False

    def compose(self):
        yield Static("nothing playing", id="now-playing-track")
        with Horizontal(id="now-playing-bar"):
            yield ProgressBar(id="now-playing-progress", show_eta=False)
            yield Static("0:00 / 0:00", id="now-playing-time")
            yield Static("vol 100", id="now-playing-volume")

    def _render_track_line(self):
        icon = "||" if self._paused else ">"
        self.query_one("#now-playing-track", Static).update(f"{icon} {self._title}")

    def on_track_changed(self, data):
        self._title = (data or {}).get("title") or "Unknown Title"
        self._video_id = (data or {}).get("video_id")
        self._render_track_line()

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
