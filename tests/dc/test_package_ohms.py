# Copyright (c) 2026 Tomachie LLC.
"""A load's balls joined through their package resistances (CurrentLoad.pad_ohms), and each
pad's own resistance to the source (LinearSystem.pad_resistance)."""

import pytest
from shapely.geometry import box

from kipi.core.mesh.mesher import mesh_nets
from kipi.dc.solver import CurrentLoad, NetProblem, SolveSettings, VoltageSource, run
from kipi.dc.solver.fem import LinearSystem
from kipi.dc.solver.setup import RHO_CU_OHM_MM
from test_pins import T, _pad, _strip

SQ = RHO_CU_OHM_MM / T          # ohms per square of the strip's copper


def _board():
    """A 10 x 1 mm strip: the source at its left end, two balls of U2 1 and 5 squares
    further on (pads span the strip, so the copper between them is whole squares)."""
    return _strip([_pad("src", "A", "U1", "1", box(0, 0, 1, 1)),
                   _pad("b1", "A", "U2", "1", box(2, 0, 3, 1)),
                   _pad("b2", "A", "U2", "2", box(8, 0, 9, 1))])


def _problem(pad_ohms=None, tie=True):
    s = SolveSettings(max_edge_mm=0.3)
    s.tie_load_pads = tie
    return NetProblem("A", [VoltageSource("U1", ["src"], 1.0)],
                      [CurrentLoad("U2", ["b1", "b2"], 1.0, pad_ohms=pad_ohms)], s)


def test_the_ideal_tie_gives_the_far_ball_nothing():
    res = run(_board(), _problem())
    t = res.fields.terminals[1]
    assert t.i_pads[0] == pytest.approx(1.0, rel=1e-6)
    assert t.i_pads[1] == pytest.approx(0.0, abs=1e-9)
    assert t.v_die is None


def test_package_resistance_shares_the_current_by_both_paths():
    """Each ball r = 5 squares to the die: the far ball's path is 5 squares of copper plus
    r, the near ball's r, so they carry 1/3 and 2/3."""
    r = 5 * SQ
    res = run(_board(), _problem([r, r]))
    t = res.fields.terminals[1]
    assert t.i_pads == pytest.approx([2 / 3, 1 / 3], rel=1e-6)
    assert sum(t.i_pads) == pytest.approx(1.0, rel=1e-9)
    assert t.i_solved
    # The die sits below the near ball by its own path's drop.
    assert t.v_pads[0] - t.v_die == pytest.approx(2 / 3 * r, rel=1e-6)
    assert t.v_pads[0] == pytest.approx(1.0 - 1.0 * SQ, rel=1e-9)
    # The copper's delivered power still balances its loss; the package's is not copper.
    assert res.fields.summary["energy_balance_error"] < 1e-6


def test_package_resistances_must_be_one_per_pad_and_above_zero():
    with pytest.raises(Exception, match="2 pads"):
        run(_board(), _problem([0.01]))
    with pytest.raises(Exception, match="above 0 ohm"):
        run(_board(), _problem([0.01, 0.0]))


def test_each_pads_own_resistance_to_the_source():
    """Untied, each ball's driving-point resistance is its own copper: 1 and 6 squares."""
    snap = _board()
    prob = _problem(tie=False)
    mesh = mesh_nets(snap, prob.nets, prob.terminal_pad_ids(), name=prob.net,
                     max_edge_mm=prob.settings.max_edge_mm)
    system = LinearSystem(snap, mesh, prob)
    r = system.pad_resistance(["b1", "b2", "src"])
    assert r["b1"] == pytest.approx(1 * SQ, rel=1e-6)
    assert r["b2"] == pytest.approx(6 * SQ, rel=1e-6)
    assert r["src"] == 0.0
