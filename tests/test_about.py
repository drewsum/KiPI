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



def test_dialog_icon_is_animated(app):
    assert about.animated_icon_path() is not None
    d = about.AboutDialog()
    assert d.movie is not None and d.movie.frameCount() > 1
    assert any(lb.movie() is d.movie for lb in d.findChildren(QLabel))


def test_dialog_falls_back_to_the_still_icon_without_gif_support(app, monkeypatch):
    # As on a Qt build without the GIF image plugin: QMovie cannot read the file.
    monkeypatch.setattr(about.QMovie, "isValid", lambda self: False)
    d = about.AboutDialog()
    assert d.movie is None
    assert any(lb.pixmap() is not None and not lb.pixmap().isNull()
               for lb in d.findChildren(QLabel))


def test_toolbar_has_help_and_about_after_open_results(app, no_3d):
    from qtpy.QtWidgets import QToolBar
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    names = [a.text() for a in w.findChild(QToolBar).actions() if a.text()]
    assert names[-3:] == ["Open results…", "Help", "About"]
    w.close()
