# Copyright (c) 2026 Tomachie LLC.
"""Undo and redo of setup edits from the main toolbar (#240), the last 25 kept."""

import os

import pytest

from kipi.dc.pi.model import PISetup
from kipi.gui.setup_history import SetupHistory


def _setup(ambient: float) -> PISetup:
    s = PISetup()
    s.thermal.ambient_c = ambient
    return s


def test_undo_and_redo_step_through_the_edits():
    h = SetupHistory()
    h.reset(_setup(25))
    assert not h.can_undo() and not h.can_redo()
    assert h.record(_setup(30)) and h.record(_setup(35))
    assert not h.record(_setup(35))                  # nothing changed: not a step
    assert h.undo().thermal.ambient_c == 30
    assert h.undo().thermal.ambient_c == 25
    assert h.undo() is None
    assert h.redo().thermal.ambient_c == 30
    h.record(_setup(40))                             # an edit drops what could be redone
    assert not h.can_redo()
    assert h.undo().thermal.ambient_c == 30


def test_only_the_last_edits_are_kept():
    h = SetupHistory(limit=3)
    h.reset(_setup(0))
    for t in range(1, 6):
        h.record(_setup(t))
    undone = []
    while h.can_undo():
        undone.append(h.undo().thermal.ambient_c)
    assert undone == [4, 3, 2]


def test_a_setup_handed_back_is_a_copy():
    h = SetupHistory()
    h.reset(_setup(25))
    h.record(_setup(30))
    back = h.undo()
    back.thermal.ambient_c = 99                      # the panels edit it from here
    assert h.redo().thermal.ambient_c == 30
    assert h.undo().thermal.ambient_c == 25


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyvistaqt")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_toolbar_undo_puts_the_setup_back_and_redo_again(app, no_3d):
    from tree_boards import board
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    try:
        assert not w.act_undo.isEnabled() and not w.act_redo.isEnabled()   # no board
        w.snapshot = board()
        w._load_setup(w.snapshot)
        w._history.reset(w._setup_state())
        w._show_history()
        panel = w.solve_panel
        start = panel.setup.thermal.ambient_c
        panel.spin_ambient.setValue(start + 10)       # an edit, still settling
        assert w.act_undo.isEnabled()
        w.act_undo.trigger()
        assert panel.setup.thermal.ambient_c == start
        assert panel.spin_ambient.value() == start
        assert w.act_redo.isEnabled() and not w.act_undo.isEnabled()
        w.act_redo.trigger()
        assert panel.setup.thermal.ambient_c == start + 10
        assert panel.spin_ambient.value() == start + 10
        # The AC panel shares the setup put back.
        assert w.ac_panel.setup is panel.setup
        assert not w._history_timer.isActive()       # putting it back is not an edit
        assert "Redid a setup change" in w.log_view.toPlainText()
    finally:
        w.close()


def test_undo_and_redo_wait_while_a_task_runs(app, no_3d, monkeypatch):
    from tree_boards import board
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    try:
        w.snapshot = board()
        w._load_setup(w.snapshot)
        w._history.reset(w._setup_state())
        w.solve_panel.spin_ambient.setValue(60)
        w._record_setup()
        monkeypatch.setattr(w, "_busy", lambda: True)
        w._show_history()
        assert not w.act_undo.isEnabled()
        w.undo_setup()
        assert w.solve_panel.setup.thermal.ambient_c == 60
    finally:
        w.close()
