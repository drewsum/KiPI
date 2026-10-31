"""The plugin's setup screen where no window toolkit is there (#7): the system's own dialogs.

KiCad's plugin Python normally has wx (Windows, Linux) or Tk (python.org on macOS); when
it has neither (Homebrew's Python on macOS without python-tk, a bare Linux Python), KiPI
asks with what the desktop provides: ``osascript`` on macOS, ``zenity`` or ``kdialog`` on
Linux. The question is shorter than the window's (what is installed, how big, PARDISO
yes or no), and the install shows a progress box (zenity) or a notice that it started.

With none of those either, :class:`SilentUI` installs without asking: the user clicked
KiPI's button to use it, and before #7 KiCad installed everything unasked anyway.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from typing import Callable, List, Optional, Sequence

from kipi.launcher.install import Canceled
from kipi.launcher.setup_env import Summary
from kipi.launcher.state import State
from kipi.launcher.system import child_kwargs

Run = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


def _run(cmd: Sequence[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(list(cmd), capture_output=True, text=True, **child_kwargs())


def _osa_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def question_text(s: Summary) -> str:
    return (f"{s.intro_text()}\n\n{s.size_line(False)}.\n\nInstall now? It takes a few "
            "minutes; KiPI starts when it is done.")


class NativeUI:
    """Yes/no questions and messages with the desktop's dialog program ``tool``
    ("osascript", "zenity" or "kdialog")."""

    def __init__(self, tool: str, run: Run = _run, popen=subprocess.Popen):
        self.tool = tool
        self.run = run
        self.popen = popen

    @classmethod
    def find(cls, platform: Optional[str] = None, which=shutil.which) -> Optional["NativeUI"]:
        platform = platform or sys.platform
        names = ["osascript"] if platform == "darwin" else \
            ["zenity", "kdialog"] if platform.startswith("linux") else []
        for name in names:
            path = which(name)
            if path:
                return cls(name)
        return None

    # --- dialogs ------------------------------------------------------------------------
    def ask(self, text: str, yes: str = "Install", no: str = "Cancel") -> bool:
        if self.tool == "osascript":
            script = (f"display dialog {_osa_quote(text)} with title \"KiPI setup\" buttons "
                      f"{{{_osa_quote(no)}, {_osa_quote(yes)}}} default button {_osa_quote(yes)}")
            out = self.run(["osascript", "-e", script])
            return out.returncode == 0 and f"button returned:{yes}" in out.stdout
        if self.tool == "zenity":
            out = self.run(["zenity", "--question", "--title=KiPI setup", "--width=520",
                            f"--text={text}", f"--ok-label={yes}", f"--cancel-label={no}"])
            return out.returncode == 0
        out = self.run(["kdialog", "--title", "KiPI setup", "--yesno", text,
                        "--yes-label", yes, "--no-label", no])
        return out.returncode == 0

    def message(self, text: str, error: bool = False) -> None:
        if self.tool == "osascript":
            kind = "critical" if error else "informational"
            self.run(["osascript", "-e", f"display alert \"KiPI\" message {_osa_quote(text)} "
                      f"as {kind}"])
        elif self.tool == "zenity":
            self.run(["zenity", "--error" if error else "--info", "--title=KiPI",
                      "--width=520", f"--text={text}"])
        else:
            self.run(["kdialog", "--title", "KiPI", "--error" if error else "--msgbox", text])

    # --- the interface the launcher uses ---------------------------------------------------
    def confirm(self, s: Summary) -> Optional[bool]:
        if not self.ask(question_text(s)):
            return None
        if not s.pardiso_offered:
            return False
        return self.ask(f"Also install PARDISO, a faster solver for large boards? It "
                        f"{s.pardiso_line()}.", yes="Install PARDISO", no="Not now")

    def install(self, task) -> State:
        if self.tool == "zenity":
            return self._install_with_progress(task)
        if self.tool == "osascript":
            self.run(["osascript", "-e", "display notification \"Installing KiPI's packages. "
                      "KiPI starts when it is done.\" with title \"KiPI setup\""])
        else:
            self.run(["kdialog", "--title", "KiPI setup", "--passivepopup",
                      "Installing KiPI's packages. KiPI starts when it is done.", "10"])
        return task(lambda line: None, lambda f: None, lambda inst: None)

    def _install_with_progress(self, task) -> State:
        """zenity's progress box reads "# text" lines and percentages on its stdin, and
        exits when Cancel is clicked."""
        box = self.popen(["zenity", "--progress", "--pulsate", "--auto-close",
                          "--title=KiPI setup", "--width=520",
                          "--text=Installing KiPI's packages…"],
                         stdin=subprocess.PIPE, text=True)       # Linux only: no flags
        installers: List = []
        canceled = threading.Event()

        def watch():
            box.wait()
            if box.returncode not in (0, None):
                canceled.set()
                for inst in list(installers):
                    inst.cancel()

        def say(text: str) -> None:
            try:
                box.stdin.write("# " + text[:100].replace("\n", " ") + "\n")
                box.stdin.flush()
            except (OSError, ValueError):
                pass

        def register(inst):
            installers.append(inst)
            if canceled.is_set():
                inst.cancel()

        threading.Thread(target=watch, daemon=True).start()
        try:
            return task(say, lambda f: None, register)
        except Canceled:
            raise
        except BaseException:
            if canceled.is_set():
                raise Canceled() from None
            raise
        finally:
            try:
                box.stdin.write("100\n")
                box.stdin.close()
            except (OSError, ValueError):
                pass

    def status(self, text: str) -> None:
        pass

    def info(self, text: str) -> None:
        self.message(text)

    def error(self, text: str) -> None:
        self.message("KiPI could not start: " + text, error=True)

    def close(self) -> None:
        pass


class SilentUI:
    """No way to show anything: install what KiPI needs (without PARDISO, which About →
    Manage dependencies adds later) and say what happened in the log only."""

    def __init__(self, log: Callable[[str], None] = lambda text: None):
        self.log = log

    def confirm(self, s: Summary) -> Optional[bool]:
        self.log("No window or dialog can open here; installing without asking")
        return False

    def install(self, task) -> State:
        return task(lambda line: None, lambda f: None, lambda inst: None)

    def status(self, text: str) -> None:
        self.log(text)

    def info(self, text: str) -> None:
        self.log(text)

    def error(self, text: str) -> None:
        self.log("KiPI could not start: " + text)

    def close(self) -> None:
        pass
