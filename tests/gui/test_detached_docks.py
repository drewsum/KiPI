"""Detached panes act like windows (#186): a floating dock gets minimize and maximize
buttons once the drag that floated it is over, F11 toggles it full screen, and it docks
again as it was."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication, QDockWidget, QLabel, QMainWindow  # noqa: E402

from kipi.gui import docks  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = QMainWindow()
    w.setCentralWidget(QLabel("view"))
    dock = QDockWidget("Log", w)
    dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
    dock.setWidget(QLabel("log"))
    docks.make_detachable(dock)
    w.addDockWidget(Qt.BottomDockWidgetArea, dock)
    w.show()
    yield w, dock
    w.close()


def _buttons(dock):
    return (docks.is_window(dock) and (dock.windowFlags() & Qt.WindowMinMaxButtonsHint)
            == Qt.WindowMinMaxButtonsHint)


def test_a_floated_dock_gets_window_buttons_and_docks_again(window):
    w, dock = window
    assert not _buttons(dock)
    dock.setFloating(True)
    QApplication.processEvents()
    assert dock.isFloating() and dock.isWindow() and _buttons(dock)
    assert not dock.isHidden()
    dock.setFloating(False)
    QApplication.processEvents()
    assert not dock.isFloating() and not dock.isWindow()
    assert w.dockWidgetArea(dock) == Qt.BottomDockWidgetArea


def test_the_buttons_wait_for_the_drag_to_end(window, monkeypatch):
    _w, dock = window
    held = [Qt.LeftButton]
    monkeypatch.setattr(docks.QApplication, "mouseButtons", staticmethod(lambda: held[0]))
    later = []
    monkeypatch.setattr(docks.QTimer, "singleShot", lambda ms, fn: later.append(fn))
    dock.setFloating(True)
    assert dock.isFloating() and not _buttons(dock) and later     # still dragging
    held[0] = Qt.NoButton
    later.pop()()
    assert _buttons(dock)


def test_f11_toggles_full_screen(window):
    _w, dock = window
    action, = [a for a in dock.actions() if a.shortcut().toString() == docks.FULL_SCREEN_KEY]
    action.trigger()                                  # docked: floats it, then full screen
    QApplication.processEvents()
    assert dock.isFloating() and _buttons(dock)
    assert dock.windowState() & Qt.WindowFullScreen
    action.trigger()
    QApplication.processEvents()
    assert not dock.windowState() & Qt.WindowFullScreen
    dock.setFloating(False)
    assert not dock.isWindow()


def test_the_main_window_docks_are_detachable(app):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    try:
        for dock in w.findChildren(QDockWidget):
            assert any(a.shortcut().toString() == docks.FULL_SCREEN_KEY
                       for a in dock.actions()), dock.windowTitle()
    finally:
        w.close()
