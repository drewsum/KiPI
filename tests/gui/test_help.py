"""The user guide ships with the plugin and the toolbar's Help opens it."""

import os
from pathlib import Path

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.gui import browser  # noqa: E402
from kipi.gui import help as kipi_help  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    """A real message box would wait for a click forever; tests that expect one patch it."""
    def fail(*args):
        raise AssertionError(f"unexpected message box: {args[1:]}")
    monkeypatch.setattr(kipi_help.QMessageBox, "information", fail)


def test_guide_ships_with_the_plugin():
    path = kipi_help.help_path()
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "<title>" in text and "KiPI" in text


def test_guide_images_exist():
    import re
    path = kipi_help.help_path()
    for src in re.findall(r'<img[^>]+src="([^"]+)"', path.read_text(encoding="utf-8")):
        assert (path.parent / src).is_file(), src


def test_guide_links_resolve():
    """Every in-page link points at an element id."""
    import re
    text = kipi_help.help_path().read_text(encoding="utf-8")
    ids = set(re.findall(r'\sid="([^"]+)"', text))
    targets = set(re.findall(r'href="#([^"]+)"', text))
    assert targets and targets <= ids, sorted(targets - ids)


def test_open_help_uses_the_desktop_browser(app, monkeypatch):
    opened = []
    monkeypatch.setattr(browser.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toLocalFile()) or True)
    assert kipi_help.open_help()
    assert opened and opened[0].endswith("help.html")


def test_open_help_falls_back_to_webbrowser(app, monkeypatch):
    import webbrowser
    opened = []
    monkeypatch.setattr(browser.QDesktopServices, "openUrl", lambda url: False)
    monkeypatch.setattr(webbrowser, "open", lambda uri: opened.append(uri) or True)
    assert kipi_help.open_help()
    assert opened and opened[0].startswith("file:")


def test_open_help_says_where_the_guide_is_when_no_browser_opens(app, monkeypatch):
    import webbrowser
    shown = []
    monkeypatch.setattr(browser.QDesktopServices, "openUrl", lambda url: False)
    monkeypatch.setattr(webbrowser, "open", lambda uri: False)
    monkeypatch.setattr(kipi_help.QMessageBox, "information",
                        lambda parent, title, text: shown.append(text))
    assert not kipi_help.open_help()
    assert shown and "help.html" in shown[0]


def test_open_help_without_the_guide_points_online(app, monkeypatch):
    shown = []
    monkeypatch.setattr(kipi_help, "help_path", lambda: None)
    monkeypatch.setattr(kipi_help.QMessageBox, "information",
                        lambda parent, title, text: shown.append(text))
    assert not kipi_help.open_help()
    assert shown and kipi_help.ONLINE in shown[0]


def test_every_control_has_a_tooltip(app, no_3d):
    """Buttons, check boxes, combo boxes and spin boxes all explain themselves on hover."""
    from qtpy.QtWidgets import (
        QAbstractSpinBox, QCheckBox, QComboBox, QLineEdit, QPushButton, QRadioButton,
        QToolBar,
    )
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    missing = []
    for cls in (QPushButton, QCheckBox, QRadioButton, QComboBox, QAbstractSpinBox):
        for c in w.findChildren(cls):
            if isinstance(c.parent(), (QComboBox, QAbstractSpinBox)):
                continue                     # a combo's or spin box's own parts
            if not c.toolTip():
                missing.append(f"{cls.__name__} {c.text() if hasattr(c, 'text') else ''}"
                               f"{c.objectName()}")
    for c in w.findChildren(QLineEdit):
        if isinstance(c.parent(), (QComboBox, QAbstractSpinBox)):
            continue
        if not c.toolTip():
            missing.append(f"QLineEdit {c.placeholderText()}")
    for a in w.findChild(QToolBar).actions():
        if a.text() and a.toolTip() == a.text():
            missing.append(f"toolbar {a.text()}")
    w.close()
    assert not missing, missing


def test_a_new_report_opens_in_the_browser(app, no_3d, opened_in_browser, tmp_path):
    """A report a solve just wrote opens by itself; a solve without one opens nothing."""
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    w._open_new_report(None)
    assert opened_in_browser == []
    report = tmp_path / "board_kipi_report.html"
    report.write_text("<html></html>", encoding="utf-8")
    w._open_new_report(report)
    assert [Path(p) for p in opened_in_browser] == [report]
    w.close()
