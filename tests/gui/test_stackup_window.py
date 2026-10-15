"""The Stackup window: locked to KiCad's stackup by default, editable when unlocked, in µm,
mm, mil or oz, with the copper resistivity; kept in the setup and used by the views and
solves (#61)."""

import os
import time

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.core.model.stackup import dielectric_er, thicknesses  # noqa: E402
from kipi.dc.pi.model import PISetup  # noqa: E402
from kipi.gui.stackup_window import (  # noqa: E402
    UOHM_CM, StackupWindow, row_unit,
)
from phase_boards import board  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def unit_setting(monkeypatch):
    """Keep the chosen unit out of this machine's KiPI settings."""
    from kipi.gui import stackup_window
    store = {}

    class Settings:
        def __init__(self, *a):
            pass

        def value(self, key, default=None):
            return store.get(key, default)

        def setValue(self, key, value):
            store[key] = value

    monkeypatch.setattr(stackup_window, "QSettings", Settings)
    return store


def _idle(app, w, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if not w._busy() and not w._sync_timer.isActive():
            return
        time.sleep(0.01)
    raise AssertionError("window did not go idle")


def _copper_rows(win):
    return [i for i, e in enumerate(win._order) if hasattr(e, "id")]


def _diel_rows(win):
    return [i for i, e in enumerate(win._order) if not hasattr(e, "id")]


def test_locked_by_default_and_editable_when_unlocked(app, unit_setting):
    st = board().stackup
    win = StackupWindow()
    got = []
    win.stackup_changed.connect(got.append)
    win.set_board(st, setup=PISetup())
    assert win.chk_lock.isChecked()
    assert all(s.isReadOnly() for s in win._thk_spins + win._er_spins)
    assert len(win._thk_spins) == len(thicknesses(st))
    win.chk_lock.setChecked(False)
    assert not any(s.isReadOnly() for s in win._thk_spins + win._er_spins)
    win.cmb_unit.setCurrentText("µm")
    top = win._thk_spins[0]
    top.lineEdit().setText("70um")
    top.interpretText()
    win._er_spins[0].setValue(3.7)
    win.flush()
    assert len(got) == 1 and got[0].edited
    assert got[0].copper[0].thickness_mm == pytest.approx(0.070)
    assert got[0].board_thickness_mm == pytest.approx(st.board_thickness_mm + 0.035)
    assert dielectric_er(got[0])[0] == pytest.approx(3.7)
    assert "KiCad" in win.lbl_total.text()
    win.chk_lock.setChecked(True)                       # back to KiCad's values
    assert got[-1] is st
    assert top.value() == pytest.approx(st.copper[0].thickness_mm * 1000)
    assert win._er_spins[0].value() == pytest.approx(dielectric_er(st)[0])


@pytest.mark.parametrize("unit, copper, dielectric", [
    ("µm", "µm", "µm"), ("mm", "mm", "mm"), ("mil", "mil", "mil"), ("oz", "oz", "mil"),
])
def test_row_unit(unit, copper, dielectric):
    assert row_unit(unit, True) == copper
    assert row_unit(unit, False) == dielectric


def test_thicknesses_show_and_edit_in_every_unit(app, unit_setting):
    st = board().stackup
    win = StackupWindow()
    got = []
    win.stackup_changed.connect(got.append)
    win.set_board(st, setup=PISetup())
    win.chk_lock.setChecked(False)
    cu, d = _copper_rows(win)[0], _diel_rows(win)[0]
    cu_mm, d_mm = thicknesses(st)[cu], thicknesses(st)[d]
    for unit, per_mm in (("mm", 1.0), ("mil", 1 / 0.0254), ("µm", 1000.0)):
        win.cmb_unit.setCurrentText(unit)
        assert win._thk_spins[cu].suffix() == f" {unit}"
        assert win._thk_spins[cu].value() == pytest.approx(cu_mm * per_mm, rel=1e-4)
        assert win._thk_spins[d].value() == pytest.approx(d_mm * per_mm, rel=1e-4)
    win.cmb_unit.setCurrentText("oz")
    assert unit_setting["stackup/thickness_unit"] == "oz"
    assert win._thk_spins[cu].suffix() == " oz"
    assert win._thk_spins[d].suffix() == " mil"          # ounces are copper weight
    win._thk_spins[cu].setValue(2.0)                     # 2 oz copper
    win.flush()
    assert got[-1].copper[0].thickness_mm == pytest.approx(0.070)
    assert got[-1].dielectrics[0].thickness_mm == pytest.approx(d_mm)
    win.cmb_unit.setCurrentText("mm")                    # showing it differently edits nothing
    win.flush()
    assert len(got) == 1


def test_resistivity_and_tcr_edit_the_setup(app, unit_setting):
    st = board().stackup
    setup = PISetup()
    win = StackupWindow()
    changed = []
    win.settings_changed.connect(lambda: changed.append(1))
    win.set_board(st, setup=setup)
    assert win.spin_rho.value() == pytest.approx(setup.settings.rho_ohm_mm / UOHM_CM)
    win.spin_rho.setValue(2.0)
    win.spin_tcr.setValue(0.004)
    win._rho_spins[0].setValue(1.9)
    assert setup.settings.rho_ohm_mm == pytest.approx(2.0 * UOHM_CM)
    assert setup.settings.tcr_per_c == pytest.approx(0.004)
    assert setup.settings.rho_by_layer == {st.copper[0].name: pytest.approx(1.9 * UOHM_CM)}
    win._rho_spins[0].setValue(0.0)                      # "default" again
    assert setup.settings.rho_by_layer == {}
    assert len(changed) == 4
    # Locked: the resistivity still edits, it is not part of KiCad's stackup.
    assert win.chk_lock.isChecked() and win.spin_rho.isEnabled()
    assert not win._rho_spins[0].isReadOnly()


def test_toolbar_opens_the_window_and_the_copper_tab_is_gone(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    names = [a.text() for a in w.findChildren(type(w.act_stackup)) if a.text()]
    assert names.index("Open results…") < names.index("Stackup") < names.index("Help")
    tabs = [w.solve_panel.tabs.tabText(i) for i in range(w.solve_panel.tabs.count())]
    assert "Copper" not in tabs
    w.act_stackup.trigger()
    assert w.stackup_window.isVisible()
    w.close()


def test_edited_stackup_drives_the_board_and_is_saved(app, no_3d, tmp_path, unit_setting):
    from kipi.gui.main_window import MainWindow
    snap = board()
    snap.project_dir = str(tmp_path)
    kicad = snap.stackup
    w = MainWindow()
    w._set_snapshot(snap)
    _idle(app, w)
    win = w.stackup_window
    win.chk_lock.setChecked(False)
    win.cmb_unit.setCurrentText("mm")
    for spin, t in zip(win._thk_spins, thicknesses(kicad)):
        spin.setValue(t * 2)
    win.flush()
    _idle(app, w)
    assert w.snapshot.stackup.edited
    assert w.snapshot.stackup.board_thickness_mm == pytest.approx(2 * kicad.board_thickness_mm)
    assert "edited in KiPI" in w.info.toPlainText()
    win.spin_rho.setValue(2.0)
    w._save_setup()
    saved = PISetup.load(w._setup_path)
    assert saved.stackup["copper_mm"][kicad.copper[0].name] == pytest.approx(
        2 * kicad.copper[0].thickness_mm)
    assert saved.settings.rho_ohm_mm == pytest.approx(2.0 * UOHM_CM)

    # Opening the board again uses the saved stackup, unlocked.
    again = board()
    again.project_dir = str(tmp_path)
    w._set_snapshot(again)
    _idle(app, w)
    assert w.snapshot.stackup.edited and not win.chk_lock.isChecked()
    assert w._imported_stackup.board_thickness_mm == pytest.approx(kicad.board_thickness_mm)
    assert win.spin_rho.value() == pytest.approx(2.0)

    # Locking again goes back to KiCad's, and the setup forgets the edit.
    win.chk_lock.setChecked(True)
    _idle(app, w)
    assert not w.snapshot.stackup.edited
    assert w.solve_panel.setup.stackup is None
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


def test_choosing_a_material_sets_its_er_and_k(app, unit_setting):
    from kipi.core.model.stackup import dielectric_materials
    from kipi.dc.thermal.materials import laminate_conductance
    st = board().stackup
    win = StackupWindow()
    got = []
    win.stackup_changed.connect(got.append)
    win.set_board(st, setup=PISetup())
    box = win._mat_boxes[0]
    assert not box.isEnabled()                           # locked
    kicad_name = dielectric_materials(st)[0]
    assert box.currentData() == kicad_name
    win.chk_lock.setChecked(False)
    assert box.isEnabled()
    row = _diel_rows(win)[0]
    box.setCurrentIndex(box.findData("Rogers RO4350B"))
    assert win._er_spins[0].value() == pytest.approx(3.48)
    assert win.table.item(row, 3).text() == "0.69"
    win.flush()
    edited = got[-1]
    assert dielectric_materials(edited)[0] == "Rogers RO4350B"
    assert dielectric_er(edited)[0] == pytest.approx(3.48)
    assert laminate_conductance(edited)[0] != pytest.approx(laminate_conductance(st)[0])
    box.setCurrentIndex(box.findData(kicad_name))        # KiCad's material: KiCad's εr
    assert win._er_spins[0].value() == pytest.approx(dielectric_er(st)[0])
    win.flush()
    assert got[-1] is st                                 # nothing edited any more
    box.setCurrentIndex(box.findData("PTFE"))
    win.chk_lock.setChecked(True)
    assert box.currentData() == kicad_name and got[-1] is st
