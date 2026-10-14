"""The About dialog opens from the toolbar and names the version, repo and icon."""

import os

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QLabel  # noqa: E402

from kipi import __version__  # noqa: E402
from kipi.gui import about  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_icon_ships_with_the_plugin():
    assert about.icon_path() is not None


def test_dialog_names_the_version_and_repo(app):
    d = about.AboutDialog()
    text = " ".join(lb.text() for lb in d.findChildren(QLabel))
    assert __version__ in text and about.REPO in text
    assert any(not lb.pixmap().isNull() for lb in d.findChildren(QLabel) if lb.pixmap())


def test_toolbar_has_about_after_open_results(app, no_3d):
    from qtpy.QtWidgets import QToolBar
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    names = [a.text() for a in w.findChild(QToolBar).actions() if a.text()]
    assert names[-2:] == ["Open results…", "About"]
    w.close()
