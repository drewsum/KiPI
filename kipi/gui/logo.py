"""The animated KiPI logo as the splash and About show it, sharp at any size and display
scaling (#231).

The frames come from resources/kipi_frames.png, a strip of full-colour frames with smooth
edges that tools/make_icons.py makes at twice the largest size shown. The README's
kipi.gif looked poor in the app: a GIF has 256 colours and on/off transparency, which
leaves the outline jagged, Qt scaled it a second time to the size shown, and QMovie scales
in device pixels, so a scaled display stretched it once more. The GIF is still played if
the strip cannot be read, and the still icon shown if neither can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from qtpy.QtCore import QObject, QSize, Qt, QTimer, Signal
from qtpy.QtGui import QImage, QMovie, QPixmap
from qtpy.QtWidgets import QLabel

from kipi.gui import about
from kipi.gui.identity import RESOURCES

FRAME_MS = 50           # a frame's time if the strip does not say


def frames_path() -> Optional[Path]:
    p = RESOURCES / "kipi_frames.png"
    return p if p.is_file() else None


def _dpr(label: QLabel) -> float:
    return max(1.0, label.devicePixelRatioF())


class LogoPlayer(QObject):
    """Plays the frame strip on a label, in as much of QMovie's interface as the splash
    and About use. Each frame is scaled once for the label's display, and again if the
    label moves to a screen with other scaling."""

    frameChanged = Signal(int)
    finished = Signal()             # for QMovie's sake: the logo loops, so never sent

    def __init__(self, path: Path, label: QLabel, size: int, speed: int = 100,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.path, self.label, self.size, self.speed = path, label, size, speed
        self.durations: List[int] = []
        self.pixmaps: Dict[int, QPixmap] = {}
        self.dpr = 0.0
        self.current = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._next)
        self._load(_dpr(label))

    def isValid(self) -> bool:
        return bool(self.pixmaps)

    def _load(self, dpr: float) -> None:
        """Every frame scaled for ``dpr``. The strip is read again rather than kept, as
        unpacked it is tens of megabytes."""
        strip = QImage(str(self.path))
        side = strip.width()
        if strip.isNull() or side <= 0 or strip.height() < side:
            self.pixmaps = {}
            return
        n = strip.height() // side
        try:
            durations = [int(t) for t in strip.text("durations").split(",")]
        except ValueError:
            durations = []
        self.durations = durations if len(durations) == n else [FRAME_MS] * n
        px = round(self.size * dpr)
        self.pixmaps = {}
        for i in range(n):
            frame = strip.copy(0, i * side, side, side).scaled(
                px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm = QPixmap.fromImage(frame)
            pm.setDevicePixelRatio(dpr)
            self.pixmaps[i] = pm
        self.dpr = dpr

    def frameCount(self) -> int:
        return len(self.pixmaps)

    def currentFrameNumber(self) -> int:
        return self.current

    def start(self) -> None:
        self.jumpToFrame(0)

    def stop(self) -> None:
        self.timer.stop()

    def jumpToFrame(self, n: int) -> bool:
        if not 0 <= n < self.frameCount():
            return False
        dpr = _dpr(self.label)
        if dpr != self.dpr:
            self._load(dpr)
            if not self.pixmaps:
                return False
        self.current = n
        self.label.setPixmap(self.pixmaps[n])
        self.timer.start(max(1, self.durations[n] * 100 // max(1, self.speed)))
        self.frameChanged.emit(n)
        return True

    def _next(self) -> None:
        self.jumpToFrame((self.current + 1) % self.frameCount())


def animate(label: QLabel, size: int, parent: QObject, speed: int = 200):
    """Play the animated logo on ``label`` at ``size`` (logical) pixels: the frame strip,
    else the GIF (Qt can be built without its GIF plugin), else None for the still logo."""
    path = frames_path()
    if path is not None:
        player = LogoPlayer(path, label, size, speed, parent)
        if player.isValid():
            player.start()
            return player
        player.deleteLater()
    path = about.animated_icon_path()
    if path is None:
        return None
    movie = QMovie(str(path), parent=parent)
    if not movie.isValid():
        return None
    movie.setScaledSize(QSize(size, size))
    label.setMovie(movie)
    movie.setSpeed(speed)
    movie.start()
    return movie


def still(label: QLabel, size: int) -> None:
    """Show the still logo on ``label``, scaled for its display."""
    path = about.icon_path()
    if path is None:
        return
    dpr = _dpr(label)
    px = round(size * dpr)
    pm = QPixmap(str(path)).scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    label.setPixmap(pm)
