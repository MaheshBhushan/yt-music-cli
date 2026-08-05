"""Audio player module.

A clean playback interface wrapping a headless mpv subprocess, controlled
over mpv's JSON IPC socket. This module knows nothing about queues,
playlists, YouTube, ytmusicapi, or yt-dlp — it simply plays whatever URL or
file path it is given.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Callable, Optional

from ytm import cache

logger = logging.getLogger(__name__)

MPV_BIN = "/usr/bin/mpv"


class Player:
    """Headless mpv player controlled via JSON IPC."""

    def __init__(self, mpv_bin: str = MPV_BIN, audio_device: Optional[str] = None) -> None:
        self._mpv_bin = mpv_bin
        self._audio_device = audio_device
        self._socket_path = os.path.join(
            tempfile.gettempdir(), f"ytm-mpv-{uuid.uuid4().hex}.sock"
        )
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._sock_file = None
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        self._request_id = 0

        self._on_position: Optional[Callable[[float], None]] = None
        self._on_eof: Optional[Callable[[], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None

        #: mpv owns pause state; this mirrors it and is only ever written
        #: from the same call that tells mpv to change it, so it never
        #: drifts from what was actually sent over IPC.
        self._paused = False

        self._start_mpv()
        self._connect()
        self._observe_property("time-pos")

    # -- lifecycle -----------------------------------------------------

    def _start_mpv(self) -> None:
        args = [
            self._mpv_bin,
            "--no-video",
            "--idle",
            f"--input-ipc-server={self._socket_path}",
        ]
        if self._audio_device:
            args.append(f"--audio-device={self._audio_device}")
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _connect(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._socket_path)
                self._sock = sock
                self._sock_file = sock.makefile("r")
                self._reader_thread = threading.Thread(
                    target=self._read_loop, daemon=True
                )
                self._reader_thread.start()
                return
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last_err = exc
                time.sleep(0.05)
        raise RuntimeError(f"could not connect to mpv IPC socket: {last_err}")

    def close(self) -> None:
        """Terminate the mpv subprocess and clean up its socket."""
        self._stop_reader.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # SIGKILL can't be blocked; a process that still isn't
                    # reaped is only stuck in an uninterruptible syscall, not
                    # alive on purpose. Block until it actually exits instead
                    # of letting this escape close() -- the caller's
                    # `contextlib.suppress(Exception)` would otherwise treat
                    # a slow-to-die mpv as if it had already been reaped.
                    self._proc.wait()
            self._proc = None
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass

    def __enter__(self) -> "Player":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- IPC -------------------------------------------------------------

    def _send(self, command: list) -> None:
        self._request_id += 1
        payload = json.dumps({"command": command, "request_id": self._request_id})
        with self._lock:
            if self._sock is None:
                raise RuntimeError("player is closed")
            self._sock.sendall((payload + "\n").encode("utf-8"))

    def _observe_property(self, name: str, observe_id: int = 1) -> None:
        self._send(["observe_property", observe_id, name])

    def _read_loop(self) -> None:
        while not self._stop_reader.is_set():
            try:
                line = self._sock_file.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._handle_event(msg)
            except Exception:
                # Keep the transport alive whatever an observer does.
                # Observers run real logic on this thread, and letting one
                # raise ends the loop: the daemon keeps running while deaf
                # to mpv for the rest of its life -- no position, no
                # end-of-track, no errors, playback stuck with nothing
                # reported.
                #
                # This is a backstop, not the handler. A failure an observer
                # can act on -- a stream URL that will not resolve -- is
                # dealt with where it happens, so it stays a counted
                # playback error rather than arriving here. Anything that
                # does reach this point is a bug in an observer, so it is
                # logged with its traceback instead of being reshaped into
                # a playback error: routing it to `on_error` would make the
                # queue respond to our own crash by skipping the user's
                # music.
                logger.exception("error handling mpv event: %r", msg)

    def _handle_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "property-change" and msg.get("name") == "time-pos":
            value = msg.get("data")
            if value is not None and self._on_position is not None:
                self._on_position(value)
        elif event == "end-file":
            # mpv fires end-file for every reason a file stops playing, not
            # just a genuine end-of-track: `loadfile ... replace` (what
            # `load()` does on every play/next) also emits it with
            # reason "stop", and a stream that fails to load emits it with
            # reason "error". Only "eof" means the track actually finished.
            reason = msg.get("reason")
            if reason == "eof":
                if self._on_eof is not None:
                    self._on_eof()
            elif reason == "error":
                if self._on_error is not None:
                    self._on_error(msg.get("file_error") or "playback error")

    # -- playback control --------------------------------------------------

    def load(self, url: str, video_id: Optional[str] = None) -> None:
        """Load and start playing a stream URL or file path.

        If `video_id` is given and already cached, the cached local file is
        played and `url` is ignored -- no resolution needed.
        """
        target = url
        if video_id is not None:
            cached_path = cache.get_cached_path(video_id)
            if cached_path is not None:
                target = str(cached_path)
        self._send(["loadfile", target, "replace"])
        # Loading a track means the caller intends to play it -- if mpv was
        # left paused (a prior deliberate pause, or a restored session),
        # every subsequent load would otherwise sit fully buffered and never
        # start.
        self._send(["set_property", "pause", False])
        self._paused = False

    def pause(self) -> None:
        self._send(["set_property", "pause", True])
        self._paused = True

    def resume(self) -> None:
        self._send(["set_property", "pause", False])
        self._paused = False

    def toggle(self) -> None:
        self._send(["cycle", "pause"])
        self._paused = not self._paused

    @property
    def paused(self) -> bool:
        """Whether mpv is currently paused."""
        return self._paused

    def seek(self, amount: float, absolute: bool = False) -> None:
        """Seek by `amount` seconds, relative by default or absolute."""
        mode = "absolute" if absolute else "relative"
        self._send(["seek", amount, mode])

    def set_volume(self, volume: float) -> None:
        """Set volume, clamped to the 0-100 range."""
        volume = max(0, min(100, volume))
        self._send(["set_property", "volume", volume])

    # -- observers --------------------------------------------------------

    def on_position(self, callback: Callable[[float], None]) -> None:
        """Register a callback invoked with the current position (seconds)."""
        self._on_position = callback

    def on_eof(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the current file finishes."""
        self._on_eof = callback

    def on_error(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked when the current file fails to load."""
        self._on_error = callback
