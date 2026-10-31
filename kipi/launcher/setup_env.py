"""One setup of KiPI's environment: what to tell the user, and the install itself.

The console and the window (:mod:`kipi.launcher.ui_console`, :mod:`kipi.launcher.ui_tk`)
show the same :class:`Summary` and run the same :func:`perform`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from kipi import __version__
from kipi.launcher import deps
from kipi.launcher.install import (
    PARDISO_PACKAGES, Canceled, InstallError, Installer, find_uv, remove_env,
)
from kipi.launcher.paths import env_python
from kipi.launcher.state import State, load, save
from kipi.launcher.system import Python

PARDISO_TITLE = "PARDISO, a faster solver for large boards (optional)"
PARDISO_PROS = [
    "Solves large rails several times faster: one 645k-unknown solve drops from about 8 s "
    "to 1.2 s, and a four-iteration electro-thermal run from about 90 s to 34 s.",
    "Uses every processor core, and solves rails of up to 1M unknowns directly (200k "
    "without it, beyond which KiPI switches to a slower iterative solver).",
]
PARDISO_CONS = [
    "Large: Intel's MKL library adds hundreds of MB.",
    "Small boards gain little.",
    "Intel x86-64 only; KiPI falls back to SciPy's solver if it ever fails to load.",
]


@dataclass
class Summary:
    """Everything the setup screen shows."""
    reason: str
    python: Python
    env: Path
    plan: deps.Plan
    pardiso_offered: bool
    pardiso_why_not: str        # why PARDISO is not offered here
    pardiso_default: bool       # what was chosen last time
    # The first line of the screen; empty for KiPI's own environment's. KiCad's plugin
    # environment for KiPI says so instead (#7).
    intro: str = ""

    def intro_text(self) -> str:
        return self.intro or (f"KiPI needs to set up its own Python environment: "
                              f"{self.reason}.")

    def size_line(self, with_pardiso: bool) -> str:
        down = self.plan.download(with_pardiso)
        on_disk = self.plan.installed(with_pardiso)
        n = len(self.plan.chosen(with_pardiso))
        if down is None:
            return f"{n} packages; sizes unknown"
        return f"{n} packages: {deps.fmt_size(down)} to download, about " \
               f"{deps.fmt_size(on_disk)} on disk"

    def pardiso_line(self) -> str:
        p = self.plan.pardiso
        down = sum(x.size or 0 for x in p)
        if not p or not down:
            return "adds Intel MKL (several hundred MB)"
        return f"adds {len(p)} packages: {deps.fmt_size(down)} to download, about " \
               f"{deps.fmt_size(int(down * deps.INSTALLED_PER_DOWNLOAD))} on disk"

    def source_line(self) -> str:
        if self.plan.source == "live":
            return "checked with PyPI just now"
        return self.plan.note or ""


class Progress:
    """A rough fraction done, from the installer's output: pip names each package as it
    collects it, then installs them all at once. uv's output does not show progress."""

    def __init__(self, n_packages: int):
        self.n = max(1, n_packages)
        self.collected = 0

    def feed(self, line: str) -> Optional[float]:
        if line.startswith("Collecting ") or re.match(r"Requirement already satisfied", line):
            self.collected += 1
            return min(0.85, 0.85 * self.collected / self.n)
        if line.startswith("Installing collected packages"):
            return 0.9
        if line.startswith("Successfully installed"):
            return 1.0
        return None


def perform(summary: Summary, pardiso: bool, data: Path, requirements: Path,
            on_line: Callable[[str], None], on_fraction: Callable[[Optional[float]], None],
            register: Callable[[Installer], None], fresh: bool = False,
            uv: Union[None, str, Sequence[str]] = "auto", owned: bool = True) -> State:
    """Make (or update) the environment and install everything; record it when done.
    ``fresh`` starts from an empty environment. ``owned`` False is an environment KiPI
    did not make (KiCad's plugin environment, #7): it is never made, deleted or started
    again, only installed into, as KiCad runs its pip. Raises Canceled or InstallError."""
    env = summary.env
    uv = find_uv() if uv == "auto" else uv
    environ = None
    if not owned:
        from kipi.launcher.kicad_env import pip_environ
        environ = pip_environ(env)
        if fresh or not env_python(env).is_file():
            raise InstallError(f"KiCad's plugin environment for KiPI has no Python ({env}). "
                               "In KiCad, recreate it under Preferences > Plugins.")
    previous = load(env)
    progress = Progress(len(summary.plan.chosen(pardiso)))

    def line(text: str):
        on_line(text)
        f = progress.feed(text)
        if f is not None:
            on_fraction(f)

    tools: list = [uv, None] if uv else [None]
    for attempt, tool in enumerate(tools):
        inst = Installer(summary.python, env, line, tool, environ=environ)
        register(inst)
        try:
            on_fraction(None)
            if owned and (fresh or not env_python(env).is_file()):
                remove_env(env, data)
                line(f"Making KiPI's environment in {env} (Python "
                     f"{summary.python.version_text}, {inst.tool})")
                inst.create_env()
            line("Installing KiPI's packages")
            inst.install(requirements, [deps.PARDISO] if pardiso else [])
            if not pardiso and previous is not None and previous.pardiso:
                line("Removing PARDISO")
                inst.uninstall([p.name for p in summary.plan.pardiso] or PARDISO_PACKAGES)
            result = State(requirements_hash=deps.requirements_hash(requirements),
                           pardiso=pardiso, python=summary.python.version_text,
                           kipi_version=__version__, installed=time.strftime("%Y-%m-%dT%H:%M:%S"),
                           tool=inst.tool)
            save(env, result)
            on_fraction(1.0)
            line("KiPI is set up")
            return result
        except Canceled:
            raise
        except InstallError as e:
            if attempt + 1 < len(tools):
                line(f"{inst.tool} failed ({e}); starting again with pip")
                # uv's environment has no pip of its own; KiCad's (made by venv) does
                fresh = owned
                continue
            raise
    raise InstallError("no installer to run")       # not reached
