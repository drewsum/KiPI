"""The launcher: set up KiPI's environment when it needs it, then start KiPI in it.

    python kipi_launcher.py [board]       # start KiPI (setting up first if needed)
    python kipi_launcher.py --setup       # change the setup (e.g. add PARDISO), then start
    python kipi_launcher.py --repair      # reinstall everything from scratch
    python kipi_launcher.py --reset       # delete KiPI's environment; set up on next start
    python kipi_launcher.py --add-pardiso | --remove-pardiso
    python kipi_launcher.py --create-shortcut   # add KiPI to the Start menu / app menu / Applications
    python kipi_launcher.py --own-env     # KiPI's own environment, even beside KiCad's

``kipi_launcher.pyw`` does the same with no console window (double-click it).

A KiPI in KiCad's plugin folders (the Plugin and Content Manager's, or a copy in
``plugins``) runs in the environment KiCad made for its plugin, adding its packages to it
if they are not there, rather than in a second one of its own (#7). If KiCad has not made
it, or the Python it was made from is gone, KiPI uses its own as before.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from kipi.launcher import deps, paths, system
from kipi.launcher.install import Canceled, InstallError, remove_env
from kipi.launcher.setup_env import Summary, perform
from kipi.launcher.state import load, needs_setup

#: Set for the KiPI the launcher starts: the launcher that started it, and the Python that
#: ran it (outside KiPI's environment, so it can rebuild it). About's "Manage
#: dependencies" starts the launcher again with them.
LAUNCHER_ENV = "KIPI_LAUNCHER"
LAUNCHER_PYTHON_ENV = "KIPI_LAUNCHER_PYTHON"
KEEP_LOGS = 10
KICAD_INTRO = ("KiPI runs in the plugin environment KiCad made for it, and installs the rest "
               "of its packages into it ({reason}).")


class Log:
    """The launcher's own log: every line the installers print, for when setup fails."""

    def __init__(self, folder: Path):
        self.path: Optional[Path] = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / f"launcher-{time.strftime('%Y%m%d-%H%M%S')}.log"
            self._f = open(self.path, "a", encoding="utf-8")
            old = sorted(folder.glob("launcher-*.log"), key=lambda p: p.stat().st_mtime)
            for p in old[:-KEEP_LOGS]:
                p.unlink()
        except OSError:
            self._f = None

    def write(self, text: str) -> None:
        if self._f is not None:
            self._f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            self._f.flush()


