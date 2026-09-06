"""The desktop's output volume, so ytm's volume is the one the OS shows.

mpv has its own software volume (the ``volume`` property) that scales the
stream before it reaches the sound server. It is invisible to the desktop:
``vol 70`` in ytm could sit next to 55 % in the system tray, and the media
keys on the keyboard moved the tray, not ytm. With a mixer the number in ytm
*is* the default output's volume -- ``+``/``-`` move the tray, the media keys
move ytm -- and mpv's own volume is pinned to 100 so nothing attenuates
twice.

PipeWire is spoken to with ``wpctl`` and PulseAudio with ``pactl`` (which
PipeWire also ships, and which provides the change stream both need).
Neither present means no mixer, and the caller falls back to mpv's volume.
"""

import re
import shutil
import subprocess
import threading
import time

#: how often to re-read the volume when no ``pactl subscribe`` is available
POLL_INTERVAL = 1.0
#: a mixer command that takes longer than this is treated as absent
COMMAND_TIMEOUT = 2.0

_WPCTL_SINK = "@DEFAULT_AUDIO_SINK@"
_PACTL_SINK = "@DEFAULT_SINK@"


class SystemVolume:
    """Get/set the default output's volume as a 0-100 integer."""

    def __init__(self, wpctl=None, pactl=None, run=subprocess.run):
        self._wpctl = wpctl
        self._pactl = pactl
        self._run = run

    @classmethod
    def detect(cls, which=shutil.which):
        """A mixer for whatever this desktop has, or None when it has neither."""
        wpctl = which("wpctl")
        pactl = which("pactl")
        if not wpctl and not pactl:
            return None
        return cls(wpctl=wpctl, pactl=pactl)

    # -- commands -------------------------------------------------------------

    def _output(self, *argv):
        try:
            result = self._run(
                list(argv), capture_output=True, text=True, timeout=COMMAND_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def get(self):
        """The current volume 0-100, or None if the mixer did not answer."""
        if self._wpctl:
            out = self._output(self._wpctl, "get-volume", _WPCTL_SINK)
            # "Volume: 0.55" or "Volume: 0.55 [MUTED]"
            match = re.search(r"Volume:\s*([0-9.]+)", out or "")
            if match:
                return int(round(float(match.group(1)) * 100))
        if self._pactl:
            out = self._output(self._pactl, "get-sink-volume", _PACTL_SINK)
            # "Volume: front-left: 36045 /  55% / -15,58 dB, ..."
            match = re.search(r"(\d+)%", out or "")
            if match:
                return int(match.group(1))
        return None

    def set(self, level):
        """Set the volume to `level` (clamped to 0-100); return what it reads back."""
        level = int(round(max(0, min(100, level))))
        if self._wpctl:
            self._output(self._wpctl, "set-volume", "-l", "1.0", _WPCTL_SINK, f"{level / 100:.2f}")
        elif self._pactl:
            self._output(self._pactl, "set-sink-volume", _PACTL_SINK, f"{level}%")
        current = self.get()
        return level if current is None else current

    # -- change stream --------------------------------------------------------

    def watch(self, callback, stop):
        """Call `callback(level)` whenever the volume changes, until `stop` is set.

        Uses ``pactl subscribe`` for a push stream of sink events when
        available (a sink event fires for every volume change, from any
        program), else polls every `POLL_INTERVAL` seconds. Blocks; run it
        on its own thread.
        """
        last = self.get()
        if self._pactl:
            try:
                proc = subprocess.Popen(
                    [self._pactl, "subscribe"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                )
            except OSError:
                proc = None
            if proc is not None:
                killer = threading.Thread(
                    target=lambda: (stop.wait(), proc.kill()), daemon=True
                )
                killer.start()
                try:
                    for line in proc.stdout:
                        if stop.is_set():
                            return
                        if "on sink" not in line and "on server" not in line:
                            continue
                        current = self.get()
                        if current is not None and current != last:
                            last = current
                            callback(current)
                finally:
                    proc.kill()
                if stop.is_set():
                    return
        while not stop.wait(POLL_INTERVAL):
            current = self.get()
            if current is not None and current != last:
                last = current
                callback(current)
