"""Count the KiCad instances the current user has running.

KiPI talks to one KiCad through its IPC API. With two open, a standalone run can connect
to either one (kipy's default socket belongs to whichever started first), and a board
read from one can be mistaken for the other's, so KiPI refuses to start until only one
is left.

Each platform lists processes its own way, without extra dependencies:

* Windows: the Toolhelp snapshot, keeping processes in this logon session.
* Linux: ``/proc``, keeping processes owned by this user.
* macOS (and Linux without ``/proc``): ``ps``, which lists this user's processes.

A KiCad instance is the project manager (``kicad``) or a PCB editor started on its own
(``pcbnew``); each runs its own API server. An editor opened from the project manager
runs inside it, so it is not counted twice.

Everything here is best effort: if the processes cannot be listed, :func:`running_kicads`
returns None and KiPI carries on as if one KiCad were open.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

KICAD_NAMES = ("kicad", "pcbnew")
# Set to 1 to skip the check, e.g. if an unrelated program is named like KiCad.
ALLOW_ENV = "KIPI_ALLOW_MULTIPLE_KICAD"

Process = Tuple[int, str]      # (pid, executable name)


def is_kicad(name: str) -> bool:
    """True for a KiCad executable name or path (``kicad.exe``, ``.../MacOS/kicad``)."""
    stem = Path(name.strip()).name.lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    return stem in KICAD_NAMES


def kicads_in(processes: Iterable[Process]) -> List[Process]:
    return sorted((pid, name) for pid, name in processes if is_kicad(name))


def running_kicads() -> Optional[List[Process]]:
    """This user's running KiCad instances, or None if processes cannot be listed."""
    try:
        if sys.platform == "win32":
            procs = _windows_processes()
        elif Path("/proc/self").is_dir():
            procs = _proc_processes()
        else:
            procs = _ps_processes()
    except Exception:  # noqa: BLE001 - a failed check must never stop KiPI
        log.warning("Could not list running processes to count KiCad instances",
                    exc_info=True)
        return None
    return kicads_in(procs)


def multiple_kicads_message() -> Optional[str]:
    """Why KiPI cannot start if more than one KiCad is open, else None."""
    if os.environ.get(ALLOW_ENV, "").strip() not in ("", "0"):
        return None
    found = running_kicads()
    if found is None or len(found) <= 1:
        return None
    listed = ", ".join(f"{name} (PID {pid})" for pid, name in found)
    return (f"KiPI works with one KiCad at a time, but {len(found)} are open: {listed}.\n\n"
            "Close the other KiCad windows and start KiPI again from the PCB editor of the "
            "board you want to analyze.")


# --- Windows --------------------------------------------------------------------------

def _windows_processes() -> List[Process]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def session(pid):
        sid = wintypes.DWORD()
        return sid.value if k32.ProcessIdToSessionId(pid, ctypes.byref(sid)) else None

    TH32CS_SNAPPROCESS = 0x2
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    mine = session(os.getpid())
    procs = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = entry.th32ProcessID
            # Other users' sessions (fast user switching, terminal server) have their own
            # KiCad and their own API sockets; they do not get in the way.
            if mine is None or session(pid) in (mine, None):
                procs.append((pid, entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return procs


# --- Linux ----------------------------------------------------------------------------

def _proc_processes() -> List[Process]:
    uid = os.getuid()
    procs = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            procs.append((int(d.name), (d / "comm").read_text().strip()))
        except OSError:         # the process exited while we looked
            continue
    return procs


# --- macOS and other Unix -------------------------------------------------------------

def _ps_processes() -> List[Process]:
    # -x: this user's processes, including those without a terminal (GUI apps).
    out = subprocess.run(["ps", "-x", "-o", "pid=", "-o", "comm="], capture_output=True,
                         text=True, timeout=5, check=True).stdout
    procs = []
    for line in out.splitlines():
        pid, _, comm = line.strip().partition(" ")
        if pid.isdigit():
            procs.append((int(pid), comm.strip()))
    return procs
