"""A load's optional DC and AC tolerances (#174), apart from its total tolerance: saved with
the setup and edited in the load editor, read by nothing yet."""

import os

import pytest

from kipi.dc.pi.model import PISetup, validate
from kipi.dc.pi.tree import solve_tree
from tree_boards import board, by_name, setup


def test_dc_and_ac_tolerances_round_trip_and_default_to_none():
    s = setup("ldo")
    u2 = s.loads[0]
    assert (u2.dc_tol_min, u2.dc_tol_max, u2.ac_tol_min, u2.ac_tol_max) == (None,) * 4
    assert u2.dc_tolerance_label() == u2.ac_tolerance_label() == ""
    u2.dc_tol_min, u2.dc_tol_max, u2.dc_tol_pct = 3.25, 3.35, False
    u2.ac_tol_min, u2.ac_tol_max, u2.ac_tol_pct, u2.ac_tol_dv = -0.03, 0.03, False, True
    back = PISetup.from_dict(s.to_dict()).loads[0]
    assert (back.dc_tol_min, back.dc_tol_max, back.dc_tol_pct) == (3.25, 3.35, False)
    assert (back.ac_tol_min, back.ac_tol_max, back.ac_tol_pct, back.ac_tol_dv) == (
        -0.03, 0.03, False, True)
    assert back.dc_tolerance_label() == "3.25 to 3.35 V"
    assert back.ac_tolerance_label() == "±0.03 V"
    assert (back.tol_min, back.tol_max) == (u2.tol_min, u2.tol_max)    # total untouched


def test_older_setups_have_none():
    d = setup("ldo").to_dict()
    for ld in d["loads"]:
        for k in [k for k in ld if k.startswith(("dc_tol", "ac_tol"))]:
            del ld[k]
    u2 = PISetup.from_dict(d).loads[0]
    assert (u2.dc_tol_min, u2.ac_tol_max, u2.dc_tol_pct, u2.ac_tol_pct) == (
        None, None, True, True)


def test_they_change_no_verdict_and_are_checked():
    s = setup("ldo")
    before = by_name(solve_tree(board(), s).loads, "U2")
    u2 = s.loads[0]
    u2.dc_tol_min, u2.dc_tol_max = -0.001, 0.001          # far tighter than the drop
    u2.ac_tol_min, u2.ac_tol_max = -0.001, 0.001
    after = by_name(solve_tree(board(), s).loads, "U2")
    assert (after.passed, after.tolerance) == (before.passed, before.tolerance)
    assert not validate(s)
    u2.dc_tol_min, u2.dc_tol_max = 1.0, -1.0
    u2.ac_tol_min, u2.ac_tol_max = 2.0, -2.0
    errors = validate(s)
    assert any("DC tolerance minimum is above its maximum" in e for e in errors)
    assert any("AC tolerance minimum is above its maximum" in e for e in errors)


def test_editor_dc_and_ac_tolerance_rows():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.dc.gui.pi_panel import PIPanel
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    s = setup("ldo")                                    # U2 on U1's 3.3 V
    panel = PIPanel()
    panel.set_board(board(), s)
    panel.load_list.lst.setCurrentRow(0)
    ed = panel.load_list.editor
    u2 = s.loads[0]
    assert ed.form.labelForField(ed.dc_tol).text() == "DC tolerance"
    assert ed.form.labelForField(ed.ac_tol).text() == "AC tolerance"
    assert ed.dc_tol.values() == ed.ac_tol.values() == (None, None)
    # AC is a swing about the DC voltage: only from nominal.
    assert [ed.ac_tol.unit.itemData(i) for i in range(ed.ac_tol.unit.count())] == ["±V", "%"]
    assert ed.ac_tol.unit.currentData() == "%"
    ed.dc_tol.min.setValue(-2.0)
    ed.dc_tol.max.setValue(1.0)
    ed.ac_tol.max.setValue(1.5)
    assert (u2.dc_tol_min, u2.dc_tol_max, u2.dc_tol_pct) == (-2.0, 1.0, True)
    assert (u2.ac_tol_min, u2.ac_tol_max, u2.ac_tol_pct) == (None, 1.5, True)
    ed.ac_tol.unit.setCurrentIndex(ed.ac_tol.unit.findData("±V"))    # converts at 3.3 V
    assert (u2.ac_tol_pct, u2.ac_tol_dv) == (False, True)
    assert u2.ac_tol_max == pytest.approx(0.0495)
    assert (u2.tol_min, u2.tol_max) == (-1.0, 1.0)                    # total untouched
    # A unit the AC row does not offer (volts, from a hand-edited file) clears it.
    u2.ac_tol_pct = u2.ac_tol_dv = False
    ed.load(u2)
    assert ed.ac_tol.unit.currentData() == "±V" and ed.ac_tol.values() == (None, None)
    for w in (ed.dc_tol, ed.dc_tol.min, ed.ac_tol.unit):
        assert "<img" in w.toolTip()                                 # the drawing (#116)
    panel.close()
