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
handle lookup plus overlapped I/O and cancellation. The core is exercised on every
platform; the native pipe path is exercised by the Windows test job.
"""

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
    """Overlapped Win32 I/O permits simultaneous reads and writes.

    CPython's Win32 wrapper owns each OVERLAPPED and its buffer until the
    operation completes, including cancellation. Synchronous file handles
    serialize reads/writes and deadlock when the reader starts first.
    """

    def __init__(self, path, timeout=None):
        import _winapi

        self._api = _winapi
        self._timeout = timeout
        self._guard = threading.Lock()
        self._pending = set()
        self._closed = False
        self._handle = _winapi.CreateFile(
            str(path), _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
            0, 0, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, 0,
        )
        self._stream = LineStream(self._read_chunk, self._cancel_io)

    def _transfer(self, data=None, timeout=None):
        api = self._api
        with self._guard:
            if self._closed:
                raise PipeClosed("the pipe is closed")
            if data is None:
                operation, error = api.ReadFile(self._handle, READ_CHUNK, overlapped=True)
            else:
                operation, error = api.WriteFile(self._handle, data, overlapped=True)
            self._pending.add(operation)
        try:
            if error == api.ERROR_IO_PENDING:
                millis = api.INFINITE if timeout is None else max(0, int(timeout * 1000))
                if api.WaitForSingleObject(operation.event, millis) == api.WAIT_TIMEOUT:
                    operation.cancel()
                    operation.GetOverlappedResult(True)
                    raise PipeTimeout("no completion within the deadline")
            size, error = operation.GetOverlappedResult(True)
            if error:
                raise PipeClosed("the pipe operation was cancelled or failed")
            return operation.getbuffer() if data is None else size
        finally:
            # Cancel and drain even on KeyboardInterrupt: the kernel must no
            # longer own a buffer when its Python OVERLAPPED is released.
            operation.cancel()
            operation.GetOverlappedResult(True)
            with self._guard:
                self._pending.discard(operation)

    def _read_chunk(self):
        return self._transfer()

    def _cancel_io(self):
        with self._guard:
            self._closed = True
            for operation in self._pending:
                operation.cancel()

    def readline(self, timeout=None):
        return self._stream.readline(timeout)

    def write_all(self, data):
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while data:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            written = self._transfer(data, remaining)
            if not written:
                raise PipeClosed("the player did not accept the request")
            data = data[written:]

    def close(self):
        self._stream.close()
        with self._guard:
            if self._handle is not None:
                self._api.CloseHandle(self._handle)
                self._handle = None


def open_transport(path, timeout=None):
    """The right transport for this platform."""
    if sys.platform.startswith("win"):
        return NamedPipeTransport(path, timeout)
    return SocketTransport(path, timeout)


