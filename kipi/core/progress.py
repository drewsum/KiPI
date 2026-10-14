"""Progress reporting shared by every pipeline stage.

Pipeline code (extraction, meshing, solving) only depends on this module, never on
Qt. The GUI supplies a reporter that forwards to Qt signals. Tests and the CLI use
the logging reporter.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

log = logging.getLogger("kipi")


class Canceled(Exception):
    """Raised inside a stage when the user cancels the run."""


class ProgressReporter:
    """Base reporter: writes to the ``kipi`` logger."""

    def __init__(self):
        self._cancel = False

    # --- overridable sinks -------------------------------------------------
    def message(self, text: str, level: int = logging.INFO):
        log.log(level, text)

    def stage(self, name: str):
        self.message(f"== {name}")

    def fraction(self, value: Optional[float]):
        """Progress through the current stage, 0..1, or None if unknown."""

    def partial(self, result):
        """Hand over one piece of a result before the whole stage finishes."""

    # --- helpers -----------------------------------------------------------
    def debug(self, text: str):
        """Detail for the log file; the GUI does not display it."""
        self.message(text, logging.DEBUG)

    def info(self, text: str):
        self.message(text, logging.INFO)

    def warn(self, text: str):
        self.message(text, logging.WARNING)

    def error(self, text: str):
        self.message(text, logging.ERROR)

    def cancel(self):
        self._cancel = True

    def check_cancel(self):
        if self._cancel:
            raise Canceled()

    @contextmanager
    def timed(self, name: str):
        self.stage(name)
        self.fraction(None)
        t0 = time.perf_counter()
        yield
        self.fraction(1.0)
        self.info(f"{name} done in {time.perf_counter() - t0:.2f} s")


NULL = ProgressReporter()
