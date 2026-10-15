"""The welcome screen shown while KiPI starts: the animated logo, the version and what it
is running on.

It closes itself after a few seconds, so it never waits for the user (a click closes it
sooner). It is cosmetic: if it cannot be shown, KiPI starts without it.
"""

from __future__ import annotations

import html
import logging
from typing import Optional

from qtpy.QtCore import QSize, Qt, QTimer
from qtpy.QtGui import QCursor, QGuiApplication, QMovie, QPalette, QPixmap
from qtpy.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from kipi import __version__
from kipi.gui import about
from kipi.gui.identity import APP_ID

log = logging.getLogger(__name__)

SHOW_MS = 3500          # from when it appears; building the main window counts toward it
LOGO_SIZE = 160
# The packages worth naming at a glance; the About dialog lists the rest.
PACKAGES = ("kicad-python", "PySide6-Essentials", "pyvista", "numpy", "scipy")


class Splash(QWidget):
    def __init__(self, show_ms: int = SHOW_MS):
        super().__init__(None, Qt.SplashScreen | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(f"{about.NAME} {__version__}")

        frame = QFrame(self)
        frame.setObjectName("splash")
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet("#splash { border: 1px solid palette(mid); "
                            "background: palette(window); }")

        logo = QLabel(frame)
        logo.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        logo.setAlignment(Qt.AlignCenter)
        self.movie = self._animate(logo)
        path = about.icon_path()
        if self.movie is None and path is not None:
            logo.setPixmap(QPixmap(str(path)).scaled(
                LOGO_SIZE, LOGO_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        rows = [("Version", __version__), ("Plugin", APP_ID), ("License", about.LICENSE)]
        rows += [(k, v) for k, v in about.versions(solver=False)
                 if k == "Python" or k in PACKAGES]
        dim = self.palette().color(QPalette.PlaceholderText).name()
        table = "".join(f"<tr><td style='color:{dim}'>{html.escape(k)}</td>"
                        f"<td>&nbsp;&nbsp;{html.escape(v)}</td></tr>" for k, v in rows)
        text = QLabel(frame)
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setText(
            f"<h1 style='margin:0'>Welcome to {about.NAME}</h1>"
            f"<p style='margin-top:4px'>{html.escape(about.DESCRIPTION)}</p>"
            f"<table>{table}</table>"
            f"<p style='margin-bottom:0'>{about.AUTHOR} · {about.REPO}</p>")
        self.status = QLabel("Starting…", frame)
        self.status.setStyleSheet(f"color: {dim}")

        right = QVBoxLayout()
        right.addWidget(text, 1)
        right.addWidget(self.status)
        inner = QHBoxLayout(frame)
        inner.setContentsMargins(20, 20, 20, 16)
        inner.addWidget(logo, 0, Qt.AlignTop)
        inner.addSpacing(16)
        inner.addLayout(right, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        self.setFixedWidth(620)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(show_ms)
        self.timer.timeout.connect(self.close)

    def _animate(self, label: QLabel) -> Optional[QMovie]:
        """Play the animated logo, or None to fall back to the still one (the GIF can be
        missing, or Qt built without its GIF plugin)."""
        path = about.animated_icon_path()
        if path is None:
            return None
        movie = QMovie(str(path), parent=self)
        if not movie.isValid():
            return None
        movie.setScaledSize(QSize(LOGO_SIZE, LOGO_SIZE))
        label.setMovie(movie)
        movie.start()
        # The GIF holds its first frame for a while (it doubles as a still logo); the
        # splash is short, so go straight to the planes flashing through the result maps.
        if movie.frameCount() > 1:
            movie.jumpToFrame(1)
        return movie

    def show_centered(self) -> None:
        self.adjustSize()
        # The screen KiPI was started on: the one with the mouse, as it just clicked KiCad.
        screen = (QGuiApplication.screenAt(QCursor.pos())
                  or QGuiApplication.primaryScreen())
        if screen is not None:
            r = screen.availableGeometry()
            self.move(r.center() - self.rect().center())
        self.show()
        self.raise_()
        self.timer.start()

    def mousePressEvent(self, event):
        self.close()

    def closeEvent(self, event):
        self.timer.stop()
        if self.movie is not None:
            self.movie.stop()
        super().closeEvent(event)


def show_splash(app=None, show_ms: int = SHOW_MS) -> Optional[Splash]:
    """Show the welcome screen; None if it could not be shown."""
    try:
        splash = Splash(show_ms)
        splash.show_centered()
        if app is not None:
            app.processEvents()     # paint it before the main window is built
        return splash
    except Exception:  # noqa: BLE001 - cosmetic; never stop KiPI starting
        log.warning("Could not show the welcome screen", exc_info=True)
        return None
