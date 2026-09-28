"""W2 native validation: real Job Object tree cleanup on Windows.

Skipped everywhere else with an explicit reason. These tests prove the
kernel behavior (kill-on-close, assignment-before-execution, owner-crash
cleanup), which a mocked platform branch cannot.
"""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

import pytest

from ytm.lifecycle import ProcessOwner

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="native Windows job-object tests (run in the Windows CI job)",
)

if sys.platform.startswith("win"):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

#: a child that immediately spawns a grandchild and reports its pid
CHILD_SPAWNS_GRANDCHILD = (
    "import subprocess, sys, time;"
    "grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "open(sys.argv[1], 'w').write(str(grand.pid));"
    "time.sleep(60)"
)

#: an independent owner session that is killed abruptly later
OWNER_SESSION = (
    "import sys, time;"
    "from ytm.lifecycle import ProcessOwner;"
    "owner = ProcessOwner();"
    "child = owner.spawn([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "open(sys.argv[1], 'w').write(str(child.pid));"
    "time.sleep(60)"
)


def _alive(pid):
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _wait_gone(pid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _wait_for_file(path, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


@pytest.fixture
def unrelated():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield process
    if process.poll() is None:
        process.kill()
    process.wait()


def test_windows_owner_terminates_grandchildren(tmp_path, unrelated):
    owner = ProcessOwner()
    try:
        pid_file = tmp_path / "grand.pid"
        child = owner.spawn([sys.executable, "-c", CHILD_SPAWNS_GRANDCHILD, str(pid_file)])
        assert _wait_for_file(pid_file), "the child never reported its grandchild"
        grandchild = int(pid_file.read_text())
        owner.close()
        assert _wait_gone(child.pid), "the direct child survived close"
        assert _wait_gone(grandchild), "the grandchild survived close"
        assert _alive(unrelated.pid), "an unrelated process was terminated"
    finally:
        owner.close()


def test_windows_owner_assignment_precedes_child_execution(tmp_path):
    """A child that spawns immediately cannot outrun job association."""
    owner = ProcessOwner()
    try:
        pid_file = tmp_path / "early.pid"
        child = owner.spawn([sys.executable, "-c", CHILD_SPAWNS_GRANDCHILD, str(pid_file)])
        assert _wait_for_file(pid_file)
        grandchild = int(pid_file.read_text())
        owner.close()
        assert _wait_gone(child.pid)
        assert _wait_gone(grandchild)
    finally:
        owner.close()


def test_windows_two_owners_are_isolated(tmp_path):
    first = ProcessOwner()
    second = ProcessOwner()
    try:
        first_child = first.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        second_child = second.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        first.close()
        assert _wait_gone(first_child.pid)
        assert _alive(second_child.pid), "one owner terminated another owner's child"
    finally:
        first.close()
        second.close()
    assert _wait_gone(second_child.pid)


def test_windows_owner_crash_closes_owned_tree(tmp_path):
    """Kill-on-close fires when the owner process dies without cleanup."""
    pid_file = tmp_path / "session.pid"
    helper = subprocess.Popen([sys.executable, "-c", OWNER_SESSION, str(pid_file)])
    try:
        assert _wait_for_file(pid_file), "the helper session never started its child"
        child_pid = int(pid_file.read_text())
        helper.kill()  # no polite close(): the process just dies
        helper.wait()
        assert _wait_gone(child_pid), "the owned child outlived its dead owner"
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait()


def test_windows_failed_assignment_leaves_no_suspended_child(tmp_path, monkeypatch):
    from ytm import windows_process

    job = windows_process.JobObject()
    job.close()  # assignment now refuses
    marker = tmp_path / "ran"
    with pytest.raises(windows_process.ProcessOwnershipError):
        windows_process.spawn_in_job(
            job, [sys.executable, "-c", f"open(r'{marker}', 'w').close()"]
        )
    time.sleep(0.3)
    assert not marker.exists(), "a suspended orphan was left to run"


def test_windows_updater_lifetime_is_independent(tmp_path):
    """A deliberately detached child (the updater pattern) survives close."""
    owner = ProcessOwner()
    detached = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        owner.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        owner.close()
        assert _alive(detached.pid), "the detached helper was caught by the job"
    finally:
        owner.close()
        if detached.poll() is None:
            detached.kill()
        detached.wait()
