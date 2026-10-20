# Portions Copyright (c) 2026 Tomachie LLC.
"""VRM load line (#94): the regulated voltage droops by R_LL times the output current its
connected loads (and the VRMs it feeds) actually draw."""

import pytest

from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm, validate
from kipi.dc.pi.tree import solve_tree
from tree_boards import R, board, by_name, pad, setup


def test_load_line_droops_the_output_pins():
    s = setup("ldo")
    s.vrms[1].load_line_mohm = 10.0
    res = solve_tree(board(), s)
    u1, ld = by_name(res.vrms, "U1"), by_name(res.loads, "U2")
    assert u1.v_droop == pytest.approx(0.01, rel=1e-9)
    assert u1.v_target == pytest.approx(3.29, rel=1e-9)
    assert u1.v_out == pytest.approx(3.29, rel=1e-9)
    assert ld.v_supply == pytest.approx(3.29 - R(8), rel=1e-9)
    # Drop is still measured from the set point, so the droop counts against tolerance.
    assert ld.nominal == 3.3 and ld.drop == pytest.approx(0.01 + R(8), rel=1e-9)


def test_load_line_with_remote_sense_droops_the_sense_pins():
    s = setup("ldo", sense=True)
    s.vrms[1].load_line_mohm = 10.0
    u1 = by_name(solve_tree(board(), s).vrms, "U1")
    assert u1.v_sense == pytest.approx(3.29, abs=1e-9)
    assert u1.v_out == pytest.approx(3.29 + R(8), rel=1e-9)


def test_load_line_follows_the_downstream_vrm_input_current():
    # J1 feeds a switcher, whose input current depends on the voltage J1's droop leaves.
    s = setup("smps", eff=0.85, iq=0.01)
    s.vrms[0].load_line_mohm = 100.0
    res = solve_tree(board(), s)
    j1, u1 = by_name(res.vrms, "J1"), by_name(res.vrms, "U1")
    assert j1.i_out == pytest.approx(u1.i_in, rel=1e-9)
    assert j1.v_droop == pytest.approx(0.1 * j1.i_out, rel=1e-9)
    assert j1.v_out == pytest.approx(12.0 - 0.1 * j1.i_out, rel=1e-9)
    assert u1.v_in == pytest.approx(12.0 - (0.1 + R(8)) * u1.i_in, rel=1e-8)


def test_disconnected_loads_add_no_droop():
    from shapely.geometry import box
    snap = board()
    snap.pads.append(pad("u3_1", "+3V3", "U3", "1", box(30, 0, 31, 1)))   # an island
    s = setup("ldo")
    s.vrms[1].load_line_mohm = 10.0
    s.loads.append(Load("U3", PinRef("U3", "+3V3", ["1"]), 5.0))
    u1 = by_name(solve_tree(snap, s).vrms, "U1")
    assert u1.i_out == pytest.approx(1.0, rel=1e-9)
    assert u1.v_droop == pytest.approx(0.01, rel=1e-9)


def test_negative_rail_droops_toward_zero():
    s = setup()
    s.vrms = [Vrm("U1", PinRef("U1", "+3V3", ["2"]), -3.3, "source", load_line_mohm=10.0)]
    s.loads[0].current = -1.0
    u1 = by_name(solve_tree(board(), s).vrms, "U1")
    assert u1.v_out == pytest.approx(-3.29, rel=1e-9)


def test_no_load_line_leaves_the_set_point():
    u1 = by_name(solve_tree(board(), setup("ldo")).vrms, "U1")
    assert u1.v_droop == 0.0 and u1.v_out == pytest.approx(3.3, abs=1e-12)


def test_load_line_is_saved_and_checked():
    s = setup("ldo")
    s.vrms[1].load_line_mohm = 1.5
    assert PISetup.from_dict(s.to_dict()).vrms[1].load_line_mohm == 1.5
    old = s.to_dict()
    del old["vrms"][1]["load_line_mohm"]                   # a setup from before #94
    assert PISetup.from_dict(old).vrms[1].load_line_mohm == 0.0
    s.vrms[1].load_line_mohm = -1.0
    assert any("load line cannot be negative" in e for e in validate(s))


