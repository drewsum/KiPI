"""The setup screen in a terminal, for when no window can open (no tkinter, no display) or
``--console`` asks for it."""

from __future__ import annotations

import textwrap
from typing import Callable, Optional

from kipi.launcher import deps
from kipi.launcher.install import Canceled
from kipi.launcher.setup_env import PARDISO_CONS, PARDISO_PROS, PARDISO_TITLE, Summary
from kipi.launcher.state import State


class ConsoleUI:
    def __init__(self, write: Callable[[str], None] = print,
                 ask: Callable[[str], str] = input):
        self.write = write
        self.ask = ask

    def _yes(self, question: str, default: bool) -> Optional[bool]:
        hint = "[Y/n]" if default else "[y/N]"
        try:
            answer = self.ask(f"{question} {hint} ").strip().lower()
        except EOFError:                # no one to ask (stdin closed)
            return None
        return default if not answer else answer.startswith("y")

    def confirm(self, s: Summary) -> Optional[bool]:
        """Show what will be installed. True/False: go ahead, with or without PARDISO;
        None: the user said no."""
        w = self.write
        w("")
        w(s.intro_text())
        w(f"It goes in {s.env}")
        w(f"and uses Python {s.python.version_text} ({s.python.executable}).")
        w("")
        source = s.source_line()
        w("Packages to install" + (f" ({source})" if source else "") + ":")
        for p in sorted(s.plan.packages, key=lambda p: -(p.size or 0)):
            size = deps.fmt_size(p.size) if p.size else ""
            w(f"  {p.name:<28} {p.version:<14} {size:>10}")
        w("Total: " + s.size_line(False))
        w("")
        pardiso = False
        if s.pardiso_offered:
            w(PARDISO_TITLE + ": " + s.pardiso_line())
            for label, items in (("+", PARDISO_PROS), ("-", PARDISO_CONS)):
                for item in items:
                    w(textwrap.fill(item, 88, initial_indent=f"  {label} ",
                                    subsequent_indent="    "))
            answer = self._yes("Install PARDISO too?", s.pardiso_default)
            if answer is None:
                return None
            pardiso = answer
            if pardiso:
                w("With PARDISO: " + s.size_line(True))
        else:
            w(f"PARDISO, the optional faster solver, is not available here: "
              f"{s.pardiso_why_not}.")
        w("")
        go = self._yes("Set up KiPI now?", True)
        return pardiso if go else None

    def install(self, task) -> State:
        """Run ``task(on_line, on_fraction, register)``; Ctrl+C cancels it."""
        installers = []

        def on_line(text: str):
            self.write("  " + text)

        try:
            return task(on_line, lambda f: None, installers.append)
        except KeyboardInterrupt:
            for inst in installers:
                inst.cancel()
            raise Canceled() from None

    def status(self, text: str) -> None:
        """What the launcher is doing now (a step that takes a moment)."""
        self.write(text)

    def info(self, text: str) -> None:
        """A message the user should read before going on."""
        self.write(text)

    def close(self) -> None:
        pass

    def error(self, text: str) -> None:
        self.write("")
        self.write("KiPI could not start: " + text)
