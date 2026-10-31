"""The welcome screen shown while KiPI starts: the animated logo, its author, the version
and what it is running on.

It is the first thing KiPI shows, before the slow part of starting (#55), and says what
KiPI is doing meanwhile. Once the main window is open it closes itself when the logo has
played through one loop (or after a few seconds if only the still logo can be shown), so
it never waits for the user; a click closes it sooner. It is cosmetic: if it cannot be
shown, KiPI starts without it.
"""

from __future__ import annotations

import html
import logging
from typing import Optional

from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtGui import QCursor, QGuiApplication, QPalette
from qtpy.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from kipi import __version__
from kipi.gui import about, logo as logo_art
from kipi.gui.identity import APP_ID
from kipi.host import Host, current as current_host

log = logging.getLogger(__name__)

SHOW_MS = 3500          # for the still logo only; the animated one plays one loop
LOGO_SIZE = 160
# The packages worth naming at a glance; the About dialog lists the rest.
PACKAGES = ("kicad-python", "PySide6-Essentials", "pyvista", "numpy", "scipy")


class Splash(QWidget):
    closed = Signal()

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
        self.movie = logo_art.animate(logo, LOGO_SIZE, self)
        if self.movie is None:
            logo_art.still(logo, LOGO_SIZE)

        host = current_host()
        rows = [("Author", f"{about.AUTHOR} ({about.EMAIL})"), ("Version", __version__),
                ("Mode", host.label)]
        if host is Host.PLUGIN:
            rows.append(("Plugin", APP_ID))
        rows.append(("License", about.LICENSE))
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
            f"<p style='margin-top:4px; margin-bottom:12px;'>{html.escape(about.DESCRIPTION)}<br></p>"
            f"<table>{table}</table>"
            f"<p style='margin-bottom:0'>{about.REPO}</p>")
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
        self.timer.timeout.connect(self._time_up)
        self._looped_from = None
        self._held = False
        self._due = False       # its time was up while held
        if self.movie is not None:
            self.movie.frameChanged.connect(self._on_frame)
            self.movie.finished.connect(self._time_up)    # a GIF that does not repeat

    def _on_frame(self, n: int) -> None:
        """Close when the logo wraps back to its first frame: one full loop played."""
        if n > 0:
            self._looped_from = n
        elif self._looped_from is not None:
            self._time_up()

    def _time_up(self) -> None:
        if self._held:
            self._due = True
        else:
            self.close()

    def hold(self) -> None:
        """Stay open, however long, until :meth:`release` (while KiPI is still starting)."""
        self._held = True

    def release(self) -> None:
        """Close now if the splash's time is up, or else when it is."""
        self._held = False
        if self._due:
            self.close()

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.repaint()

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
        if self.movie is None or self.movie.frameCount() <= 1:
            self.timer.start()      # nothing to loop through

    def mousePressEvent(self, event):
        self.close()

    def closeEvent(self, event):
        self.timer.stop()
        if self.movie is not None:
            self.movie.stop()
        super().closeEvent(event)
        self.closed.emit()


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