def test_old_results_read_without_a_load_line():
    import pickle
    u1 = by_name(solve_tree(board(), setup("ldo")).vrms, "U1")
    state = dict(u1.__dict__)
    del state["load_line_mohm"], state["v_droop"]
    old = object.__new__(type(u1))
    old.__setstate__(state)
    assert old.v_droop == 0.0 and old.v_target == old.v_set
    assert pickle.loads(pickle.dumps(old)).v_target == old.v_set


def test_reports_show_the_load_line(tmp_path):
    import csv
    from kipi.dc.pi.report import export_csv
    snap = board()
    s = setup("ldo")
    s.vrms[1].load_line_mohm = 10.0
    tree = solve_tree(snap, s)
    export_csv(tree, snap, tmp_path)
    with open(tmp_path / "kipi_vrms.csv", encoding="utf-8") as fh:
        row = next(r for r in csv.DictReader(fh) if r["vrm"] == "U1")
    assert float(row["load_line_mOhm"]) == 10.0
    assert float(row["v_target_V"]) == pytest.approx(3.29, rel=1e-9)


def test_editor_and_notes_show_the_load_line():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.dc.gui.pi_panel import PIPanel
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    s = setup("ldo")
    panel = PIPanel()
    panel.set_board(board(), s)
    panel.vrm_list.lst.setCurrentRow(1)
    ed = panel.vrm_list.editor
    assert ed.load_line.text() == "none"
    ed.load_line.setValue(10.0)
    assert s.vrms[1].load_line_mohm == 10.0
    tree = solve_tree(board(), s)
    panel.show_results(tree, next(iter(tree.groups)))
    notes = panel.txt_notes.toPlainText()
    assert "U1 load line: 10 mΩ × 1 A = 10 mV droop, regulating to 3.29000 V" in notes
    panel.close()


def test_editor_input_range_is_one_row_in_volts_or_percent():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.dc.gui.pi_panel import PIPanel
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    s = setup("smps")
    panel = PIPanel()
    panel.set_board(board(), s)
    panel.vrm_list.lst.setCurrentRow(1)
    ed = panel.vrm_list.editor
    assert ed.form.labelForField(ed.vin_row).text() == "Input range"
    assert ed.vin_row.min.parent() is ed.vin_row and ed.vin_row.max.parent() is ed.vin_row
    assert ed.form.labelForField(ed.vin_row.min) is None
    ed.vin_row.min.setValue(10.8)
    ed.vin_row.max.setValue(13.2)
    assert (s.vrms[1].vin_min, s.vrms[1].vin_max, s.vrms[1].vin_pct) == (10.8, 13.2, False)
    ed.vin_row.unit.setCurrentIndex(ed.vin_row.unit.findData("%"))         # of J1's 12 V
    u1 = s.vrms[1]
    assert u1.vin_pct and ed.vin_row.min.suffix() == " %"
    assert (u1.vin_min, u1.vin_max) == (pytest.approx(-10.0), pytest.approx(10.0))
    ed.vin_row.max.setValue(ed.vin_row.max.minimum())                      # "not set"
    ed.vin_row.unit.setCurrentIndex(ed.vin_row.unit.findData("V"))
    assert (u1.vin_min, u1.vin_max, u1.vin_pct) == (pytest.approx(10.8), None, False)
    s.vrms[0].voltage = 0.0                    # no nominal input: nothing to convert from
    ed.vin_row.unit.setCurrentIndex(ed.vin_row.unit.findData("%"))
    assert (u1.vin_min, u1.vin_max, u1.vin_pct) == (None, None, True)
    panel.vrm_list.lst.setCurrentRow(0)                            # a source has no input
    assert not ed.form.isRowVisible(ed.vin_row)
    panel.vrm_list.lst.setCurrentRow(1)
    assert ed.vin_row.unit.currentData() == "%" and ed.vin_row.min.text() == "not set"
    panel.close()


