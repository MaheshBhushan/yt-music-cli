"""mpv IPC transports with bounded, cancelable reads.

The POSIX endpoint is a Unix socket; the Windows endpoint is a byte-mode
named pipe carrying the same newline-delimited JSON protocol. A plain
``open(path, "r+b")`` on Windows gives blocking reads with no timeout, so a
player that accepts the connection and then stops answering could block a
command forever. `NamedPipeTransport` keeps one reader thread per
connection pumping bytes into a queue: `readline(deadline)` waits with a
budget, and `close()` interrupts a blocked native read with ``CancelIoEx``.
There is one worker per connection, never one per request, and a timed-out
request never leaves a worker behind.

`LineStream` is the platform-neutral framing core (deadlines, partial
frames, UTF-8 line decoding, bounded buffering); the Win32 glue is a thin
handle lookup plus the cancellation call. The core is exercised on every
platform; the native pipe path is exercised by the Windows test job.
"""

import ctypes
import os
import queue
import socket
import sys
import threading
import time

#: refuse to buffer more than this without a newline (a malformed peer must
#: not be able to grow memory without bound; large playlists arrive as
#: separate newline-terminated replies, not one giant line)
MAX_BUFFER = 8 * 1024 * 1024

#: close() waits this long for the pump thread before returning anyway; the
#: thread is a daemon, so a stuck native read cannot hold process exit
PUMP_JOIN_TIMEOUT = 2.0

#: one blocking read per chunk; pipes deliver whatever is available
READ_CHUNK = 4096


class PipeTimeout(TimeoutError):
    """No complete line arrived within the caller's deadline."""


class PipeClosed(OSError):
    """The pipe is closed, canceled or broken."""


class LineStream:
    """Newline framing over a blocking byte reader, with deadlines.

    ``read_chunk`` blocks until bytes arrive and returns them; it raises
    OSError when the connection breaks. ``cancel`` must make a blocked
    read_chunk return (``CancelIoEx`` on Windows, a socket shutdown on
    POSIX). The pump owns exactly one thread for the connection's lifetime.
    """

    def __init__(self, read_chunk, cancel, max_buffer=MAX_BUFFER):
        self._read_chunk = read_chunk
        self._cancel = cancel
        self._max_buffer = max_buffer
        self._lines = queue.Queue()
        self._closed = threading.Event()
        self._error = None
        self._buffer = b""
        self._thread = threading.Thread(
            target=self._pump, name="ytm-ipc-reader", daemon=True
        )
        self._thread.start()

    def _pump(self):
        try:
            while not self._closed.is_set():
                chunk = self._read_chunk()
                if not chunk:
                    break  # EOF: the peer closed its end
                self._buffer += chunk
                while True:
                    newline = self._buffer.find(b"\n")
                    if newline < 0:
                        break
                    line, self._buffer = self._buffer[:newline], self._buffer[newline + 1:]
                    self._lines.put(line)
                if len(self._buffer) > self._max_buffer:
                    raise ValueError("unterminated response")
        except BaseException as exc:  # cancellation and broken pipes included
            self._error = exc
        finally:
            self._lines.put(None)

    def readline(self, timeout=None):
        """One complete line without its newline, or raise.

        Raises `PipeTimeout` when the budget runs out and `PipeClosed` when
        the connection ended. A complete line already queued is delivered
        even if the pump has since stopped.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise PipeTimeout("no reply within the deadline")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise PipeTimeout("no reply within the deadline") from None
            if line is not None:
                # complete lines only: a multibyte codepoint cannot be split
                return line.decode("utf-8", errors="replace")
            error = self._error
            if isinstance(error, ValueError):
                raise PipeClosed("the player sent an unterminated response") from error
            raise PipeClosed("the connection to the player ended") from error

    def close(self):
        """Cancel a blocked read and stop the pump; idempotent.

        Cancellation is requested before the join, and the pump's buffers
        stay alive until its thread has actually finished, so no pending
        read can touch freed memory.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._cancel()
        except Exception:
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(PUMP_JOIN_TIMEOUT)

    @property
    def closed(self):
        return self._closed.is_set()


class SocketTransport:
    """The Unix-socket transport behind the common interface."""

    def __init__(self, path, timeout=None):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
            self._file = sock.makefile("rwb", buffering=0)
            self._socket = sock
        except BaseException:
            sock.close()
            raise

    def readline(self, timeout=None):
        # A per-call timeout keeps a command's deadline from resetting on
        # every unrelated event.
        self._socket.settimeout(timeout)
        return self._file.readline().decode("utf-8", errors="replace")

    def write_all(self, data):
        self._file.write(data)

    def close(self):
        # Interrupt a blocking observer read before closing the wrapper.
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


class NamedPipeTransport:
    """Windows named pipe: `LineStream` plus native cancellation.

    The handle is opened as a blocking byte stream, exactly as mpv's pipe
    expects; ``CancelIoEx`` targets that handle so a blocked read returns
    instead of parking a thread for the rest of the process's life.
    """

    def __init__(self, path, timeout=None):
        self._file = open(path, "r+b", buffering=0)
        self._handle = _os_handle(self._file)
        self._kernel32 = _kernel32()
        self._stream = LineStream(self._read_chunk, self._cancel_io)

    def _read_chunk(self):
        return self._file.read(READ_CHUNK)

    def _cancel_io(self):
        # CancelIoEx cancels outstanding I/O for the handle regardless of
        # which thread issued it; already-completed operations are a no-op.
        self._kernel32.CancelIoEx(ctypes.c_void_p(self._handle), None)

    def readline(self, timeout=None):
        return self._stream.readline(timeout)

    def write_all(self, data):
        view = memoryview(data)
        while view:
            written = self._file.write(view)
            if not written:
                raise PipeClosed("the player did not accept the request")
            view = view[written:]

    def close(self):
        self._stream.close()
        try:
            self._file.close()
        except OSError:
            pass


def open_transport(path, timeout=None):
    """The right transport for this platform."""
    if sys.platform.startswith("win"):
        return NamedPipeTransport(path, timeout)
    return SocketTransport(path, timeout)


def _os_handle(file):
    import msvcrt

    return msvcrt.get_osfhandle(file.fileno())


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CancelIoEx.restype = ctypes.c_int
    return kernel32
