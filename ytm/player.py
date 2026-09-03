"""Control of a persistent mpv over its JSON IPC.

This is the playback half of the simplified core: mpv owns the playlist, the
playback state and, through its bundled ``ytdl_hook``, the stream resolution
of every YouTube URL it is handed. ytm never resolves a stream URL itself --
it appends ``https://music.youtube.com/watch?v=<id>`` entries and mpv calls
yt-dlp when an entry is about to play, which is also when the URL has to be
fresh.

Nothing here keeps state between calls. A command connects to the running
mpv (spawning one if there is none), sends its commands, reads the replies
and returns. The IPC endpoint is a Unix socket on POSIX and a named pipe on
Windows; both are newline-delimited JSON with the same command set.

Only the properties and commands the CLI needs are wrapped. Anything else is
reachable through :meth:`Player.command`, :meth:`Player.get` and
:meth:`Player.set`, so a new feature does not need a new abstraction.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: how long to wait for a freshly spawned mpv to open its IPC endpoint
SPAWN_TIMEOUT = 10.0

#: how long one reply may take; ytdl_hook resolution happens asynchronously
#: in mpv, so no command blocks on the network, but a wedged mpv should not
#: hang the CLI forever either
REPLY_TIMEOUT = 5.0

WATCH_URL = "https://music.youtube.com/watch?v={video_id}"


class PlayerError(Exception):
    """mpv could not be reached, could not be started, or rejected a command."""


def default_ipc_path(platform=None):
    """Where the persistent mpv listens: a socket on POSIX, a pipe on Windows."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return r"\\.\pipe\ytm-mpv"
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = os.path.join(tempfile.gettempdir(), f"ytm-{os.getuid()}")
    return str(Path(runtime_dir) / "ytm" / "mpv.sock")


def mpv_args(
    ipc_path,
    mpv_bin="mpv",
    ytdlp_path=None,
    cookies_file=None,
    extractor_args=None,
    js_runtimes=None,
    audio_device=None,
    scripts=(),
    script_opts=None,
    extra_args=(),
):
    """The command line for the persistent mpv.

    `extra_args` are appended verbatim -- the escape hatch for anything not
    worth a keyword, such as ``--log-file=...`` when debugging playback.

    Everything yt-dlp needs -- cookies, the PO token provider, a JavaScript
    runtime -- travels in ``--ytdl-raw-options`` and is applied by
    ``ytdl_hook`` to every resolution, so the Python side never has to know
    how a stream gets resolved.
    """
    args = [
        mpv_bin,
        "--idle=yes",
        "--no-video",
        "--no-terminal",
        "--force-window=no",
        f"--input-ipc-server={ipc_path}",
        "--ytdl-format=bestaudio",
    ]
    if ytdlp_path:
        args.append(f"--script-opts=ytdl_hook-ytdl_path={ytdlp_path}")
    raw = []
    if cookies_file:
        raw.append(f"cookies={cookies_file}")
    if extractor_args:
        raw.append(f"extractor-args={extractor_args}")
    if js_runtimes:
        raw.append(f"js-runtimes={js_runtimes}")
    if raw:
        args.append("--ytdl-raw-options=" + ",".join(raw))
    if audio_device and audio_device != "auto":
        args.append(f"--audio-device={audio_device}")
    for script in scripts:
        args.append(f"--script={script}")
    for key, value in (script_opts or {}).items():
        args.append(f"--script-opts-append={key}={value}")
    args.extend(extra_args)
    return args


def spawn_mpv(args):
    """Start mpv detached from this process so it outlives the CLI command."""
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **kwargs)
    except OSError as exc:
        raise PlayerError(f"could not start mpv ({args[0]}): {exc}") from exc