def parse(argv: Optional[List[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="kipi_launcher", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?", help="board file to open")
    what = ap.add_mutually_exclusive_group()
    what.add_argument("--setup", action="store_true", help="show the setup again")
    what.add_argument("--repair", action="store_true", help="reinstall from scratch")
    what.add_argument("--reset", action="store_true", help="delete KiPI's environment")
    what.add_argument("--add-pardiso", action="store_true", help="install PARDISO")
    what.add_argument("--remove-pardiso", action="store_true", help="remove PARDISO")
    what.add_argument("--create-shortcut", action="store_true",
                      help="add KiPI to the Start menu (Windows), applications menu "
                           "(Linux) or Applications folder (macOS)")
    ap.add_argument("--console", action="store_true", help="set up in this terminal")
    ap.add_argument("--own-env", action="store_true",
                    help="use KiPI's own environment, not the one KiCad made for its plugin")
    ap.add_argument("--no-launch", action="store_true", help="set up only; do not start KiPI")
    # KiPI asking for its own setup to change: wait for it to close, as its environment
    # cannot change while it runs.
    ap.add_argument("--after-pid", type=int, action="append", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def make_ui(console: bool, windowed: bool):
    from kipi.launcher.ui_console import ConsoleUI
    if not console and system.can_show_windows():
        try:
            from kipi.launcher.ui_tk import TkUI
            return TkUI()
        except Exception:  # noqa: BLE001 - no window after all: fall back to the terminal
            pass
    if not console:
        try:                # KiCad's Python: wx but no Tk (#7)
            from kipi.launcher import ui_wx
            if ui_wx.available():
                return ui_wx.WxUI()
        except Exception:  # noqa: BLE001
            pass
    if windowed:
        return _MessageBoxUI()
    return ConsoleUI()


class _MessageBoxUI:
    """Started with no console and no way to open a window (no tkinter): say how to set
    KiPI up from a terminal instead, the only thing left to do."""

    def _box(self, text: str) -> None:
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, text, "KiPI", 0x10)
                return
            except Exception:  # noqa: BLE001
                pass
        print(text, file=sys.stderr)

    def confirm(self, s: Summary) -> Optional[bool]:
        self._box("KiPI needs to set up its Python environment, and this Python cannot open "
                  "a setup window (it has no tkinter). Run this in a terminal instead:\n\n"
                  f"python \"{paths.KIPI_ROOT / 'kipi_launcher.py'}\"")
        return None

    def install(self, task):
        return task(lambda line: None, lambda f: None, lambda inst: None)

    def status(self, text: str) -> None:
        pass

    def info(self, text: str) -> None:
        pass

    def error(self, text: str) -> None:
        self._box("KiPI could not start: " + text)

    def close(self) -> None:
        pass


def choose_python(env: Path, fresh: bool):
    """A Python from the system for the environment, which also asks pip what to install.
    An existing environment's own Python is the last resort, for an update only: one made
    by uv has no pip, so the package list then comes from the release's."""
    commands = system.candidates()
    if not fresh and paths.env_python(env).is_file():
        commands = commands + [[str(paths.env_python(env))]]
    return system.find_python(commands)


def kicad_env_for(own_env: bool, log: "Log") -> Optional[Path]:
    """KiCad's plugin environment to run in, if KiPI is in KiCad's plugin folders and it
    can run KiPI (#7); else None, for KiPI's own."""
    if own_env:
        return None
    from kipi.launcher import kicad_env
    env, why = kicad_env.find(paths.KIPI_ROOT)
    if env is None:
        if kicad_env.kicad_version(paths.KIPI_ROOT):
            log.write(f"Not using KiCad's plugin environment: {why}")
        return None
    py = system.probe([str(paths.env_python(env))])
    if py is None or not system.is_supported(py.version):
        log.write(f"Not using KiCad's plugin environment {env}: its Python "
                  f"{py.version_text if py else 'does not run'} ({system.supported_text()} "
                  "is needed)")
        return None
    log.write(f"Using KiCad's plugin environment {env}")
    return env


def kicad_reason(env: Path, req: Path, log: "Log") -> Optional[str]:
    """Why KiPI's packages have to be added to KiCad's environment, or None (recording an
    environment that has them all already)."""
    from kipi.launcher import kicad_env, plugin_setup
    previous = load(env)
    wanted = deps.requirements_hash(req)
    if previous is not None and previous.requirements_hash == wanted:
        return None
    unmet = kicad_env.unmet_in(env, req)
    if unmet == []:
        py = system.probe([str(paths.env_python(env))])
        kicad_env.adopt(env, wanted, py.version_text if py else "")
        log.write(f"KiCad's plugin environment {env} has every package; recorded it")
        return None
    return plugin_setup.FIRST if previous is None else plugin_setup.CHANGED


def main(argv: Optional[List[str]] = None, windowed: bool = False) -> int:
    args = parse(argv)
    if os.environ.get("KICAD_API_SOCKET"):
        # KiCad started this as its plugin: KiCad made the plugin's environment itself.
        sys.path.insert(0, str(paths.KIPI_ROOT))
        import kipi_entry
        return kipi_entry.main()

    data = paths.data_dir()
    req = paths.requirements_file()
    log = Log(paths.log_dir(data))
    kicad = kicad_env_for(args.own_env, log)
    env = kicad or paths.env_dir(data)
    ui = make_ui(args.console, windowed)
    if args.create_shortcut:
        from kipi.launcher.shortcut import ShortcutError, create
        try:
            ui.info(f"KiPI is in your applications now ({create()}).")
            return 0
        except ShortcutError as e:
            ui.error(f"no shortcut was made: {e}")
            return 2
    if args.after_pid:
        ui.status("Waiting for KiPI to close...")
        if not all(system.wait_for_exit(pid) for pid in args.after_pid):
            ui.error("KiPI is still open. Close it, then change the setup again.")
            return 1
    previous = load(env)
    pardiso_ok, pardiso_why = system.pardiso_supported()
    # Ticked by default wherever it can be had (#129); after that, whatever was chosen.
    want_pardiso = previous.pardiso if previous else pardiso_ok
    reason = kicad_reason(env, req, log) if kicad else needs_setup(env, deps.requirements_hash(req))
    fresh, ask = False, True
    if kicad and (args.reset or args.repair):
        ui.error(f"KiPI runs in the environment KiCad made for its plugin ({env}). To start "
                 "it again from nothing, use Recreate Plugin Environment on KiPI in KiCad's "
                 "Preferences > Plugins (KiPI sets it up on its next start), or run the "
                 "launcher with --own-env to use an environment of KiPI's own.")
        return 2
    try:
        if args.reset:
            remove_env(env, data)
            ui.info(f"Deleted KiPI's environment ({env}). It is set up again the next time "
                    "KiPI starts.")
            return 0
        if args.repair:
            reason, fresh = "you asked to reinstall it", True
        elif args.setup:
            reason = reason or "you asked to change it"
        elif args.add_pardiso or args.remove_pardiso:
            if args.add_pardiso and not pardiso_ok:
                ui.error(f"PARDISO is not available here: {pardiso_why}.")
                return 2
            want_pardiso, ask = args.add_pardiso, False
            reason = reason or ("adding PARDISO" if want_pardiso else "removing PARDISO")
    except InstallError as e:
        ui.error(str(e))
        return 2

    if reason:
        log.write(f"Setup: {reason}")
        if kicad:       # its own Python, which has pip: KiCad made it with venv
            python, problems = system.probe([str(paths.env_python(env))]), []
        else:
            python, problems = choose_python(env, fresh)
        if python is None:
            found = "".join(f"\n  {p}" for p in problems)
            ui.error(f"KiPI needs {system.supported_text()} to set up its environment, and "
                     f"none was found.{' Found:' + found if found else ''}\n\nInstall one "
                     "(for example from python.org) and start KiPI again.")
            return 2
        ui.status("Checking which packages KiPI needs...")
        plan = deps.make_plan([python.executable], req, pardiso_ok)
        log.write(f"Plan ({plan.source or 'names only'}"
                  + (f"; {plan.note}" if plan.note else "") + "): "
                  + ", ".join(f"{p.name} {p.version}" for p in plan.packages))
        summary = Summary(reason, python, env, plan, pardiso_ok, pardiso_why, want_pardiso,
                          intro=KICAD_INTRO.format(reason=reason) if kicad else "")
        choice = ui.confirm(summary) if ask else want_pardiso
        if choice is None:
            ui.info("Setup canceled; nothing was changed.")
            if not args.after_pid:
                return 1
            ui.close()          # KiPI was closed only to change the setup: open it again
            return launch(env, args.board, windowed)

        def task(on_line, on_fraction, register):
            def line(text):
                log.write(text)
                on_line(text)
            if kicad:
                return perform(summary, choice, data, req, line, on_fraction, register,
                               owned=False)
            return perform(summary, choice, data, req, line, on_fraction, register, fresh)

        try:
            ui.install(task)
        except Canceled:
            log.write("Canceled")
            ui.error("setup was canceled. It picks up again the next time KiPI starts.")
            return 1
        except (InstallError, OSError) as e:
            log.write(f"Failed: {e}")
            ui.error(f"{e}\n\nEverything the installer printed is in {log.path}")
            return 1
    ui.close()
    # A KiPI icon to double-click in the KiPI folder (Windows; made once, #127).
    from kipi.launcher.shortcut import keep_folder_icon
    keep_folder_icon(stamp=data / "folder-shortcut.txt")
    if args.no_launch:
        return 0
    return launch(env, args.board, windowed)


def launch(env: Path, board: Optional[str], windowed: bool) -> int:
    """Start KiPI, standalone, with the environment's Python and this KiPI folder."""
    python = paths.env_python(env, windowed)
    if not python.is_file():
        python = paths.env_python(env)
    cmd = [str(python), "-m", "kipi"] + ([str(Path(board).resolve())] if board else [])
    environ = dict(os.environ)
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(paths.KIPI_ROOT)] + ([environ["PYTHONPATH"]] if environ.get("PYTHONPATH") else []))
    environ["KIPI_HOST"] = "standalone"
    environ[LAUNCHER_ENV] = str(paths.KIPI_ROOT / "kipi_launcher.py")
    environ[LAUNCHER_PYTHON_ENV] = sys.executable
    if windowed:
        # Nothing to wait for: the launcher's job is done once KiPI is running.
        flags = 0x00000008 if sys.platform == "win32" else 0      # DETACHED_PROCESS
        subprocess.Popen(cmd, env=environ, close_fds=True, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return 0
    return subprocess.call(cmd, env=environ)
