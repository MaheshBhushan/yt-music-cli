"""W2: the Windows job-ownership contract, tested with injected fakes.

The real Job Object and suspended launch are Windows-only; these tests
prove the owner routes every child through the job, releases it exactly
once, and rejects work after close. `tests/test_windows_process_native.py`
proves the kernel behavior on Windows (and is skipped elsewhere).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ytm.lifecycle import ProcessOwner


class FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = -1


class FakeJob:
    def __init__(self):
        self.assigned = []
        self.terminated = 0
        self.closed = 0

    def assign(self, handle):
        self.assigned.append(handle)

    def terminate(self):
        self.terminated += 1

    def close(self):
        self.closed += 1


def windows_owner(monkeypatch, job=None, spawner=None):
    job = job or FakeJob()
    spawned = []

    def default_spawn(job_arg, args, **kwargs):
        process = FakeProcess(pid=1000 + len(spawned))
        spawned.append((job_arg, args, kwargs, process))
        return process

    owner = ProcessOwner(
        platform="win32",
        windows_job=job,
        windows_spawn=spawner or default_spawn,
    )
    return owner, job, spawned


def test_windows_owner_spawns_every_child_through_the_job(monkeypatch):
    owner, job, spawned = windows_owner(monkeypatch)
    process = owner.spawn(["mpv", "--idle"])
    assert spawned and spawned[0][0] is job
    assert spawned[0][1] == ["mpv", "--idle"]
    assert spawned[0][2]["env"]["YTM_PROCESS_OWNER"] == owner._token
    assert process is spawned[0][3]
    owner.close()


def test_windows_owner_close_terminates_and_closes_the_job_once(monkeypatch):
    owner, job, _ = windows_owner(monkeypatch)
    owner.spawn(["mpv", "--idle"])
    owner.close()
    owner.close()
    assert job.terminated == 1
    assert job.closed == 1


def test_windows_owner_rejects_spawn_after_close(monkeypatch):
    owner, job, spawned = windows_owner(monkeypatch)
    owner.close()
    with pytest.raises(OSError, match="shutting down"):
        owner.spawn(["mpv", "--idle"])
    assert spawned == []


def test_windows_owner_propagates_a_failed_assignment(monkeypatch):
    from ytm.windows_process import ProcessOwnershipError

    def failing_spawn(job, args, **kwargs):
        raise ProcessOwnershipError(5, "assignment refused")

    owner, job, _ = windows_owner(monkeypatch, spawner=failing_spawn)
    with pytest.raises(ProcessOwnershipError, match="assignment refused"):
        owner.spawn(["mpv", "--idle"])
    assert owner._processes == []
    owner.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership branch")
def test_linux_owner_has_no_windows_job():
    owner = ProcessOwner()
    try:
        assert owner._job is None
        assert not owner._windows
    finally:
        owner.close()


def test_linux_spawn_tracks_a_real_child():
    """The default owner on this host still uses plain Popen and tracking."""
    owner = ProcessOwner()
    try:
        process = owner.spawn([sys.executable, "-c", "pass"])
        process.wait(timeout=5)
        assert process.pid in [tracked.pid for tracked in owner._processes]
    finally:
        owner.close()


def test_updater_spawns_detached_and_never_joins_the_job(tmp_path, monkeypatch):
    """The updater deliberately outlives the app: a plain detached Popen."""
    from ytm import update

    calls = []
    monkeypatch.setattr(
        update.shutil, "which",
        lambda name: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        if name == "powershell.exe" else name,
    )
    monkeypatch.setattr(
        update, "upgrade_commands",
        lambda kind=None, target=None: [["pip", "install", "-U", "ytm"]],
    )
    monkeypatch.setattr(
        update.subprocess, "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(update.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    folder = tmp_path / "ytm-update"
    folder.mkdir()
    monkeypatch.setattr(update.tempfile, "mkdtemp", lambda prefix: str(folder))
    monkeypatch.setattr(update.shutil, "copyfile", lambda src, dst: Path(dst).write_text("script"))

    ok, text = update.start_windows_upgrade(kind="pip", target=None)
    assert ok and "Update window opened" in text
    assert calls and calls[0][1]["creationflags"] == 0x10
    # no session owner or job is involved: the helper must survive close
    assert "ProcessOwner" not in repr(calls)
