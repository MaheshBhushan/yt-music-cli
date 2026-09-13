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
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

#: How long to wait for a freshly spawned mpv to open its IPC endpoint.
#: Generous on purpose: mpv's first start after it is installed can take
#: ten seconds or more while the OS verifies a new binary and its libraries
#: (macOS took 11 s for a just-brewed mpv, one second past the old 10 s
#: limit, and ytm gave up while mpv was still coming up). Waiting this long
#: costs nothing when mpv is merely slow, because an mpv that has *died* is
#: noticed as soon as it exits rather than at the end of the wait.
SPAWN_TIMEOUT = 60.0

#: how often to look for the endpoint, and at the process, while waiting
SPAWN_POLL = 0.05

#: mpv's own start-up chatter, kept beside the socket so a failure to start
#: can say what mpv said instead of only that nothing appeared
STARTUP_LOG = "mpv-start.log"

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


#: Package managers that ship mpv, in the order to try them, keyed by
#: platform: (tool on PATH, command, whether it needs root). mpv is a C
#: program, not a Python one -- the `mpv` and `python-mpv` packages on PyPI
#: are bindings to libmpv and carry no binary -- so `pip install ytm` and
#: `uv tool install ytm` cannot bring it along and this is the next best
#: thing: know the one command that works on this machine.
MPV_INSTALLERS = {
    "darwin": [("brew", ["brew", "install", "mpv"], False)],
    "linux": [
        ("apt-get", ["apt-get", "install", "-y", "mpv"], True),
        ("dnf", ["dnf", "install", "-y", "mpv"], True),
        ("pacman", ["pacman", "-S", "--noconfirm", "mpv"], True),
        ("zypper", ["zypper", "install", "-y", "mpv"], True),
        ("apk", ["apk", "add", "mpv"], True),
        ("xbps-install", ["xbps-install", "-y", "mpv"], True),
    ],
    "freebsd": [("pkg", ["pkg", "install", "-y", "mpv"], True)],
    # shinchiro's is the mpv in winget; `mpv.net` there is a different program
    "win32": [
        ("scoop", ["scoop", "install", "mpv"], False),
        ("winget", ["winget", "install", "-e", "--id", "shinchiro.mpv"], False),
        ("choco", ["choco", "install", "-y", "mpv"], False),
    ],
}

MPV_SITE = "https://mpv.io"


def _platform_key(platform=None):
    platform = platform or sys.platform
    for prefix, key in (("win", "win32"), ("linux", "linux"), ("freebsd", "freebsd")):
        if platform.startswith(prefix):
            return key
    return platform


def mpv_install_command(platform=None, which=shutil.which, root=None):
    """The command that installs mpv on this machine, or None if none fits.

    The first package manager actually on PATH wins. `sudo` is prefixed only
    where the manager needs root, this is not already root, and sudo exists
    -- never for Homebrew, which refuses to run under it.
    """
    if root is None:
        geteuid = getattr(os, "geteuid", None)
        root = geteuid() == 0 if geteuid is not None else False
    for tool, command, needs_root in MPV_INSTALLERS.get(_platform_key(platform), []):
        if which(tool) is None:
            continue
        if needs_root and not root and which("sudo") is not None:
            return ["sudo", *command]
        return list(command)
    return None


def mpv_missing_message(mpv_bin="mpv", platform=None, which=shutil.which, root=None):
    """Why ytm cannot start, and the one thing to do about it.

    The bare OSError ("[Errno 2] No such file or directory: 'mpv'") does not
    say that mpv is a separate program, let alone how to get it, and it is
    the first thing a fresh `uv tool install ytm` shows.
    """
    lead = (
        f"{mpv_bin} is not installed. mpv is a separate program -- it is what "
        f"actually plays the audio -- and pip cannot install it"
    )
    command = mpv_install_command(platform=platform, which=which, root=root)
    if command is None:
        return f"{lead}. Install it from {MPV_SITE}, then run ytm again."
    return f"{lead}. Run 'ytm install-mpv' (it runs: {' '.join(command)}), or see {MPV_SITE}."


def startup_log_path(args):
    """Where to keep mpv's start-up output: beside its socket, or nowhere.

    Windows names a pipe rather than a file, so there is no directory to
    put it in there and the caller falls back to discarding the output.
    """
    for arg in args:
        if arg.startswith("--input-ipc-server="):
            endpoint = arg.split("=", 1)[1]
            if endpoint.startswith("\\\\"):  # \\.\pipe\... is not a path
                return None
            return Path(endpoint).parent / STARTUP_LOG
    return None


