"""Sources that share copper (#219): offboard supplies on one connector share its ground
pins, which every one of them holds at 0 V."""

import pytest
from shapely.geometry import box

from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver import CurrentLoad, NetProblem, SolveSettings, VoltageSource, run
from kipi.dc.solver.fem import LinearSystem, SolveError
from kipi.dc.solver.setup import RHO_CU_OHM_MM as RHO
from tests.dc.test_solver import F, T_CU, pad, two_layer

from tree_boards import R, board, by_name

L, W = 20.0, 1.0


def _strip():
    return two_layer([(F, box(0, -W / 2, L, W / 2))],
                     [pad("src", "J1", F, box(0, -W / 2, 1, W / 2)),
                      pad("ld", "U2", F, box(L - 1, -W / 2, L, W / 2))])


def _problem(v1=1.0, v2=1.0):
    return NetProblem("N", [VoltageSource("A", ["src"], v1), VoltageSource("B", ["src"], v2)],
                      [CurrentLoad("LOAD", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3))


def test_sources_on_the_same_pads_solve_as_one():
    res = run(_strip(), _problem())
    drop = 1.0 - res.fields.terminals[2].v_mean
    assert drop == pytest.approx(RHO * (L - 2) / (W * T_CU) * 2.0, rel=1e-3)
    # The 2 A through the shared pad is split evenly, and counted once in all.
    i = res.solution.source_current
    assert i["A"] == pytest.approx(1.0, rel=1e-9)
    assert i["B"] == pytest.approx(1.0, rel=1e-9)
    assert res.fields.summary["kcl_error"] < 1e-9


def test_weights_split_the_shared_current():
    snap, prob = _strip(), _problem()
    res = run(snap, prob)
    system = LinearSystem(snap, res.mesh, prob)
    sol = system.solve(source_weights={"A": 3.0, "B": 1.0})
    assert sol.source_current["A"] == pytest.approx(1.5, rel=1e-9)
    assert sol.source_current["B"] == pytest.approx(0.5, rel=1e-9)
    # All weights zero: even again.
    sol = system.solve(source_weights={"A": 0.0, "B": 0.0})
    assert sol.source_current["A"] == pytest.approx(1.0, rel=1e-9)


def test_sources_sharing_copper_at_different_voltages_is_an_error():
    with pytest.raises(SolveError, match="different voltages"):
        run(_strip(), _problem(1.0, 1.2))


def test_offboard_supplies_sharing_ground_pins():
    """J1 brings in 12 V and J2 3.3 V, both returning over J1.2; U2 draws 1 A from J2."""
    s = PISetup([Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source",
                     ground=PinRef("J1", "GND", ["2"])),
                 Vrm("J2", PinRef("U1", "+3V3", ["2"]), 3.3, "source",
                     ground=PinRef("J1", "GND", ["2"]))],
                [Load("U2", PinRef("U2", "+3V3", ["1"]), 1.0,
                      ground=PinRef("U2", "GND", ["2"]), tol_min=-1.0, tol_max=1.0)],
                [], SolveSettings(max_edge_mm=0.3))
    res = solve_tree(board(), s)
    ld = by_name(res.loads, "U2")
    assert ld.v_ground == pytest.approx(1.0 * R(20), rel=1e-6)
    # The return goes back to J2, whose output it is; J1 delivers nothing.
    gnd = next(t for gr in res.groups.values() if gr.kind == "ground"
               for t in gr.result.fields.terminals if t.kind == "source" and "J2" in t.name)
    assert abs(gnd.current) == pytest.approx(1.0, rel=1e-6)
