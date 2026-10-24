"""A DC result's voltages in volts, or in mV or % from the target voltage (#177)."""

import os
from types import SimpleNamespace

import pytest

from kipi.dc.pi import vunits
from kipi.dc.pi.vunits import MV, PCT, VOLTS


def _load(**kw):
    base = dict(nominal=3.3, v_effective=3.29, v_low=3.25, v_high=3.31, limit_low=3.2,
                limit_high=3.35)
    return SimpleNamespace(**{**base, **kw})


def test_a_voltage_in_each_unit():
    assert vunits.show(3.25, 3.3, VOLTS) == "3.25000"
    assert vunits.show(3.25, 3.3, MV) == "-50.000"
    assert vunits.show(3.35, 3.3, MV) == "+50.000"                # a boost
    assert vunits.show(3.25, 3.3, PCT) == "-1.515"
    assert vunits.show(0.01, 0.0, PCT) == ""                      # % of a 0 V ground
    assert vunits.show(float("nan"), 3.3, MV) == vunits.show(None, 3.3, MV) == ""
    assert vunits.show(-3.25, -3.3, PCT) == "+1.515"              # nearer 0 on a negative rail


def test_a_loads_margin_is_its_closer_pin_to_the_tolerance():
    assert vunits.margin(_load()) == pytest.approx(0.04)          # 3.35 - 3.31
    assert vunits.margin(_load(v_low=3.15)) == pytest.approx(-0.05)   # outside, below
    assert vunits.margin(_load(limit_high=None)) == pytest.approx(0.05)
    assert vunits.margin(_load(limit_low=None, limit_high=None)) is None
    assert vunits.margin(_load(v_high=None)) == pytest.approx(0.05)    # saved before boosts


def test_a_loads_cells_in_each_unit():
    assert vunits.load_cells(_load(), MV) == ["-10.000", "-50.000", "+10.000",
                                              "-100.000 / +50.000", "+40.000"]
    assert vunits.load_cells(_load(), VOLTS) == ["3.29000", "3.25000", "3.31000",
                                                 "3.20000 to 3.35000", "+0.04000"]
    assert vunits.load_cells(_load(limit_low=None), PCT)[3] == "— / +1.515"
    assert vunits.load_cells(_load(limit_low=None, limit_high=None), MV)[3:] == ["", ""]


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_loads_table_switches_units(app):
    from kipi.dc.gui.pi_panel import PIPanel
    from kipi.dc.pi.tree import solve_tree
    from phase_boards import board, two_phase
    setup = two_phase()
    for ld in setup.loads:
        ld.tol_min, ld.tol_max = -5.0, 5.0
    snap = board()
    tree = solve_tree(snap, setup)
    panel = PIPanel()
    panel.snapshot = snap
    panel.show_results(tree, next(iter(tree.groups)))
    t = panel.tbl_loads
    load = tree.loads[0]
    headers = lambda: [t.horizontalHeaderItem(c).text() for c in range(3, 8)]  # noqa: E731
    assert panel.vunit() == MV and headers() == [
        "Mean (mV)", "Lowest pin (mV)", "Highest pin (mV)", "Tolerance (mV)", "Margin (mV)"]
    for unit in (MV, PCT, VOLTS):
        panel.cmb_vunit.setCurrentIndex(panel.cmb_vunit.findData(unit))
        assert [t.item(0, c).text() for c in range(3, 8)] == vunits.load_cells(load, unit)
        assert headers()[0] == f"Mean ({unit})"
    assert t.item(0, 8).text() == load.verdict == "PASS"
    panel.close()
