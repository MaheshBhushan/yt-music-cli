"""Processes owned by one interactive YTM session, never by executable name."""

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


class ProcessOwner:
    """Track children and their descendants; terminate and reap them on close.

    Linux helpers may create their own process groups (mpv's extractors do),
    so group membership alone is insufficient. An inherited instance token
    identifies even those descendants after their immediate parent exits.
    A subreaper keeps their exit statuses here rather than leaving zombies
    to a desktop's potentially non-reaping PID 1.
    """

    _lock = threading.RLock()
    _users = 0
    _previous_subreaper = 0

    def __init__(self, stop_event=None):
        self._guard = threading.RLock()
        self._processes = []
        self._births = {}
        self._closed = False
        self.stop_event = stop_event or threading.Event()
        self._token = uuid.uuid4().hex
        self._linux = sys.platform.startswith("linux")
        if self._linux:
            with self._lock:
                if not self._users:
                    previous = ctypes.c_int()
                    libc = ctypes.CDLL(None, use_errno=True)
                    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
                        raise OSError(ctypes.get_errno(), "could not enable child reaping")
                    type(self)._previous_subreaper = previous.value
                type(self)._users += 1

    def spawn(self, args, **kwargs):
        with self._guard:
            if self._closed or self.stop_event.is_set():
                raise OSError("YTM is shutting down")
            env = dict(kwargs.pop("env", os.environ))
            env["YTM_PROCESS_OWNER"] = self._token
            if os.name != "nt":
                kwargs.setdefault("start_new_session", True)
            process = subprocess.Popen(args, env=env, **kwargs)
            self._processes.append(process)
            if self._linux:
                self._births[process.pid] = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            return process

    def run(self, args, *, capture_output=False, text=False, timeout=None, **kwargs):
        if capture_output:
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process = self.spawn(args, text=text, **kwargs)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        finally:
            # Volume polling must not accumulate one Popen object per sample.
            if process.returncode is not None:
                with self._guard:
                    self._processes.remove(process)
                    self._births.pop(process.pid, None)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def _descendants(self):
        token = f"YTM_PROCESS_OWNER={self._token}".encode()
        found = {}
        parents = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
                pid, started = int(path.name), stat[19]
                parents[pid] = (int(stat[1]), started)
                # Zombies have no environ. An adopted zombie still carries
                # the session ID of its owned launcher, even after that
                # launcher has died and been waited on.
                adopted_zombie = (
                    stat[0] == "Z" and int(stat[1]) == os.getpid()
                    and int(stat[3]) in self._births
                )
                if (self._births.get(pid) == started or adopted_zombie
                        or token in (path / "environ").read_bytes().split(b"\0")):
                    found[pid] = started
            except (OSError, IndexError):
                continue
        # Also cover descendants that deliberately clear their environment.
        while True:
            children = {pid: started for pid, (parent, started) in parents.items()
                        if parent in found and pid not in found}
            if not children:
                break
            found.update(children)
        return found

    @staticmethod
    def _same_process(pid, started):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19] == started
        except (OSError, IndexError):
            return False

    def _signal(self, pid, started, sig):
        try:
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                fd = os.pidfd_open(pid)
                try:
                    if self._same_process(pid, started):
                        signal.pidfd_send_signal(fd, sig)
                finally:
                    os.close(fd)
            elif self._same_process(pid, started):
                os.kill(pid, sig)
        except ProcessLookupError:
            pass

    def close(self):
        with self._guard:
            if self._closed:
                return
            self._closed = True
            self.stop_event.set()
            processes = list(self._processes)
        try:
            if self._linux:
                known = self._descendants()
                # First ask all owned processes to terminate; keep discovering
                # children while parents unwind, then kill stubborn survivors.
                for sig, timeout in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 1.5)):
                    deadline = time.monotonic() + timeout
                    signalled = set()
                    while True:
                        known.update(self._descendants())
                        for pid, started in list(known.items()):
                            if not self._same_process(pid, started):
                                known.pop(pid)
                                continue
                            if (pid, started) not in signalled:
                                self._signal(pid, started, sig)
                                signalled.add((pid, started))
                        for process in processes:
                            process.poll()  # reap direct children through Popen
                        for pid in known:
                            if any(p.pid == pid for p in processes):
                                continue
                            try:
                                os.waitpid(pid, os.WNOHANG)  # adopted grandchildren
                            except ChildProcessError:
                                pass
                        if not known or time.monotonic() >= deadline:
                            break
                        time.sleep(0.02)
            else:
                for process in processes:
                    if process.poll() is None:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            if self._linux:
                with self._lock:
                    type(self)._users -= 1
                    if not self._users:
                        ctypes.CDLL(None).prctl(36, self._previous_subreaper, 0, 0, 0)


async def daemon_call(function):
    """Cancelable UI work without executor threads delaying interpreter exit.

    HTTP calls still have their normal timeout, but a cancelled result can
    never keep the terminal/player alive while that timeout elapses.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(result, error):
        if not future.done():
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)

    def work():
        try:
            result, error = function(), None
        except BaseException as exc:
            result, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:  # the app has already closed its loop
            pass

    threading.Thread(target=work, daemon=True, name="ytm-request").start()
    return await future
