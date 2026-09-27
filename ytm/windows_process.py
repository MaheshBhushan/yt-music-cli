"""Windows process-tree ownership with a kill-on-close Job Object (W2).

On Linux, `ProcessOwner` discovers and terminates descendants; on Windows
the old implementation terminated only directly tracked children, so an mpv
that had launched yt-dlp (and a JavaScript runtime) could leave helpers
behind on forced exit or startup failure.

One Job Object per interactive session carries
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: children join it before they can
run, descendants inherit membership automatically, and closing the job
handle terminates everything still inside. Children are created suspended,
assigned, then resumed, so nothing can spawn before ownership exists.

The job handle is created non-inheritable (the ``CreateJobObject`` default),
so a child cannot keep the job alive after the owner exits. Windows 8+
supports nested jobs, so running inside another job still works; if
assignment does fail, the suspended child is terminated and the error is
reported instead of silently running an unowned process tree.

Only playback helpers join this job. Normal login browsers and the detached
updater are launched elsewhere on purpose (see README, "Shutdown and
ownership").
"""

import ctypes
import subprocess
import sys
from ctypes import wintypes


class ProcessOwnershipError(OSError):
    """The session could not establish ownership of a child process."""


if sys.platform.startswith("win"):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    PROCESS_SUSPEND_RESUME = 0x0800
    CREATE_SUSPENDED = 0x00000004

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long


class JobObject:
    """One kill-on-close job; the interactive session owns the handle."""

    def __init__(self):
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ProcessOwnershipError(
                ctypes.get_last_error(), "could not create the playback job"
            )
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            self._handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        )
        if not configured:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(self._handle)
            self._handle = None
            raise ProcessOwnershipError(
                error, "could not configure the playback job"
            )
        self._closed = False

    def assign(self, process_handle):
        if self._closed:
            raise ProcessOwnershipError("the playback job is already closed")
        if not kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ProcessOwnershipError(
                ctypes.get_last_error(), "could not assign the player to its job"
            )

    def terminate(self):
        """Terminate every process still in the job (graceful exit first)."""
        if self._handle and not self._closed:
            kernel32.TerminateJobObject(self._handle, 1)

    def close(self):
        """Close the job handle; kill-on-close removes any survivors."""
        if self._closed:
            return
        self._closed = True
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None


def spawn_in_job(job, args, **kwargs):
    """Start `args` suspended, assign it to `job`, then let it run.

    Public `subprocess.Popen` cannot resume a suspended process (CPython
    discards the primary-thread handle), so resumption goes through
    ``NtResumeProcess`` on a handle we open by pid. The process cannot run
    -- and therefore cannot spawn anything -- until assignment succeeded.
    """
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
    process = subprocess.Popen(args, **kwargs)
    handle = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME, False, process.pid
    )
    if not handle:
        error = ctypes.get_last_error()
        process.kill()
        process.wait()
        raise ProcessOwnershipError(
            error, "could not open the new player process"
        )
    try:
        job.assign(handle)
        status = ntdll.NtResumeProcess(handle)
        if status != 0:
            raise ProcessOwnershipError(
                f"could not resume the player process (NTSTATUS 0x{status & 0xffffffff:08x})"
            )
    except BaseException:
        # Never leave a suspended orphan: terminate it while we still can.
        kernel32.TerminateProcess(handle, 1)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        kernel32.CloseHandle(handle)
    return process
