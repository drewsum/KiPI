"""Detached panes that act like windows (#186): a dock pulled out of the main window gets
its own window's minimize and maximize buttons, and F11 toggles it full screen.

A floating dock is a tool window, which has only a close button (and here not even that).
Once the drag that floated it is over, it becomes a normal window with minimize and
maximize. Window managers that ignore those buttons still have F11, which works anywhere.
Docking it again, by double-clicking or dragging its title bar onto the main window, puts
it back as Qt left it.
"""

from __future__ import annotations

from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QAction, QKeySequence
from qtpy.QtWidgets import QApplication, QDockWidget

FULL_SCREEN_KEY = "F11"
WINDOW_FLAGS = (Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint
                | Qt.WindowSystemMenuHint | Qt.WindowMinMaxButtonsHint)
_POLL_MS = 100            # how often to look whether the drag that floated a dock is over


def make_detachable(dock: QDockWidget) -> None:
    """Give ``dock`` window buttons whenever it floats, and F11 for full screen."""
    dock.topLevelChanged.connect(lambda floating: _floated(dock) if floating
                                 else dock.setWindowState(Qt.WindowNoState))
    key = QAction("Full screen", dock)
    key.setShortcut(QKeySequence(FULL_SCREEN_KEY))
    key.setShortcutContext(Qt.WidgetWithChildrenShortcut)
    key.triggered.connect(lambda: toggle_full_screen(dock))
    dock.addAction(key)


def _floated(dock: QDockWidget) -> None:
    # Changing the window flags mid-drag would recreate the window under the mouse and end
    # the drag (and the chance to dock it elsewhere), so wait for the button to come up.
    if QApplication.mouseButtons() != Qt.NoButton:
        QTimer.singleShot(_POLL_MS, lambda: _floated(dock))
        return
    if not dock.isFloating() or is_window(dock):
        return
    geometry = dock.geometry()
    dock.setWindowFlags(WINDOW_FLAGS)
    dock.setGeometry(geometry)
    dock.show()


def is_window(dock: QDockWidget) -> bool:
    """Whether ``dock`` floats as a normal window (a tool window is Window plus other
    bits, so the type is compared whole)."""
    return (dock.windowFlags() & Qt.WindowType_Mask) == Qt.Window


def toggle_full_screen(dock: QDockWidget) -> None:
    """Full screen and back for a detached pane; a docked pane is detached first."""
    if not dock.isFloating():
        dock.setFloating(True)
        _floated(dock)
    if dock.isFullScreen():
        dock.showNormal()
    else:
        dock.showFullScreen()
