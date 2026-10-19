# Portions Copyright (c) 2026 Tomachie LLC.
"""The main window must fit a common screen when maximized. Its minimum size is the sum of
its docks' minimums; once that is taller or wider than the screen, Windows places the
window partly off it and the bottom (the log) is cut off."""

import os

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

# A 1080p screen less the taskbar and title bar; 1366 is the narrowest common laptop.
MAX_HEIGHT = 1000
MAX_WIDTH = 1366


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    w.show()
    app.processEvents()
    yield w
    w.close()


def test_the_main_window_fits_a_1080p_screen(window):
    size = window.minimumSizeHint()
    assert size.height() <= MAX_HEIGHT, f"minimum height {size.height()} px"
    assert size.width() <= MAX_WIDTH, f"minimum width {size.width()} px"


def test_the_window_opens_inside_the_screens_work_area(window):
    """Frame and title bar included, the window opens within the area above the taskbar,
    on any screen: a 2560 x 1600 laptop at 175 % has only 1463 x 874 logical pixels."""
    area, frame = window.screen().availableGeometry(), window.frameGeometry()
    assert frame.top() >= area.top() and frame.left() >= area.left()
    assert frame.bottom() <= area.bottom()                     # clear of the taskbar
    if window.minimumSizeHint().width() <= area.width():      # room for it across
        assert frame.right() <= area.right()


def test_the_side_docks_scroll_rather_than_set_the_window_height(window):
    # The setup and board docks scroll, so the window's minimum stays well under a
    # small laptop screen's 874 px.
    assert window.minimumSizeHint().height() <= 600
