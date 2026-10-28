"""Memory free on this machine, for sizing work that would otherwise run out of it: the
SuperLU threads of an AC sweep (#213), direct solves of large DC systems (#214) and blocks
of right-hand sides (#215)."""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional


def free_memory() -> Optional[int]:
    """Bytes of memory free for use now; None where they can't be read."""
    try:
        if sys.platform == "win32":
            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = Status()
            st.dwLength = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullAvailPhys)
            return None
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
            return None
        if sys.platform == "darwin":
            # macOS gives no figure for memory free that counts what it would hand back
            # from caches; half of all memory stands in for it.
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2
    except (OSError, ValueError, AttributeError):
        return None
    return None