class Player:
    """One connection to the persistent mpv."""

    def __init__(
        self, ipc_path=None, spawn=True, spawner=spawn_mpv, timeout=REPLY_TIMEOUT, **mpv_options
    ):
        self._ipc_path = ipc_path or default_ipc_path()
        self._mpv_options = mpv_options
        #: None for a connection that sits in `observe()` indefinitely
        self._timeout = timeout
        self._file = None
        self._request_id = 0
        self._connect(spawn=spawn, spawner=spawner)

    # -- connection ----------------------------------------------------------

    def _connect(self, spawn, spawner):
        try:
            self._open()
            return
        except OSError:
            pass
        if not spawn:
            raise PlayerError(
                f"mpv is not running (no IPC endpoint at {self._ipc_path})"
            )
        spawner(mpv_args(self._ipc_path, **self._mpv_options))
        deadline = time.monotonic() + SPAWN_TIMEOUT
        last = None
        while time.monotonic() < deadline:
            try:
                self._open()
                return
            except OSError as exc:
                last = exc
                time.sleep(0.05)
        raise PlayerError(
            f"mpv started but never opened its IPC endpoint at "
            f"{self._ipc_path}: {last}"
        )

    def _open(self):
        if sys.platform.startswith("win"):
            # A named pipe behaves like a file on Windows; both directions
            # go through the same handle.
            self._file = open(self._ipc_path, "r+b", buffering=0)
            return
        Path(self._ipc_path).parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect(self._ipc_path)
        self._file = sock.makefile("rwb", buffering=0)

    def close(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- raw protocol --------------------------------------------------------

    def command(self, *args):
        """Send one mpv command and return its ``data``.

        Events mpv pushes on the same connection are skipped; only the reply
        carrying our request id is returned. Raises PlayerError when mpv
        answers with anything but ``success``.
        """
        if self._file is None:
            raise PlayerError("player connection is closed")
        self._request_id += 1
        request = {"command": list(args), "request_id": self._request_id}
        try:
            self._file.write((json.dumps(request) + "\n").encode("utf-8"))
            while True:
                line = self._file.readline()
                if not line:
                    raise PlayerError("mpv closed the connection")
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("request_id") != self._request_id:
                    continue
                if message.get("error") != "success":
                    raise PlayerError(
                        f"mpv rejected {args[0]}: {message.get('error')}"
                    )
                return message.get("data")
        except (OSError, socket.timeout) as exc:
            raise PlayerError(f"lost the connection to mpv: {exc}") from exc

    def observe(self, *names):
        """Yield ``(name, value)`` for every change to the given properties.

        Blocks for as long as the caller iterates; build the Player with
        ``timeout=None`` for this. mpv reports each property once right
        after it is observed, so the first values arrive immediately.
        """
        # All registrations are written before anything is read: mpv answers
        # each observe_property with the property's current value right
        # away, and `command()` would discard those events while waiting
        # for the next reply -- which is how the first track sometimes went
        # unannounced. Replies are skipped here instead.
        if self._file is None:
            raise PlayerError("player connection is closed")
        try:
            for observe_id, name in enumerate(names, 1):
                self._request_id += 1
                request = {
                    "command": ["observe_property", observe_id, name],
                    "request_id": self._request_id,
                }
                self._file.write((json.dumps(request) + "\n").encode("utf-8"))
            while True:
                line = self._file.readline()
                if not line:
                    raise PlayerError("mpv closed the connection")
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in message and message.get("error") != "success":
                    raise PlayerError(f"mpv rejected observe_property: {message.get('error')}")
                if message.get("event") == "property-change":
                    yield message.get("name"), message.get("data")
        except (OSError, socket.timeout) as exc:
            raise PlayerError(f"lost the connection to mpv: {exc}") from exc

    def get(self, name, default=None):
        """A property's value, or `default` if mpv says it is unavailable."""
        try:
            return self.command("get_property", name)
        except PlayerError as exc:
            if "unavailable" in str(exc) or "not found" in str(exc):
                return default
            raise

    def set(self, name, value):
        self.command("set_property", name, value)

    # -- loading -------------------------------------------------------------

    def play(self, url, title=None):
        """Insert `url` right after the current entry and play it now.

        mpv's ``*-play`` loadfile flags only start playback when it is idle,
        so "play this" while something is playing needs an explicit jump to
        the inserted entry; the rest of the queue stays behind it.
        """
        index = self.get("playlist-pos", -1)
        index = -1 if index is None else index
        self._loadfile(url, "insert-next", title)
        self.play_index(index + 1 if index >= 0 else 0)

    def enqueue(self, url, title=None):
        """Append `url` to the end of the playlist without interrupting."""
        self._loadfile(url, "append", title)

    def _loadfile(self, url, flags, title):
        self.command("loadfile", url, flags, -1, option_list(**{"force-media-title": title}) if title else "")

    # -- transport -----------------------------------------------------------

    def pause(self):
        self.set("pause", True)

    def resume(self):
        self.set("pause", False)

    def toggle(self):
        self.command("cycle", "pause")

    def stop(self):
        """Stop playback and empty the playlist; mpv stays alive and idle."""
        self.command("stop")

    def next(self):
        self.command("playlist-next", "force")

    def prev(self):
        self.command("playlist-prev", "force")

    def seek(self, seconds, absolute=False):
        self.command("seek", seconds, "absolute" if absolute else "relative")

    def volume(self, level=None):
        """Set the volume to `level` (0-100) if given; return the current one."""
        if level is not None:
            self.set("volume", max(0, min(100, level)))
        return self.get("volume", 0)

    def quit(self):
        """Ask mpv to exit; the connection is closed afterwards."""
        try:
            self.command("quit")
        except PlayerError:
            # mpv may close the socket before replying; that is a success
            pass
        finally:
            self.close()

    # -- playlist ------------------------------------------------------------

    def playlist(self):
        """The playlist as mpv holds it: url, title (if any) and cursor flag."""
        entries = self.get("playlist", []) or []
        return [
            {
                "url": entry.get("filename"),
                "video_id": video_id_of(entry.get("filename")),
                "title": entry.get("title"),
                "current": bool(entry.get("current")),
            }
            for entry in entries
        ]

    def play_index(self, index):
        """Jump to entry `index` and make sure it is audible: mpv keeps its
        paused state across a jump, so "play this" while paused would
        otherwise load the track and sit there silently."""
        self.command("playlist-play-index", index)
        self.set("pause", False)

    def remove(self, index):
        self.command("playlist-remove", index)

    def move(self, from_index, to_index):
        self.command("playlist-move", from_index, to_index)

    def clear(self):
        """Drop every entry except the one playing."""
        self.command("playlist-clear")

    def shuffle(self):
        self.command("playlist-shuffle")

    # -- state ---------------------------------------------------------------

    def status(self):
        """A snapshot of what mpv is doing, shaped for `ytm status`."""
        index = self.get("playlist-pos", -1)
        if index is None:
            index = -1
        idle = bool(self.get("idle-active", False)) or index < 0
        return {
            "idle": idle,
            "title": None if idle else self.get("media-title"),
            "position": 0.0 if idle else float(self.get("playback-time", 0.0) or 0.0),
            "duration": 0.0 if idle else float(self.get("duration", 0.0) or 0.0),
            "paused": bool(self.get("pause", False)),
            "volume": self.get("volume", 0),
            "index": index,
            "count": int(self.get("playlist-count", 0) or 0),
        }


def option_list(**options):
    """mpv ``key=value,key=value`` option string with every value quoted.

    mpv splits the list on commas, so a title like "Hello, World" is
    rejected as an invalid parameter unless it is length-prefixed with the
    parser's ``%n%`` form; the count is in UTF-8 bytes.
    """
    parts = []
    for key, value in options.items():
        text = str(value)
        parts.append(f"{key}=%{len(text.encode('utf-8'))}%{text}")
    return ",".join(parts)


def watch_url(video_id):
    return WATCH_URL.format(video_id=video_id)


def video_id_of(url):
    """The YouTube video id in a watch URL, or None for anything else."""
    if not url:
        return None
    marker = "v="
    start = url.find(marker)
    if start < 0:
        return None
    end = url.find("&", start)
    return url[start + len(marker):] if end < 0 else url[start + len(marker):end]
