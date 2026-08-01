"""Windows job objects: children that cannot outlive their launcher.

``Popen.terminate`` is not enough here for two reasons: a venv's python.exe is
a shim that re-executes the real interpreter (so terminating the shim can
orphan the child), and a launcher that is force-killed never runs cleanup at
all. Observed in practice: stopping the launcher left six processes running.

A job object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` makes the *operating
system* responsible: when the launcher's handle closes - gracefully, crashed,
or killed - every process in the job is terminated. Non-Windows platforms get
a no-op object, so callers need no branching.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

from myagent.logging import get_logger

log = get_logger(__name__)

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
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


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class ProcessGroup:
    """Container whose members die when this object is closed."""

    def __init__(self) -> None:
        self._handle: int | None = None
        if sys.platform != "win32":
            return
        kernel32: Any = ctypes.windll.kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            log.warning("job_object_unavailable")
            return
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            log.warning("job_object_limit_failed")
            kernel32.CloseHandle(handle)
            return
        self._handle = handle

    @property
    def active(self) -> bool:
        """True when the OS will enforce cleanup for us."""
        return self._handle is not None

    def add(self, pid: int) -> bool:
        """Put a process (and its future children) under this group."""
        if self._handle is None:
            return False
        kernel32: Any = ctypes.windll.kernel32
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            log.warning("job_object_open_failed", pid=pid)
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self._handle, process))
        finally:
            kernel32.CloseHandle(process)

    def close(self) -> None:
        """Close the group, which terminates every member."""
        if self._handle is None:
            return
        ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None


def kill_tree(pid: int) -> None:
    """Terminate a process and its descendants (graceful-stop path)."""
    if sys.platform != "win32":
        return
    import subprocess

    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
    )
