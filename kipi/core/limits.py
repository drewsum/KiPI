# Copyright (c) 2026 Tomachie LLC.
"""Limits on what a headless solve may take from a shared machine: threads, memory and
priority, so a solve beside other work (a server's other jobs) cannot starve it.

Call :func:`apply` before NumPy, SciPy or PARDISO are imported: the BLAS and MKL thread
pools read their thread counts once, when they load. This module imports neither.

The memory cap is a Windows Job Object limit on the process's committed memory: an
allocation above it fails (MemoryError in Python, an out-of-memory error from PARDISO)
instead of the machine paging. Elsewhere it is RLIMIT_AS.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

# The thread pools NumPy, SciPy, PARDISO (MKL) and pyamg draw on.
THREAD_VARIABLES = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")


def limit_threads(n: int) -> None:
    for name in THREAD_VARIABLES:
        os.environ[name] = str(n)


def _win_job_memory(limit_bytes: int) -> None:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes",
                     "other_bytes")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("per_process_user_time", ctypes.c_int64),
                    ("per_job_user_time", ctypes.c_int64),
                    ("limit_flags", wintypes.DWORD),
                    ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t),
                    ("active_process_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t),
                    ("priority_class", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                    ("process_memory_limit", ctypes.c_size_t),
                    ("job_memory_limit", ctypes.c_size_t),
                    ("peak_process_memory_used", ctypes.c_size_t),
                    ("peak_job_memory_used", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
    info = ExtendedLimits()
    info.basic.limit_flags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.process_memory_limit = limit_bytes
    if not k32.SetInformationJobObject(wintypes.HANDLE(job),
                                       JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not k32.AssignProcessToJobObject(wintypes.HANDLE(job),
                                        wintypes.HANDLE(k32.GetCurrentProcess())):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")


def limit_memory(gb: float) -> bool:
    """Cap the process's memory at ``gb``; False where the system cannot (macOS takes no
    address-space limit), said in the log."""
    limit = int(gb * 1024 ** 3)
    if sys.platform == "win32":
        _win_job_memory(limit)
        return True
    import resource
    if sys.platform == "darwin":
        logging.getLogger(__name__).warning(
            "macOS cannot cap a process's memory; solving without the %g GB cap", gb)
        return False
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)            # a soft limit may not exceed the hard one
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    return True


def lower_priority() -> None:
    """Below normal: other work on the machine is served first."""
    if sys.platform == "win32":
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass(ctypes.c_void_p(k32.GetCurrentProcess()),
                             BELOW_NORMAL_PRIORITY_CLASS)
    else:
        os.nice(10)


def apply(threads: Optional[int] = None, memory_gb: Optional[float] = None,
          low_priority: bool = True) -> Optional[float]:
    """Apply the limits; the memory cap set, GB (None: none asked, or the system takes
    none)."""
    if threads:
        limit_threads(threads)
    capped = memory_gb if memory_gb and limit_memory(memory_gb) else None
    if low_priority:
        lower_priority()
    return capped


def peak_memory_gb() -> Optional[float]:
    """The process's peak memory so far (Windows: peak commit), GB; None if unknown."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD),
                            ("peak_working_set", ctypes.c_size_t),
                            ("working_set", ctypes.c_size_t),
                            ("peak_paged_pool", ctypes.c_size_t),
                            ("paged_pool", ctypes.c_size_t),
                            ("peak_nonpaged_pool", ctypes.c_size_t),
                            ("nonpaged_pool", ctypes.c_size_t),
                            ("pagefile", ctypes.c_size_t),
                            ("peak_pagefile", ctypes.c_size_t)]

            c = Counters()
            c.cb = ctypes.sizeof(c)
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi = ctypes.WinDLL("psapi")
            if not psapi.GetProcessMemoryInfo(ctypes.c_void_p(k32.GetCurrentProcess()),
                                              ctypes.byref(c), c.cb):
                return None
            return c.peak_pagefile / 1024 ** 3
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 ** 2
    except Exception:  # noqa: BLE001 - a measurement only
        return None
