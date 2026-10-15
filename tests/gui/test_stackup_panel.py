"""The Stackup pane: locked to KiCad's thicknesses by default, editable when unlocked,
kept in the setup, and used by the views and solves (#61)."""

import os
import time

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.core.model.stackup import thicknesses  # noqa: E402
from kipi.dc.pi.model import PISetup  # noqa: E402
from kipi.gui.stackup_panel import ManualStackupDialog, StackupPanel  # noqa: E402
from phase_boards import board  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _idle(app, w, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if not w._busy() and not w._sync_timer.isActive():
            return
        time.sleep(0.01)
    raise AssertionError("window did not go idle")


def test_locked_by_default_and_editable_when_unlocked(app):
    st = board().stackup
    p = StackupPanel()
    got = []
    p.stackup_changed.connect(got.append)
    p.set_stackup(st)
    assert p.chk_lock.isChecked()
    assert all(s.isReadOnly() for s in p._spins)
    assert not p.btn_copy.isEnabled()
    assert len(p._spins) == len(thicknesses(st))
    p.chk_lock.setChecked(False)
    assert not any(s.isReadOnly() for s in p._spins)
    p._spins[0].lineEdit().setText("2 mil")            # not a unit the field reads
    p._spins[0].interpretText()
    p._spins[0].lineEdit().setText("70um")
    p._spins[0].interpretText()
    p._emit_edit()
    assert len(got) == 1 and got[0].edited
    assert got[0].copper[0].thickness_mm == pytest.approx(0.070)
    assert got[0].board_thickness_mm == pytest.approx(st.board_thickness_mm + 0.035)
    assert "KiCad" in p.lbl_total.text()
    p.chk_lock.setChecked(True)                         # back to KiCad's values
    assert got[-1] is st
    assert p._spins[0].value() == pytest.approx(st.copper[0].thickness_mm * 1000)


def test_manual_dialog_lists_the_thicknesses(app):
    d = ManualStackupDialog(board().stackup, "no API")
    lines = d.text().splitlines()
    assert lines[0].startswith("F.Cu\t")
    assert len(lines) == len(thicknesses(board().stackup))


def test_edited_stackup_drives_the_board_and_is_saved(app, no_3d, tmp_path):
    from kipi.gui.main_window import MainWindow
    snap = board()
    snap.project_dir = str(tmp_path)
    kicad = snap.stackup
    w = MainWindow()
    w._set_snapshot(snap)
    _idle(app, w)
    p = w.stackup_panel
    p.chk_lock.setChecked(False)
    new = [t * 2 for t in thicknesses(kicad)]
    for spin, t in zip(p._spins, new):
        spin.setValue(t * 1000)
    p._emit_edit()
    _idle(app, w)
    assert w.snapshot.stackup.edited
    assert w.snapshot.stackup.board_thickness_mm == pytest.approx(2 * kicad.board_thickness_mm)
    assert "edited in KiPI" in w.info.toPlainText()
    w._save_setup()
    saved = PISetup.load(w._setup_path)
    assert saved.stackup["copper_mm"][kicad.copper[0].name] == pytest.approx(
        2 * kicad.copper[0].thickness_mm)

    # Opening the board again uses the saved thicknesses, unlocked.
    again = board()
    again.project_dir = str(tmp_path)
    w._set_snapshot(again)
    _idle(app, w)
    assert w.snapshot.stackup.edited and not p.chk_lock.isChecked()
    assert w._imported_stackup.board_thickness_mm == pytest.approx(kicad.board_thickness_mm)

    # Locking again goes back to KiCad's, and the setup forgets the edit.
    p.chk_lock.setChecked(True)
    _idle(app, w)
    assert not w.snapshot.stackup.edited
    assert w.solve_panel.setup.stackup is None
    w.close()


def test_copy_without_kicad_lists_the_values(app, no_3d, monkeypatch):
    from kipi.gui import stackup_panel
    from kipi.gui.main_window import MainWindow
    shown = []
    monkeypatch.setattr(stackup_panel.ManualStackupDialog, "exec",
                        lambda self: shown.append(self.text()))
    w = MainWindow()
    w._copy_stackup_to_kicad(board().stackup)          # no board connected
    assert shown and shown[0].startswith("F.Cu")
    w.close()


def test_a_change_during_a_task_waits_for_it(app, no_3d, monkeypatch, tmp_path):
    from kipi.core.model.stackup import restack
    from kipi.gui.main_window import MainWindow
    snap = board()
    snap.project_dir = str(tmp_path)
    w = MainWindow()
    w._set_snapshot(snap)
    _idle(app, w)
    edited = restack(snap.stackup, thicknesses(snap.stackup))
    monkeypatch.setattr(w, "_busy", lambda: True)
    w._stackup_changed(edited)
    assert w.snapshot.stackup is not edited and w._pending_stackup is edited
    monkeypatch.setattr(w, "_busy", lambda: False)
    w._on_task_finished()
    _idle(app, w)
    assert w.snapshot.stackup is edited
    w.close()
