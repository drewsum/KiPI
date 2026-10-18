"""Keyboard shortcuts (#105): Home for the iso view in the 3D view, and keys for the
toolbar, the analysis mode, Solve and Cancel. Each is named in its tooltip."""

import os

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtGui import QKeySequence  # noqa: E402
from qtpy.QtWidgets import QApplication, QToolBar  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    yield w
    w.close()


def _actions(w):
    return {a.text(): a for a in w.findChild(QToolBar).actions() if a.text()}


def _all_keys(w):
    keys = [a.shortcut().toString() for a in w.findChild(QToolBar).actions()]
    keys += [a.shortcut().toString() for a in w.actions()]
    return [k for k in keys if k]


def test_home_is_the_iso_view_while_the_3d_view_has_focus(window):
    from kipi.gui import main_window as mw
    iso = _actions(window)["Iso"]
    assert iso.shortcut() == QKeySequence(mw.VIEW_KEYS["iso"]) == QKeySequence("Home")
    assert iso.shortcutContext() == Qt.WidgetWithChildrenShortcut
    assert iso in window.viewer.actions()
    assert "(Home)" in iso.toolTip()
    for name in ("Top", "Bottom"):
        assert _actions(window)[name] in window.viewer.actions()


def test_iso_key_shows_the_3d_view_and_turns_it(window, monkeypatch):
    turned = []
    monkeypatch.setattr(window.viewer, "view_iso", lambda: turned.append(True))
    window.views.setCurrentWidget(window.plot)
    _actions(window)["Iso"].trigger()
    assert turned and window.views.currentWidget() is window.viewer


def test_every_shortcut_is_used_once_and_shown_in_a_tooltip(window):
    from kipi.gui import main_window as mw
    keys = _all_keys(window)
    assert len(keys) == len(set(keys)), keys
    for a in window.findChild(QToolBar).actions():
        if not a.shortcut().isEmpty():
            assert f"({a.shortcut().toString()})" in a.toolTip(), a.text()
    for name in ("reload", "save", "screenshot", "results", "stackup", "power_tree", "help",
                 "solve", "cancel", "dc", "ac"):
        assert QKeySequence(mw.KEYS[name]).toString() in keys, name
    assert mw.KEYS["solve"] in window.solve_panel.btn_solve.toolTip()
    assert mw.KEYS["cancel"] in window.btn_cancel.toolTip()


def test_mode_keys_switch_the_analysis_mode(window):
    from kipi.gui import main_window as mw
    by_key = {a.shortcut().toString(): a for a in window.actions()}
    by_key[QKeySequence(mw.KEYS["ac"]).toString()].trigger()
    assert window.mode == "ac"
    by_key[QKeySequence(mw.KEYS["dc"]).toString()].trigger()
    assert window.mode == "dc"


def test_esc_cancels_only_while_something_runs(window):
    assert not window.act_cancel.isEnabled()       # Esc still closes popups as usual


def test_solve_key_presses_solve_only_when_it_can_be_pressed(window, monkeypatch):
    class Button:
        enabled = False
        pressed = 0

        def isEnabled(self):
            return self.enabled

        def click(self):
            self.pressed += 1

    button = Button()
    monkeypatch.setattr(window, "_mode_panel", lambda: type("P", (), {"btn_solve": button}))
    window._solve_shortcut()
    assert button.pressed == 0
    button.enabled = True
    window._solve_shortcut()
    assert button.pressed == 1
