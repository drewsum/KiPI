"""Nets joined by series parts are solved as one problem."""

import pytest
from shapely.geometry import box

from kipi.core.mesh.mesher import mesh_nets
from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Zone,
)
from kipi.dc.solver import (
    CurrentLoad, NetProblem, SeriesLink, SolveError, SolveSettings, VoltageSource, run,
)
from kipi.dc.solver.fem import LinearSystem
from kipi.dc.solver.setup import RHO_CU_OHM_MM as RHO

F, B = 3, 34
T = 0.035


def poly(g):
    return Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])


def pad(pid, net, ref, num, g):
    return Pad(pid, net, ref, num, (g.centroid.x, g.centroid.y), "smd", shapes={F: [poly(g)]})


def board():
    """Net A: strip x 0..10; net B: strip x 12..22 (W = 1 mm). R1 bridges the gap."""
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [pad("src", "A", "J1", "1", box(0, 0, 1, 1)),
            pad("r1a", "A", "R1", "1", box(9, 0, 10, 1)),
            pad("r1b", "B", "R1", "2", box(12, 0, 13, 1)),
            pad("ld", "B", "U1", "1", box(21, 0, 22, 1))]
    zones = [Zone("za", "A", "", 0, {F: [poly(box(0, 0, 10, 1))]}),
             Zone("zb", "B", "", 0, {F: [poly(box(12, 0, 22, 1))]})]
    return BoardSnapshot("series", "", st, [Net(1, "A"), Net(2, "B")], [], [], pads, [],
                         zones, [])


def strip_r(length):
    return RHO * length / (1.0 * T)


def problem(r_series, current=2.0):
    return NetProblem("A + B", [VoltageSource("VRM", ["src"], 1.0)],
                      [CurrentLoad("U1", ["ld"], current)], SolveSettings(max_edge_mm=0.3),
                      series=[SeriesLink("R1", ["r1a"], ["r1b"], r_series)], nets=["A", "B"])


def test_series_resistor_joins_nets():
    res = run(board(), problem(0.02))
    expected = 2.0 * (strip_r(8) + 0.02 + strip_r(8))     # pads are equipotential
    assert 1.0 - res.fields.terminals[1].v_mean == pytest.approx(expected, rel=1e-9)
    (sr,) = res.fields.series
    assert sr.current == pytest.approx(2.0, rel=1e-9)
    assert sr.power == pytest.approx(2.0 ** 2 * 0.02, rel=1e-9)
    assert res.fields.summary["energy_balance_error"] < 1e-9


def test_series_diode_applies_forward_drop_and_counts_its_loss():
    p = problem(0.02)
    p.series[0].voltage_drop = 0.5
    res = run(board(), p)
    expected = 2.0 * (strip_r(8) + 0.02 + strip_r(8)) + 0.5
    assert 1.0 - res.fields.terminals[1].v_mean == pytest.approx(expected, rel=1e-9)
    (sr,) = res.fields.series
    assert sr.current == pytest.approx(2.0, rel=1e-9)
    assert sr.power == pytest.approx(2.0 ** 2 * 0.02 + 2.0 * 0.5, rel=1e-9)
    assert res.fields.summary["energy_balance_error"] < 1e-9


def test_zero_ohm_part_is_a_short():
    res = run(board(), problem(0.0))
    expected = 2.0 * (strip_r(8) + strip_r(8))
    assert 1.0 - res.fields.terminals[1].v_mean == pytest.approx(expected, rel=1e-9)
    assert res.fields.series[0].power == 0.0


def test_without_the_series_part_the_load_is_unreachable():
    p = problem(0.02)
    p.series = []
    with pytest.raises(SolveError, match="DC connections"):
        run(board(), p)


def test_linear_system_reuse_matches_fresh_solves():
    snap = board()
    p = problem(0.05)
    mesh = mesh_nets(snap, p.nets, p.terminal_pad_ids(), max_edge_mm=0.3)
    system = LinearSystem(snap, mesh, p)
    for amps, volts in [(1.0, 1.0), (3.0, 1.0), (3.0, 1.2)]:
        reused = system.solve({"VRM": volts}, {"U1": amps})
        p2 = problem(0.05, current=amps)
        p2.sources[0].voltage = volts
        fresh = LinearSystem(snap, mesh, p2).solve()
        u = p.loads[0].pad_ids[0]
        node = mesh.pad_nodes[u][F][0]
        assert reused.node_v[node] == pytest.approx(fresh.node_v[node], abs=1e-12)
        expected = volts - amps * (strip_r(8) + 0.05 + strip_r(8))
        assert reused.node_v[node] == pytest.approx(expected, rel=1e-9)