def test_editor_vrm_dc_accuracy_is_min_and_max_in_percent_or_volts():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.dc.gui.pi_panel import PIPanel
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    s = setup("ldo")
    panel = PIPanel()
    panel.set_board(board(), s)
    panel.vrm_list.lst.setCurrentRow(1)                            # the 3.3 V LDO
    ed = panel.vrm_list.editor
    v = s.vrms[1]
    assert ed.form.labelForField(ed.acc_row).text() == "DC accuracy"
    assert ed.form.isRowVisible(ed.acc_row) and ed.acc_row.pct()
    assert ed.acc_row.min.isEnabled() and ed.bandwidth.isEnabled()  # AC only, still editable
    assert "Used by the AC analysis only" in ed.acc_row.min.toolTip()
    ed.acc_row.min.setValue(-1.0)
    ed.acc_row.max.setValue(2.0)
    assert (v.acc_min, v.acc_max, v.acc_pct) == (-1.0, 2.0, True)
    assert v.accuracy_v() == pytest.approx(0.066)                  # +2 % of 3.3 V
    ed.acc_row.unit.setCurrentIndex(ed.acc_row.unit.findData("V"))
    assert (v.acc_min, v.acc_max) == (pytest.approx(3.267), pytest.approx(3.366))
    assert v.acc_pct is False and v.accuracy_v() == pytest.approx(0.066)
    v2 = PISetup.from_dict(s.to_dict()).vrms[1]
    assert (v2.acc_min, v2.acc_max, v2.acc_pct) == (v.acc_min, v.acc_max, False)
    panel.vrm_list.lst.setCurrentRow(0)                            # a source has none
    assert not ed.form.isRowVisible(ed.acc_row)
    v.acc_min, v.acc_max = 3.4, 3.3
    assert any("DC accuracy minimum is above its maximum" in e for e in validate(s))
    panel.close()


def test_editor_load_tolerance_is_min_and_max_in_percent_or_volts():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.dc.gui.pi_panel import PIPanel
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    s = setup("ldo")                                   # U2: ±1 % of U1's 3.3 V
    panel = PIPanel()
    panel.set_board(board(), s)
    panel.load_list.lst.setCurrentRow(0)
    ed = panel.load_list.editor
    u2 = s.loads[0]
    assert ed.form.labelForField(ed.tolerance_line).text() == "Tolerance"
    assert ed.tol.unit.currentData() == "%" and ed.tol.values() == (-1.0, 1.0)
    assert ed.nominal_v.value() == ed.nominal_v.minimum()
    ed.nominal_v.setValue(3.0)
    assert u2.nominal_v == pytest.approx(3.0)
    assert u2.limits(3.3) == pytest.approx((2.97, 3.03))
    ed.tol.min.setValue(-3.0)
    assert (u2.tol_min, u2.tol_max, u2.tol_pct) == (-3.0, 1.0, True)
    ed.tol.unit.setCurrentIndex(ed.tol.unit.findData("V"))
    assert u2.tol_pct is False and ed.tol.min.suffix() == " V"
    assert (u2.tol_min, u2.tol_max) == (pytest.approx(2.91), pytest.approx(3.03))
    ed.tol.max.setValue(ed.tol.max.minimum())                      # "not set"
    assert u2.tol_max is None and u2.tolerance_label() == "min 2.91 V"
    panel.close()


def test_multiphase_load_line_droops_the_mean_phase_output():
    from phase_boards import board as phase_board, two_phase
    s = two_phase()
    s.vrms[1].load_line_mohm = 5.0
    u1 = by_name(solve_tree(phase_board(), s).vrms, "U1")
    assert u1.v_droop == pytest.approx(5e-3 * u1.i_out, rel=1e-9)
    assert u1.v_out == pytest.approx(u1.v_set - u1.v_droop, abs=1e-9)   # mean of the phases