def spawn_mpv(args):
    """Start mpv detached from this process so it outlives the CLI command.

    Returns the `Popen`, so the caller can tell an mpv that is starting
    slowly from one that has already died -- they look identical from the
    socket's side, which is nothing.
    """
    if shutil.which(args[0]) is None:
        raise PlayerError(mpv_missing_message(args[0]))
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        # mpv runs with --no-terminal and nothing attached, so without this
        # a refused option or a missing library is lost and all ytm can
        # report is that no socket turned up
        "stderr": _startup_log_file(args),
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(args, **kwargs)
    except OSError as exc:
        raise PlayerError(f"could not start mpv ({args[0]}): {exc}") from exc
    finally:
        handle = kwargs["stderr"]
        if handle != subprocess.DEVNULL:
            handle.close()


def _startup_log_file(args):
    path = startup_log_path(args)
    if path is None:
        return subprocess.DEVNULL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "wb")
    except OSError:
        return subprocess.DEVNULL


def _mpv_said(args, limit=400):
    """The tail of what mpv printed while starting, or "" if it said nothing."""
    path = startup_log_path(args)
    if path is None:
        return ""
    try:
        said = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return said[-limit:] if said else ""


class Player:
    """One connection to the persistent mpv."""

    def __init__(
        self,
        ipc_path=None,
        spawn=True,
        spawner=spawn_mpv,
        timeout=REPLY_TIMEOUT,
        mixer=None,
        **mpv_options,
    ):
        self._ipc_path = ipc_path or default_ipc_path()
        self._mpv_options = mpv_options
        #: a `ytm.volume.SystemVolume`: then `volume()` is the desktop's
        #: output volume and mpv's own stays at 100 (None: mpv's volume)
        self.mixer = mixer
        #: None for a connection that sits in `observe()` indefinitely
        self._timeout = timeout
        self._file = None
        self._request_id = 0
        # one request/reply at a time on the socket; callers may be on any thread
        self._io_lock = threading.RLock()
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
        args = mpv_args(self._ipc_path, **self._mpv_options)
        process = spawner(args)
        deadline = time.monotonic() + SPAWN_TIMEOUT
        last = None
        while time.monotonic() < deadline:
            try:
                self._open()
                return
            except OSError as exc:
                last = exc
            # an mpv that has exited is never going to open the socket, and
            # waiting out the whole timeout to say so helps nobody
            if process is not None and process.poll() is not None:
                raise PlayerError(_died_message(args, process.returncode))
            time.sleep(SPAWN_POLL)
        raise PlayerError(_never_ready_message(args, self._ipc_path, last))

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
        with self._io_lock:
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

    def get_many(self, *names, default=None):
        """Values for several properties in one round trip.

        mpv answers commands in the order they arrive, so the whole batch is
        written before the first reply is read; replies are matched by
        request id exactly as `command` matches its own. A property mpv
        reports as unavailable yields `default` rather than an error, which
        is what the individual `get` does too.
        """
        with self._io_lock:
            if self._file is None:
                raise PlayerError("player connection is closed")
            wanted = {}
            try:
                for name in names:
                    self._request_id += 1
                    wanted[self._request_id] = name
                    request = {
                        "command": ["get_property", name],
                        "request_id": self._request_id,
                    }
                    self._file.write((json.dumps(request) + "\n").encode("utf-8"))
                values = {}
                while wanted:
                    line = self._file.readline()
                    if not line:
                        raise PlayerError("mpv closed the connection")
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    name = wanted.pop(message.get("request_id"), None)
                    if name is None:
                        continue
                    if message.get("error") != "success":
                        values[name] = default
                        continue
                    value = message.get("data")
                    values[name] = default if value is None else value
                return values
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
        existing = self.index_of(url)
        if existing is not None:
            # already queued: jump to it rather than queue a second copy
            self.play_index(existing)
            return
        index = self.get("playlist-pos", -1)
        index = -1 if index is None else index
        self._loadfile(url, "insert-next", title)
        self.play_index(index + 1 if index >= 0 else 0)

    def enqueue(self, url, title=None):
        """Append `url` to the end of the playlist without interrupting.

        A track that is already queued is not added again; returns whether
        anything was appended.
        """
        if self.index_of(url) is not None:
            return False
        self._loadfile(url, "append", title)
        return True

    def enqueue_many(self, items):
        """Append several ``(url, title)`` pairs, skipping what is queued.

        The playlist is read once for the whole batch. Appending one by one
        re-read it per track, so queueing a radio station or a playlist cost
        a round trip per entry on a list that grew with every one of them.
        Returns the number of entries actually appended.
        """
        items = list(items)
        if not items:
            return 0
        queued = {entry["video_id"] or entry["url"] for entry in self.playlist()}
        added = 0
        for url, title in items:
            wanted = video_id_of(url) or url
            if wanted in queued:
                continue
            queued.add(wanted)
            self._loadfile(url, "append", title)
            added += 1
        return added

    def enqueue_next(self, url, title=None):
        """Put `url` right after the current entry without interrupting.

        A track already in the queue is moved there instead of added twice.
        Returns whether the queue changed.
        """
        index = self.get("playlist-pos", -1)
        index = -1 if index is None else index
        existing = self.index_of(url)
        if existing is not None:
            if existing in (index, index + 1):
                return False  # playing now, or already up next
            # playlist-move puts the entry *before* the one at the target
            # index, which is "right after current" for both directions
            self.move(existing, index + 1)
            return True
        self._loadfile(url, "insert-next" if index >= 0 else "append", title)
        return True

    def index_of(self, url):
        """Playlist position of `url` (matched on video id), or None."""
        wanted = video_id_of(url) or url
        for position, entry in enumerate(self.playlist()):
            if (entry["video_id"] or entry["url"]) == wanted:
                return position
        return None

    def queued_ids(self):
        """The set of video ids currently in the playlist."""
        return {e["video_id"] for e in self.playlist() if e["video_id"]}

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
        """Set the volume to `level` (0-100) if given; return the current one.

        With a mixer this is the system output volume, and mpv's software
        volume is pinned to 100 so the two never attenuate the stream twice
        (an mpv started before the mixer existed may still sit at 70).
        """
        if self.mixer is None:
            if level is not None:
                self.set("volume", max(0, min(100, level)))
            return self.get("volume", 0)
        if level is not None:
            if self.get("volume", 100) != 100:
                self.set("volume", 100)
            return self.mixer.set(level)
        current = self.mixer.get()
        return self.get("volume", 0) if current is None else current

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
        """A snapshot of what mpv is doing, shaped for `ytm status`.

        Every property comes back in one batch: the TUI asks for a status
        after each transport key, and eight separate round trips there were
        eight chances to wait on a busy mpv.
        """
        props = self.get_many(
            "playlist-pos", "idle-active", "media-title", "playback-time",
            "duration", "pause", "playlist-count", "volume",
        )
        index = props.get("playlist-pos")
        index = -1 if index is None else index
        idle = bool(props.get("idle-active")) or index < 0
        return {
            "idle": idle,
            "title": None if idle else props.get("media-title"),
            "position": 0.0 if idle else float(props.get("playback-time") or 0.0),
            "duration": 0.0 if idle else float(props.get("duration") or 0.0),
            "paused": bool(props.get("pause")),
            "volume": self._volume_of(props),
            "index": index,
            "count": int(props.get("playlist-count") or 0),
        }

    def _volume_of(self, props):
        """The volume to report for a status whose batch already read mpv's.

        Without a mixer that batched value is the answer; with one the
        desktop's own volume is, and mpv's is only the fallback for a mixer
        that does not answer.
        """
        mpv_volume = props.get("volume") or 0
        if self.mixer is None:
            return mpv_volume
        current = self.mixer.get()
        return mpv_volume if current is None else current


