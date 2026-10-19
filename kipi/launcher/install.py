"""Making KiPI's environment and installing packages into it.

uv makes both much faster, so it is used when it can be found: on the PATH, or run through
uvx or pipx, which fetch it (#113). Otherwise, or when uv fails, the standard ``venv``
module and pip do the same. Each command's output goes to
``on_line`` as it comes, and :meth:`Installer.cancel` stops the one running.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

from kipi.launcher.paths import env_python
from kipi.launcher.system import Python, child_kwargs

#: Everything PARDISO brings with it, for when it is removed and no live list is known.
PARDISO_PACKAGES = ["pypardiso", "mkl", "intel-openmp", "onemkl-license", "tbb",
                    "intel-cmplr-lib-ur", "tcmlib", "umf"]


class InstallError(RuntimeError):
    pass


class Canceled(Exception):
    pass


class Installer:
    def __init__(self, python: Python, env: Path, on_line: Callable[[str], None],
                 uv: Union[None, str, Sequence[str]] = None):
        """``uv`` is the uv program to use, or the command that runs it (``["pipx", "run",
        "uv"]``), or None for venv and pip."""
        self.python = python
        self.env = env
        self.on_line = on_line
        self.uv: Optional[List[str]] = [uv] if isinstance(uv, str) else list(uv) if uv else None
        self._proc: Optional[subprocess.Popen] = None
        self._canceled = threading.Event()

    @property
    def tool(self) -> str:
        return uv_label(self.uv) if self.uv else "pip"

    # --- commands ---------------------------------------------------------------------
    def create_env(self) -> None:
        if self.uv:
            self._run([*self.uv, "venv", "--python", self.python.executable, str(self.env)])
        else:
            self._run([self.python.executable, "-m", "venv", str(self.env)])
        if not env_python(self.env).is_file():
            raise InstallError(f"the environment was made but has no Python: {self.env}")

    def install(self, requirements: Optional[Path] = None, packages: Sequence[str] = (),
                options: Sequence[str] = ()) -> None:
        args = list(options) + (["-r", str(requirements)] if requirements else []) + list(packages)
        if self.uv:
            self._run([*self.uv, "pip", "install", "--python", str(env_python(self.env)), *args])
        else:
            self._run([str(env_python(self.env)), "-m", "pip", "install",
                       "--disable-pip-version-check", *args])

    def uninstall(self, packages: Sequence[str]) -> None:
        if self.uv:
            self._run([*self.uv, "pip", "uninstall", "--python", str(env_python(self.env)),
                       *packages])
        else:
            self._run([str(env_python(self.env)), "-m", "pip", "uninstall", "-y",
                       "--disable-pip-version-check", *packages])

    # --- running ----------------------------------------------------------------------
    def cancel(self) -> None:
        self._canceled.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _run(self, cmd: List[str]) -> None:
        if self._canceled.is_set():
            raise Canceled()
        self.on_line("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1, **child_kwargs())
        except OSError as e:
            raise InstallError(f"could not run {cmd[0]}: {e}") from e
        self._proc = proc
        tail: List[str] = []
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.on_line(line)
                    tail = (tail + [line])[-8:]
            proc.wait()
        finally:
            self._proc = None
        if self._canceled.is_set():
            raise Canceled()
        if proc.returncode != 0:
            raise InstallError(f"{Path(cmd[0]).name} failed (exit {proc.returncode}): "
                               + " | ".join(tail[-3:]))


def find_uv(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[List[str]]:
    """The command that runs uv: uv itself, else uvx or pipx running it (each fetches uv
    the first time). None if there is none of them, for venv and pip."""
    uv = which("uv")
    if uv:
        return [uv]
    uvx = which("uvx")
    if uvx:
        beside = Path(uvx).with_name(Path(uvx).name.replace("uvx", "uv", 1))
        return [str(beside)] if beside.is_file() else [uvx, "uv"]
    pipx = which("pipx")
    if pipx:
        return [pipx, "run", "uv"]
    return None


def uv_label(cmd: Sequence[str]) -> str:
    """How uv is run, for the log and the setup record: "uv", "uv (uvx)", "uv (pipx)"."""
    first = Path(cmd[0]).stem.lower()
    return "uv" if len(cmd) == 1 else f"uv ({first})"


def remove_env(env: Path, data: Path) -> None:
    """Delete KiPI's environment, and nothing else: only a folder named ``venv`` directly
    in the launcher's data folder that is a virtual environment (or empty)."""
    env, data = env.resolve(), data.resolve()
    if not env.exists():
        return
    if env.parent != data or env.name != "venv":
        raise InstallError(f"refusing to delete {env}: not KiPI's environment")
    if any(env.iterdir()) and not (env / "pyvenv.cfg").is_file():
        raise InstallError(f"refusing to delete {env}: not a virtual environment")
    shutil.rmtree(env)
