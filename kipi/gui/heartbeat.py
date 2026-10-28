"""Signs of life while KiPI works (#111): a spinner and the time taken in the status bar,
with an estimate of the time left where there is one (#224), and the busy cursor while
the window itself is busy.

The spinner turns on a timer of the UI thread, so it keeps turning while a task runs in
the background and stops only if the window itself stalls.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Dict, Hashable, Optional, Tuple

from qtpy.QtCore import QRectF, QSize, Qt, QTimer
from qtpy.QtGui import QColor, QPainter, QPen
from qtpy.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

from kipi.core.eta import Estimate, left_text

TICK_MS = 80              # about 12 frames a second: smooth, and cheap to repaint
STEP_DEG = 30             # the arc turns this far each tick
SHOW_AFTER_S = 0.4        # quick operations finish before the indicator shows


def elapsed_text(seconds: float) -> str:
    """0:07, 1:42 or 1:02:03."""
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _Spinner(QWidget):
    """A turning arc, drawn in the window's highlight colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle = 0
        self.setFixedSize(QSize(14, 14))

    def step(self):
        self.angle = (self.angle + STEP_DEG) % 360
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = self.palette().highlight().color()
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        track = QColor(color)
        track.setAlpha(60)
        p.setPen(QPen(track, 2))
        p.drawEllipse(rect)
        p.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, -self.angle * 16, 100 * 16)     # Qt angles are 1/16 degree
        p.end()


class Heartbeat(QWidget):
    """Status bar indicator: a spinner and "name · m:ss" while any activity runs, and
    "· about 3 min left" when the activity has an :class:`Estimate` that knows.

    Each activity (the main task, the part models loading beside it) has a key of its
    own and a name; the newest is shown, with the time since it started.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(6)
        self.spinner = _Spinner(self)
        self.label = QLabel(self)
        lay.addWidget(self.spinner)
        lay.addWidget(self.label)
        self.setToolTip("KiPI is working. The spinner turns while it does; the time is how "
                        "long the operation has taken so far.\n\nThe time left is an "
                        "estimate for the step running now (shown in the Log), from how "
                        "fast it is going or, for a step that cannot tell, from how long "
                        "steps of about its size took before on this computer.")
        # key -> (name, start, estimate)
        self._started: Dict[Hashable, Tuple[str, float, Optional[Estimate]]] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self, key: Hashable, name: str, estimate: Optional[Estimate] = None):
        self._started.pop(key, None)
        self._started[key] = (name, time.monotonic(), estimate)
        if not self._timer.isActive():
            self._timer.start()

    def stop(self, key: Hashable):
        self._started.pop(key, None)
        if not self._started:
            self._timer.stop()
            self.hide()
        else:
            self._tick()

    @property
    def active(self) -> bool:
        return bool(self._started)

    def text(self) -> str:
        if not self._started:
            return ""
        name, t0, estimate = next(reversed(self._started.values()))
        text = f"{name} · {elapsed_text(time.monotonic() - t0)}"
        left = estimate.remaining() if estimate is not None else None
        return text if left is None else f"{text} · {left_text(left)}"

    def _tick(self):
        if not self._started:
            return
        t0 = min(t for _, t, _ in self._started.values())
        if time.monotonic() - t0 < SHOW_AFTER_S:
            return
        self.label.setText(self.text())
        self.spinner.step()
        if not self.isVisible():
            self.show()


@contextmanager
def busy_cursor():
    """The wait cursor while the UI thread works (redrawing a solve, saving a file): the
    spinner cannot turn then, but the cursor still tells the user KiPI is busy."""
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()
