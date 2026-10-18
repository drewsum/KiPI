"""Loads not connected by copper (#88): before solving, each load pin is checked for a path
through copper or series links to its VRM. Unconnected pins draw nothing; a load with no
supply pin (or no ground pin) connected is left out and shown as disconnected; a rail with
no load connected at all stops the solve."""

import math

import pytest
from shapely.geometry import box

from kipi.dc.pi.model import Load, PinGroup, PinRef
from kipi.dc.pi.results import DISCONNECTED
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver.fem import SolveError
from tree_boards import B, board, by_name, pad, setup


def island_board():
    """The tree board plus copper islands: U2.3 and U3.1 on +3V3 and U3.2 on GND, away
    from every zone."""
    snap = board()
    snap.pads += [pad("u2_3", "+3V3", "U2", "3", box(25, 0, 26, 1)),
                  pad("u3_1", "+3V3", "U3", "1", box(30, 0, 31, 1)),
                  pad("u3_2", "GND", "U3", "2", box(30, 0, 31, 1), B)]
    return snap


def _notes(tree, load):
    return [w for w in tree.warnings if w.startswith(f"Load {load}:")]


def test_a_disconnected_load_is_left_out_and_marked():
    base = solve_tree(island_board(), setup())
    s = setup()
    s.loads.append(Load("U3", PinRef("U3", "+3V3", ["1"]), 2.0, tol_min=-1.0, tol_max=1.0))
    tree = solve_tree(island_board(), s)
    u3 = by_name(tree.loads, "U3")
    assert u3.status == DISCONNECTED and u3.verdict == DISCONNECTED and u3.passed is None
    assert u3.current == 0.0 and math.isnan(u3.v_effective)
    # U2 and the VRM see exactly what they would without U3.
    assert by_name(tree.loads, "U2").v_effective == pytest.approx(
        by_name(base.loads, "U2").v_effective, abs=1e-12)
    assert by_name(tree.vrms, "U1").i_out == pytest.approx(by_name(base.vrms, "U1").i_out)
    (note,) = _notes(tree, "U3")
    assert "supply pin(s) U3 1 on +3V3" in note and "U1" in note
    assert "2 A is left out" in note and DISCONNECTED in note


def test_unconnected_pins_hand_their_current_to_the_others():
    base = solve_tree(island_board(), setup())
    s = setup()
    s.loads[0].supply.pins = ["1", "3"]               # U2.3 sits on an island
    tree = solve_tree(island_board(), s)
    u2 = by_name(tree.loads, "U2")
    assert not u2.status and u2.current == 1.0
    assert u2.v_effective == pytest.approx(by_name(base.loads, "U2").v_effective, abs=1e-12)
    (note,) = _notes(tree, "U2")
    assert "U2 3" in note and "drawn through its other pins" in note


def test_a_pin_group_with_no_pin_connected_draws_nothing():
    s = setup()
    s.loads[0].supply.pins = ["1", "3"]
    s.loads[0].groups = [PinGroup(["1"], 1.0, "core"), PinGroup(["3"], 0.5, "io")]
    tree = solve_tree(island_board(), s)
    u2 = by_name(tree.loads, "U2")
    assert u2.current == pytest.approx(1.0)
    core, io = u2.pin_groups
    assert core[1] == 1.0 and not math.isnan(core[2])
    assert io[1] == 0.0 and math.isnan(io[2])


def test_a_load_whose_ground_is_unconnected_is_left_out():
    s = setup(ground=True)
    s.loads.append(Load("U3", PinRef("U2", "+3V3", ["1"]), 2.0,
                        ground=PinRef("U3", "GND", ["2"])))
    base = solve_tree(island_board(), setup(ground=True))
    tree = solve_tree(island_board(), s)
    assert by_name(tree.loads, "U3").status == DISCONNECTED
    assert by_name(tree.vrms, "U1").i_out == pytest.approx(by_name(base.vrms, "U1").i_out)
    (note,) = _notes(tree, "U3")
    assert "ground pin(s) U3 2 on GND" in note and "ground reference" in note


def test_a_rail_with_no_load_connected_stops_the_solve():
    s = setup()
    s.loads[0] = Load("U3", PinRef("U3", "+3V3", ["1"]), 1.0)
    with pytest.raises(SolveError, match="no load on this rail is connected to U1"):
        solve_tree(island_board(), s)


def test_reports_show_a_disconnected_load(tmp_path):
    import csv
    from kipi.dc.pi.report import export_csv
    from kipi.dc.pi.results import fmt_drop
    snap = island_board()
    s = setup()
    s.loads.append(Load("U3", PinRef("U3", "+3V3", ["1"]), 2.0, tol_min=-1.0, tol_max=1.0))
    tree = solve_tree(snap, s)
    export_csv(tree, snap, tmp_path)
    with open(tmp_path / "kipi_loads.csv", encoding="utf-8") as fh:
        row = next(r for r in csv.DictReader(fh) if r["load"] == "U3")
    assert row["result"] == DISCONNECTED and row["current_A"] == "0"
    assert row["v_effective_V"] == "" and row["mean_is"] == ""
    assert fmt_drop(math.nan) == ""


@pytest.fixture(scope="module")
def qapp():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_results_table_shows_a_disconnected_load(qapp):
    from kipi.dc.gui.pi_panel import PIPanel
    s = setup()
    s.loads.append(Load("U3", PinRef("U3", "+3V3", ["1"]), 2.0, tol_min=-1.0, tol_max=1.0))
    snap = island_board()
    tree = solve_tree(snap, s)
    panel = PIPanel()
    panel.set_board(snap, s, "test")
    panel.show_results(tree, next(iter(tree.groups)))
    t = panel.tbl_loads
    row = next(r for r in range(t.rowCount()) if t.item(r, 0).text() == "U3")
    assert t.item(row, 8).text() == DISCONNECTED and t.item(row, 3).text() == ""
    assert "U3 1 on +3V3" in panel.txt_notes.toPlainText()


def test_unconnected_pins_add_no_load_heat():
    from kipi.dc.thermal.cosim import load_powers, solve_electrothermal
    from kipi.dc.thermal.model import ThermalSettings
    snap = island_board()
    s = setup(ground=True)
    s.loads[0].supply.pins = ["1", "3"]
    s.loads.append(Load("U3", PinRef("U3", "+3V3", ["1"]), 2.0))
    tree = solve_tree(snap, s)
    powers = load_powers(tree, snap, s)
    watts, pts = powers["U2"]
    assert math.isfinite(watts) and watts > 0 and len(pts) == 2      # U2.1 and its ground
    assert powers["U3"][0] == 0.0 and len(powers["U3"][1]) == 0
    th = ThermalSettings(enabled=True, iterations=1, grid_mm=0.5, load_power=True)
    hot = solve_electrothermal(snap, s, th).thermal
    assert math.isfinite(hot.hottest()[2])
