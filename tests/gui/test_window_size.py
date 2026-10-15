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