def _log_file_of(args):
    for arg in args:
        if arg.startswith("--log-file="):
            return arg.split("=", 1)[1]
    return None


def _died_message(args, code):
    said = _mpv_said(args)
    if said:
        return f"mpv exited straight away (status {code}): {said}"
    # ytm runs mpv with --no-terminal, which silences mpv's own messages --
    # only what the OS writes (a missing library, say) reaches the capture,
    # and an option mpv refuses is rejected before the log file is opened
    log = _log_file_of(args)
    where = f", and {log} has the rest" if log else ""
    return (
        f"mpv exited straight away (status {code}) without saying why{where}. "
        f"Running mpv with the same options in a terminal shows what it objects to."
    )


def _never_ready_message(args, ipc_path, last):
    said = _mpv_said(args)
    detail = f". mpv said: {said}" if said else ""
    return (
        f"mpv is running but did not open its IPC endpoint at {ipc_path} "
        f"within {SPAWN_TIMEOUT:g}s ({last}){detail}"
    )


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


#: the `v=` query parameter, and only that one -- matching a bare "v=" also
#: matched the tail of "?rv=" or "&sv=" and returned somebody else's id
_VIDEO_ID_PARAM = re.compile(r"[?&]v=([^&]*)")


def video_id_of(url):
    """The YouTube video id in a watch URL, or None for anything else."""
    if not url:
        return None
    match = _VIDEO_ID_PARAM.search(url)
    return match.group(1) if match else None
