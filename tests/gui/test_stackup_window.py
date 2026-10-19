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
    C_K, UOHM_CM, StackupWindow, row_unit,
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
    # Locked: the board's resistivity still edits; a layer's own is locked with the rest.
    assert win.chk_lock.isChecked() and win.spin_rho.isEnabled()
    assert win._rho_spins[0].isReadOnly()


def test_loss_tangent_and_default_er_edit_the_ac_setup(app, unit_setting):
    """They moved here from the AC Mesh tab (#134), and like resistivity are not locked."""
    setup = PISetup()
    setup.ac.loss_tangent, setup.ac.default_er = 0.015, 3.9
    win = StackupWindow()
    changed = []
    win.settings_changed.connect(lambda: changed.append(1))
    win.set_board(board().stackup, setup=setup)
    assert win.spin_tand.value() == pytest.approx(0.015)
    assert win.spin_er.value() == pytest.approx(3.9)
    assert win.chk_lock.isChecked() and win.spin_tand.isEnabled() and win.spin_er.isEnabled()
    win.spin_tand.setValue(0.01)
    win.spin_er.setValue(4.5)
    assert (setup.ac.loss_tangent, setup.ac.default_er) == (pytest.approx(0.01), 4.5)
    assert len(changed) == 2


def test_ac_mesh_tab_has_no_dielectric_settings(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    assert not hasattr(w.ac_panel, "spin_tand") and not hasattr(w.ac_panel, "spin_er")
    w.close()


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
    box.setCurrentIndex(box.findData("Rogers RO4350B"))
    assert win._er_spins[0].value() == pytest.approx(3.48)
    assert win._k_spins[0].value() == pytest.approx(0.69)
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


def test_choosing_a_conductor_sets_rho_tcr_and_k(app, unit_setting):
    st = board().stackup
    setup = PISetup()
    win = StackupWindow()
    changed = []
    win.settings_changed.connect(lambda: changed.append(1))
    win.set_board(st, setup=setup)
    name = st.copper[0].name
    box = win._cond_boxes[name]
    assert win.chk_lock.isChecked() and not box.isEnabled()          # locked
    assert all(sp.isReadOnly() for sp in win._rho_spins)
    assert win.spin_rho.isEnabled() and win.spin_tcr.isEnabled()      # the board's: never
    win.chk_lock.setChecked(False)
    assert box.isEnabled() and not any(sp.isReadOnly() for sp in win._rho_spins)
    assert box.currentData() == ""
    row = _copper_rows(win)[0]
    assert win.table.item(row, C_K).text() == "390"
    box.setCurrentIndex(box.findData("Aluminum"))
    s = setup.settings
    assert s.conductor_by_layer == {name: "Aluminum"}
    assert s.rho_for(name) == pytest.approx(2.65 * UOHM_CM)
    assert s.tcr_for(name) == pytest.approx(0.00429)
    assert s.tcr_for(st.copper[1].name) == s.tcr_per_c
    assert win._rho_spins[0].value() == pytest.approx(2.65)
    assert win.table.item(row, C_K).text() == "237"
    win._rho_spins[0].setValue(0.0)                       # "default" drops the conductor
    assert not (s.conductor_by_layer or s.rho_by_layer or s.tcr_by_layer)
    assert box.currentData() == ""
    box.setCurrentIndex(box.findData("Silver"))
    box.setCurrentIndex(box.findData(""))                 # the board's copper again
    assert not (s.conductor_by_layer or s.rho_by_layer or s.tcr_by_layer)
    assert len(changed) == 4
    box.setCurrentIndex(box.findData("Silver"))
    win.chk_lock.setChecked(True)                         # locking keeps the conductor
    assert not box.isEnabled() and box.currentData() == "Silver"
    assert s.conductor_by_layer == {name: "Silver"}
    win.chk_lock.setChecked(False)
    box.setCurrentIndex(box.findData(""))

    # Saved and read back; a hand-typed resistivity shows as custom.
    box.setCurrentIndex(box.findData("Gold"))
    s.rho_by_layer[st.copper[1].name] = 1.9 * UOHM_CM
    back = PISetup.from_dict(setup.to_dict())
    win.set_board(st, setup=back)
    assert win._cond_boxes[name].currentData() == "Gold"
    assert win._cond_boxes[st.copper[1].name].currentText() == "Custom resistivity"


# --- Dielectric k edited in the window (#90) --------------------------------------------------

def test_dielectric_k_can_be_typed_over_and_reset(app, unit_setting):
    from kipi.core.model.stackup import dielectric_k, from_setup, to_setup
    from kipi.dc.thermal.materials import laminate_conductance, lookup
    st = board().stackup
    win = StackupWindow()
    got = []
    win.stackup_changed.connect(got.append)
    win.set_board(st, setup=PISetup())
    spin = win._k_spins[0]
    assert spin.isReadOnly()                             # locked
    assert spin.value() == pytest.approx(lookup(st.dielectrics[0].material).k_w_mk)
    win.chk_lock.setChecked(False)
    spin.setValue(3.5)
    win.flush()
    edited = got[-1]
    assert dielectric_k(edited)[0] == 3.5 and edited.edited
    _, rows = laminate_conductance(edited)
    assert rows[0][2].k_w_mk == 3.5
    again, why = from_setup(st, to_setup(edited))        # kept in the setup file
    assert why == "" and dielectric_k(again)[0] == 3.5
    box = win._mat_boxes[0]                              # a material sets its typical k
    box.setCurrentIndex(box.findData("Rogers RO4350B"))
    assert win._k[0] == 0.0 and spin.value() == pytest.approx(0.69)
    spin.setValue(9.0)
    win.chk_lock.setChecked(True)                        # locking resets it
    assert dielectric_k(got[-1]) == dielectric_k(st) and win._k_spins[0].isReadOnly()


def test_old_setup_laminate_k_moves_to_the_stackup(app, no_3d, tmp_path, unit_setting):
    import json
    from kipi.core.model.stackup import dielectric_k
    from kipi.dc.pi.model import setup_path
    from kipi.gui.main_window import MainWindow
    snap = board()
    snap.project_dir = str(tmp_path)
    path = setup_path(snap.board_name, snap.project_dir)
    path.write_text(json.dumps({"thermal": {"enabled": True, "laminate_k_w_mk": 1.7}}))
    assert PISetup.load(path).legacy_laminate_k == 1.7
    assert "legacy_laminate_k" not in PISetup.load(path).to_dict()
    w = MainWindow()
    w._set_snapshot(snap)
    _idle(app, w)
    st = w.snapshot.stackup
    assert st.edited and set(dielectric_k(st)) == {1.7}
    assert w.solve_panel.setup.stackup["dielectric_k"] == [1.7] * len(st.dielectrics)
    assert not w.stackup_window.chk_lock.isChecked()
    w.close()


def test_snapshots_leave_out_an_unset_k():
    from kipi.core.model import snapshot_io
    snap = board()
    d = snapshot_io.to_dict(snap)
    assert all("k_w_mk" not in x for x in d["stackup"]["dielectrics"])
    snap.stackup.dielectrics[0].k_w_mk = 2.5
    back = snapshot_io.from_dict(snapshot_io.to_dict(snap))
    assert back.stackup.dielectrics[0].k_w_mk == 2.5


def test_solder_masks_and_silkscreens_are_listed_and_editable(app, unit_setting):
    """The coatings sit above and below the copper; a thickness typed while unlocked
    changes them and not the board (#82)."""
    from kipi.core.model.board import MASK, SILK, Coating
    from kipi.core.model.stackup import coating_thicknesses
    st = board().stackup
    st.coatings = [Coating(SILK, "top", 0.015), Coating(MASK, "top", 0.01),
                   Coating(MASK, "bottom", 0.01), Coating(SILK, "bottom", 0.015)]
    win = StackupWindow()
    got = []
    win.stackup_changed.connect(got.append)
    win.set_board(st, setup=PISetup())
    assert len(win._thk_spins) == len(thicknesses(st)) + 4
    names = [win.table.item(i, 0).text() for i in range(win.table.rowCount())]
    assert names[:2] == ["Top silkscreen", "Top solder mask"]
    assert names[-2:] == ["Bottom solder mask", "Bottom silkscreen"]
    assert len(win.view.bands()) == len(names)
    assert win._thk_spins[1].isReadOnly()
    win.chk_lock.setChecked(False)
    win.cmb_unit.setCurrentText("µm")
    mask = win._thk_spins[1]
    mask.lineEdit().setText("25um")
    mask.interpretText()
    win.flush()
    assert len(got) == 1 and got[0].edited
    assert coating_thicknesses(got[0]) == pytest.approx([0.015, 0.025, 0.01, 0.015])
    assert got[0].board_thickness_mm == pytest.approx(st.board_thickness_mm)
    assert thicknesses(got[0]) == pytest.approx(thicknesses(st))
    win.chk_lock.setChecked(True)
    assert got[-1] is st
