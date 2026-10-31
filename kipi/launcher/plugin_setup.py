"""The KiCad plugin's first start: KiPI installs its packages into KiCad's plugin environment.

KiCad installs a plugin's ``requirements.txt`` in the background with nothing on screen,
and KiPI's packages are about 250 MB, so a new user waited minutes for a button. Now
``requirements.txt`` is only kicad-python, the button appears at once, and the first
click shows the same setup screen as standalone (the packages and their sizes, PARDISO,
Install) and installs ``requirements-standalone.txt`` into KiCad's environment with its
own pip, as KiCad runs it. KiCad never removes packages, so they stay through plugin
reloads; *Recreate Plugin Environment* empties it, and the next click sets up again.

:func:`ensure` runs in the plugin before anything imports KiPI's packages. The screen runs
in a process of its own (``python -m kipi.launcher.plugin_setup``), so its toolkit
(wx, which KiCad's Python has; else Tk; else the desktop's dialogs, see
:mod:`kipi.launcher.ui_native`) never shares a process with KiPI's Qt.

Standard library only (plus wx or Tk for the screen).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from kipi.launcher import deps, kicad_env, paths, system
from kipi.launcher.install import Canceled, InstallError
from kipi.launcher.setup_env import Summary, perform
from kipi.launcher.state import load

LOCK_NAME = "kipi-setup.lock"
LOCK_STALE_S = 3600                # a setup still "running" after an hour has died
FIRST = "this is KiPI's first start in it"
CHANGED = "this KiPI needs packages that are not installed yet"


def intro(reason: str) -> str:
    return ("KiCad made a plugin environment for KiPI with only what KiPI needs to start; "
            f"KiPI installs the rest of its packages into it ({reason}).")


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def reason(env: Path, requirements: Path,
           unmet: Callable[[Path], List[str]] = kicad_env.unmet) -> Optional[str]:
    """Why KiPI's packages have to be installed into ``env`` (this Python's), or None when
    they are there. An environment that has them all but no record of being set up (KiCad
    installed everything, before #7) or a record for other requirements is recorded as
    set up, without asking."""
    wanted = deps.requirements_hash(requirements)
    st = load(env)
    if st is not None and st.requirements_hash == wanted:
        return None
    if not unmet(requirements):
        kicad_env.adopt(env, wanted, ".".join(map(str, sys.version_info[:3])))
        return None
    return FIRST if st is None else CHANGED


# --- In the plugin --------------------------------------------------------------------------

def _take_lock(env: Path) -> bool:
    """One setup at a time: a second click while the screen is up does nothing."""
    path = env / LOCK_NAME
    try:
        if time.time() - path.stat().st_mtime > LOCK_STALE_S:
            path.unlink()
    except OSError:
        pass
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True                # can't lock (read-only?): go on unlocked


def _drop_lock(env: Path) -> None:
    try:
        (env / LOCK_NAME).unlink()
    except OSError:
        pass


def _lazy_log() -> Callable[[str], None]:
    """The launcher's log, opened only once there is something to say: most starts have
    nothing to set up."""
    opened = []

    def write(text: str) -> None:
        if not opened:
            from kipi.launcher.main import Log
            opened.append(Log(paths.log_dir()))
        opened[0].write(text)
    return write


def ensure(log: Optional[Callable[[str], None]] = None) -> bool:
    """Make sure KiPI's packages are in this plugin environment, showing the setup screen
    if they are not. True: go on and start KiPI; False: they are not there (canceled,
    failed, or another setup is running). Never raises."""
    log = log or _lazy_log()
    if not in_venv():
        return True                 # a system Python: nothing of KiPI's to install into
    env, req = Path(sys.prefix), paths.requirements_file()
    try:
        why = reason(env, req)
    except Exception as e:  # noqa: BLE001 - KiPI tries to start anyway
        log(f"Could not check KiPI's packages: {e}")
        return True
    if why is None:
        return True
    if not _take_lock(env):
        log("KiPI's setup is running already")
        return False
    try:
        log(f"Setting up KiPI's packages: {why}")
        environ = dict(os.environ)
        environ["PYTHONPATH"] = os.pathsep.join(
            [str(paths.KIPI_ROOT)] + ([environ["PYTHONPATH"]] if environ.get("PYTHONPATH") else []))
        code = subprocess.call([sys.executable, "-m", "kipi.launcher.plugin_setup"],
                               cwd=str(paths.KIPI_ROOT), env=environ, **system.child_kwargs())
        log(f"Setup finished ({code})")
        return code == 0 and reason(env, req) is None
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Could not start KiPI's setup: {e}")
        return False
    finally:
        _drop_lock(env)


# --- The setup screen's process -------------------------------------------------------------

def make_ui(log: Callable[[str], None]):
    """wx (KiCad's Python has it), else Tk, else the desktop's dialogs, else none."""
    try:
        from kipi.launcher import ui_wx
        if ui_wx.available():
            return ui_wx.WxUI()
    except Exception as e:  # noqa: BLE001 - no wx: the next one
        log(f"No wx window: {e}")
    if system.can_show_windows():
        try:
            from kipi.launcher.ui_tk import TkUI
            return TkUI()
        except Exception as e:  # noqa: BLE001
            log(f"No Tk window: {e}")
    from kipi.launcher.ui_native import NativeUI, SilentUI
    return NativeUI.find() or SilentUI(log)


def main(argv: Optional[List[str]] = None, ui=None) -> int:
    """0: set up; 1: canceled; 2: failed."""
    from kipi.launcher.main import Log
    from kipi.launcher.system import Python
    env, req = Path(sys.prefix), paths.requirements_file()
    data = paths.data_dir()
    log = Log(paths.log_dir(data))
    why = reason(env, req)
    if why is None:
        return 0
    log.write(f"Plugin setup in {env}: {why}")
    ui = ui or make_ui(log.write)
    python = Python(str(paths.env_python(env)), tuple(sys.version_info[:3]), True)
    pardiso_ok, pardiso_why = system.pardiso_supported()
    previous = load(env)
    ui.status("Checking which packages KiPI needs...")
    plan = deps.make_plan([python.executable], req, pardiso_ok)
    log.write(f"Plan ({plan.source or 'names only'}"
              + (f"; {plan.note}" if plan.note else "") + "): "
              + ", ".join(f"{p.name} {p.version}" for p in plan.packages))
    summary = Summary(why, python, env, plan, pardiso_ok, pardiso_why,
                      previous.pardiso if previous else pardiso_ok, intro=intro(why))
    choice = ui.confirm(summary)
    if choice is None:
        log.write("Canceled")
        ui.close()
        return 1

    def task(on_line, on_fraction, register):
        def line(text):
            log.write(text)
            on_line(text)
        return perform(summary, choice, data, req, line, on_fraction, register, owned=False)

    try:
        ui.install(task)
    except Canceled:
        log.write("Canceled")
        ui.error("setup was canceled. It starts again the next time you click KiPI.")
        return 1
    except (InstallError, OSError) as e:
        log.write(f"Failed: {e}")
        ui.error(f"{e}\n\nEverything the installer printed is in {log.path}")
        return 2
    ui.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
