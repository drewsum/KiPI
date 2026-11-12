"""What this computer can run: a Python for KiPI's environment, and the PARDISO solver."""

from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

#: Pythons KiPI's dependencies have prebuilt packages for (Triangle and PyAMG have none for
#: 3.14 yet, so pip would have to compile them).
SUPPORTED = ((3, 10), (3, 13))


def is_supported(version: Sequence[int]) -> bool:
    return SUPPORTED[0] <= tuple(version[:2]) <= SUPPORTED[1]


def supported_text() -> str:
    (a, b), (c, d) = SUPPORTED
    return f"Python {a}.{b} to {c}.{d}"


def pardiso_supported(system: Optional[str] = None,
                      machine: Optional[str] = None) -> Tuple[bool, str]:
    """Whether PARDISO can be installed here, and if not, why. Intel MKL, which it needs,
    is published for Windows and Linux on x86-64 only."""
    system = system or _platform.system()
    machine = (machine or _platform.machine()).lower()
    if system not in ("Windows", "Linux"):
        return False, f"Intel MKL is not available for {system}"
    if machine not in ("amd64", "x86_64", "x64"):
        return False, f"Intel MKL is not available for {machine} processors"
    return True, ""


# --- Finding a Python for the environment ------------------------------------------------

@dataclass(frozen=True)
class Python:
    executable: str
    version: Tuple[int, int, int]
    has_venv: bool               # the venv module and ensurepip work (Debian splits them out)

    @property
    def version_text(self) -> str:
        return ".".join(str(v) for v in self.version)


_PROBE = ("import sys, json\n"
          "try:\n"
          "    import venv, ensurepip\n"
          "    ok = True\n"
          "except ImportError:\n"
          "    ok = False\n"
          "print(json.dumps([sys.executable, list(sys.version_info[:3]), ok]))\n")


def probe(command: Sequence[str], timeout: float = 20.0) -> Optional[Python]:
    """What Python ``command`` runs, or None if it does not run."""
    import json
    try:
        out = subprocess.run(list(command) + ["-c", _PROBE], capture_output=True, text=True,
                             timeout=timeout, **child_kwargs())
        exe, version, ok = json.loads(out.stdout.strip().splitlines()[-1])
        return Python(exe, tuple(version), bool(ok))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def extra_dirs(platform: Optional[str] = None, home: Optional[str] = None) -> List[str]:
    """Where Python installers put their commands, beyond PATH. An app started from the
    Dock or Finder (KiCad, so its plugins) gets launchd's PATH, /usr/bin:/bin:/usr/sbin:
    /sbin, which has none of them (#237)."""
    platform = platform or sys.platform
    if platform == "win32":
        return []
    home = home or os.path.expanduser("~")
    dirs = ["/opt/homebrew/bin", "/usr/local/bin", f"{home}/.local/bin"]
    if platform == "darwin":            # python.org's installer
        dirs += [f"/Library/Frameworks/Python.framework/Versions/3.{m}/bin"
                 for m in range(SUPPORTED[1][1], SUPPORTED[0][1] - 1, -1)]
    return dirs


def _which(name: str) -> Optional[str]:
    path = os.pathsep.join([os.environ.get("PATH", os.defpath)] + extra_dirs())
    return shutil.which(name, path=path)


def candidates(platform: Optional[str] = None,
               which: Callable[[str], Optional[str]] = _which) -> List[List[str]]:
    """Commands that may run a supported Python, best first: the one running the launcher,
    then the newest supported version the system can find."""
    platform = platform or sys.platform
    found = [[sys.executable]]
    minors = range(SUPPORTED[1][1], SUPPORTED[0][1] - 1, -1)
    if platform == "win32":
        py = which("py")
        if py:
            found += [[py, f"-3.{m}"] for m in minors]
    else:
        found += [[p] for p in (which(f"python3.{m}") for m in minors) if p]
    uv = which("uv")
    if uv:                       # Pythons uv installed (it never downloads one here)
        found += [[p] for p in (_uv_python(uv, m) for m in minors) if p]
    return found


def _uv_python(uv: str, minor: int) -> Optional[str]:
    try:
        # --system: never a virtual environment that happens to be near the current folder
        out = subprocess.run([uv, "python", "find", "--system", f"3.{minor}"],
                             capture_output=True, text=True, timeout=20, **child_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    path = out.stdout.strip()
    return path if out.returncode == 0 and path else None


def find_python(commands: Optional[List[List[str]]] = None,
                probe_fn: Callable[[Sequence[str]], Optional[Python]] = probe
                ) -> Tuple[Optional[Python], List[str]]:
    """The first supported Python that can make a virtual environment, and what was wrong
    with the others (for the message when there is none)."""
    problems: List[str] = []
    seen = set()
    for cmd in commands if commands is not None else candidates():
        p = probe_fn(cmd)
        if p is None or p.executable in seen:
            continue
        seen.add(p.executable)
        if not is_supported(p.version):
            problems.append(f"{p.executable} is Python {p.version_text}")
        elif not p.has_venv:
            problems.append(f"{p.executable} (Python {p.version_text}) cannot make virtual "
                            "environments; on Debian or Ubuntu install python3-venv")
        else:
            return p, problems
    return None, problems


#: What KiCad sets for its own Python on macOS and passes on to every process it starts,
#: plugins included. Any other Python started with them looks for its standard library in
#: KiCad's Python 3.9 and stops before it runs a line ("No module named 'encodings'", #237).
KICAD_PYTHON_VARS = ("PYTHONHOME", "PYTHONPATH")


def drop_kicad_python_vars(environ=None) -> None:
    """Remove KiCad's Python home and path from ``environ`` (this process's environment,
    so every child gets a clean one)."""
    environ = os.environ if environ is None else environ
    for name in KICAD_PYTHON_VARS:
        environ.pop(name, None)


def child_kwargs() -> dict:
    """For every process the launcher starts: no stdin (started with no console, as
    kipi_launcher.pyw is, there is none to inherit, and Windows refuses to start a child
    that inherits an invalid handle), and no console window flashing up (Windows)."""
    kwargs = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000    # CREATE_NO_WINDOW
    return kwargs


def can_show_windows() -> bool:
    """Whether a Tk window can open: tkinter is there and so is a display."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def wait_for_exit(pid: int, timeout: float = 60.0, poll: float = 0.2) -> bool:
    """Wait for process ``pid`` to end; False if it is still running after ``timeout``."""
    import time
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return True                 # gone already (or never ours to see)
        try:
            ms = max(0, int(timeout * 1000))
            return ctypes.windll.kernel32.WaitForSingleObject(handle, ms) != WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    while time.monotonic() < deadline:
        if _ended(int(pid)):
            return True
        time.sleep(poll)
    return False


def _ended(pid: int) -> bool:
    """POSIX: whether ``pid`` has ended. A process that has exited stays as a zombie, which
    ``kill(pid, 0)`` still finds, until its parent collects it: a child of ours is
    collected here, and anyone else's zombie counts as ended."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except ChildProcessError:           # not our child
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:             # alive, but someone else's
        return False
    try:                                # Linux: a zombie's state is Z
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        return stat.rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False
