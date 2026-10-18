"""Solver validation against closed-form results."""

import math

import numpy as np
import pytest
from shapely.geometry import Point, box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Via, Zone,
)
from kipi.dc.solver import CurrentLoad, NetProblem, SolveError, SolveSettings, VoltageSource, run
from kipi.dc.solver.setup import RHO_CU_OHM_MM as RHO
from kipi.dc.solver.via_barrel import ViaRule

F, B = 3, 34
T_CU = 0.035
DIEL = 1.5


def poly(g):
    return Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])


def two_layer(zones, pads, vias=()):
    copper = [CopperLayer(F, "F.Cu", 0, T_CU, DIEL + T_CU),
              CopperLayer(B, "B.Cu", 1, T_CU, 0.0)]
    st = Stackup(copper, [DielectricLayer(DIEL, "FR4", 4.5, F, B)], DIEL + 2 * T_CU)
    return BoardSnapshot("analytic", "", st, [Net(1, "N")], [], list(vias), pads, [],
                         [Zone(f"z{i}", "N", "", 0, {l: [poly(g)]}) for i, (l, g) in
                          enumerate(zones)], [])


def pad(pid, ref, layer, g):
    return Pad(pid, "N", ref, "1", (g.centroid.x, g.centroid.y), "smd", shapes={layer: [poly(g)]})


def test_straight_strip_matches_rho_l_over_wt():
    L, W = 20.0, 1.0
    snap = two_layer([(F, box(0, -W / 2, L, W / 2))],
                     [pad("src", "U1", F, box(0, -W / 2, 1, W / 2)),
                      pad("ld", "U2", F, box(L - 1, -W / 2, L, W / 2))])
    prob = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                      [CurrentLoad("LOAD", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3))
    res = run(snap, prob)
    expected = RHO * (L - 2) / (W * T_CU) * 2.0          # pads are equipotential
    (src, load) = res.fields.terminals
    assert 1.0 - load.v_mean == pytest.approx(expected, rel=1e-9)
    assert src.current == pytest.approx(2.0, rel=1e-9)
    assert res.fields.summary["energy_balance_error"] < 1e-7
    # Uniform current density away from the pads: I / (W t)
    assert res.fields.summary["j_max"] == pytest.approx(2.0 / (W * T_CU), rel=1e-6)


def test_radial_disc_through_via_matches_log_and_barrel_formulas():
    R_OUT, R_PAD, DRILL, TP = 10.0, 9.0, 0.3, 0.025
    disc = Point(0, 0).buffer(R_OUT, quad_segs=64)
    ring = disc.difference(Point(0, 0).buffer(R_PAD, quad_segs=64))
    via = Via("v", "N", (0.0, 0.0), DRILL, 0.6, [F, B])
    snap = two_layer([(F, disc), (B, disc)],
                     [pad("src", "J1", F, ring), pad("ld", "J2", B, ring)], [via])
    settings = SolveSettings(max_edge_mm=0.4, circle_quad_segs=16,
                             via_rules=[ViaRule("any", TP)])
    prob = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                      [CurrentLoad("LOAD", ["ld"], 1.0)], settings)
    res = run(snap, prob)

    r_spread = RHO / (2 * math.pi * T_CU) * math.log(R_PAD / (DRILL / 2))
    dz = DIEL + T_CU
    r_barrel = RHO * dz / (math.pi * (DRILL * TP - TP * TP))
    v_load = res.fields.terminals[1].v_mean
    assert 1.0 - v_load == pytest.approx(2 * r_spread + r_barrel, rel=2e-3)

    (el,) = res.solution.barrel_elements
    v_top = res.solution.v_merged[el.node_a]
    v_bot = res.solution.v_merged[el.node_b]
    assert v_top - v_bot == pytest.approx(r_barrel, rel=1e-7)         # exact resistor
    assert 1.0 - v_top == pytest.approx(r_spread, rel=2e-3)            # FEM spreading
    assert res.fields.vias[0].current == pytest.approx(1.0, rel=1e-7)
    assert res.fields.summary["energy_balance_error"] < 1e-7



def test_layer_resistivity_override_scales_that_layer_only():
    """A strip on F.Cu at 1.5x rho drops 1.5x as much; current density is unchanged."""
    L, W = 20.0, 1.0
    snap = two_layer([(F, box(0, -W / 2, L, W / 2))],
                     [pad("src", "U1", F, box(0, -W / 2, 1, W / 2)),
                      pad("ld", "U2", F, box(L - 1, -W / 2, L, W / 2))])

    def solve(**kw):
        prob = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                          [CurrentLoad("LOAD", ["ld"], 2.0)],
                          SolveSettings(max_edge_mm=0.3, **kw))
        return run(snap, prob).fields
    base = solve()
    hot = solve(rho_by_layer={"F.Cu": 1.5 * RHO})
    other = solve(rho_by_layer={"B.Cu": 1.5 * RHO})       # no copper there: no effect
    drop = lambda f: 1.0 - f.terminals[1].v_mean
    assert drop(hot) == pytest.approx(1.5 * RHO * (L - 2) / (W * T_CU) * 2.0, rel=1e-9)
    assert drop(other) == pytest.approx(drop(base), rel=1e-12)
    assert hot.summary["j_max"] == pytest.approx(2.0 / (W * T_CU), rel=1e-6)
    assert hot.summary["energy_balance_error"] < 1e-7


def test_layer_resistivity_override_leaves_via_barrels_on_the_default():
    R_OUT, R_PAD, DRILL, TP = 10.0, 9.0, 0.3, 0.025
    disc = Point(0, 0).buffer(R_OUT, quad_segs=64)
    ring = disc.difference(Point(0, 0).buffer(R_PAD, quad_segs=64))
    via = Via("v", "N", (0.0, 0.0), DRILL, 0.6, [F, B])
    snap = two_layer([(F, disc), (B, disc)],
                     [pad("src", "J1", F, ring), pad("ld", "J2", B, ring)], [via])
    settings = SolveSettings(max_edge_mm=0.4, circle_quad_segs=16, via_rules=[ViaRule("any", TP)],
                             rho_by_layer={"B.Cu": 2 * RHO})
    res = run(snap, NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                               [CurrentLoad("LOAD", ["ld"], 1.0)], settings))
    r_spread = RHO / (2 * math.pi * T_CU) * math.log(R_PAD / (DRILL / 2))
    r_barrel = RHO * (DIEL + T_CU) / (math.pi * (DRILL * TP - TP * TP))
    assert 1.0 - res.fields.terminals[1].v_mean == pytest.approx(
        3 * r_spread + r_barrel, rel=2e-3)                 # F.Cu 1x, B.Cu 2x, barrel 1x
    assert res.solution.layer_rho == {F: RHO, B: 2 * RHO}
    assert res.fields.summary["energy_balance_error"] < 1e-7

def test_floating_island_is_reported_and_load_on_it_is_an_error():
    snap = two_layer([(F, box(0, 0, 10, 1)), (F, box(0, 5, 10, 6))],
                     [pad("src", "U1", F, box(0, 0, 1, 1)),
                      pad("ld", "U2", F, box(9, 0, 10, 1)),
                      pad("iso", "U3", F, box(9, 5, 10, 6))])
    ok = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)], [CurrentLoad("L", ["ld"], 1.0)])
    res = run(snap, ok)
    assert any("not connected to a source" in w for w in res.solution.warnings)
    bad = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)], [CurrentLoad("L", ["iso"], 1.0)])
    with pytest.raises(SolveError, match="no copper path"):
        run(snap, bad)


def test_multi_pad_load_splits_current_and_amg_path_agrees(monkeypatch):
    snap = two_layer([(F, box(0, 0, 30, 10))],
                     [pad("src", "U1", F, box(0, 4, 1, 6)),
                      pad("a", "U2", F, box(29, 0, 30, 1)), pad("b", "U2", F, box(29, 9, 30, 10))])
    prob = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                      [CurrentLoad("L", ["a", "b"], 3.0)], SolveSettings(max_edge_mm=0.25))
    direct = run(snap, prob)
    va, vb = direct.fields.terminals[1].v_pads
    assert va == pytest.approx(vb, rel=1e-6)            # mirror symmetry (mesh is not exactly)
    import kipi.core.solver.spd as spd
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS", 0)
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO", 0)
    amg = run(snap, prob)
    assert amg.solution.stats["method"] == "amg-cg"
    assert amg.fields.terminals[1].v_mean == pytest.approx(direct.fields.terminals[1].v_mean,
                                                           abs=1e-9)
    assert amg.fields.summary["energy_balance_error"] < 1e-6


def test_plated_hole_shorts_its_ring_even_when_other_layers_are_dead_copper():
    """Current flowing past a PTH hole: the barrel wall ties the hole edge together.

    The pad's B.Cu ring is isolated (dead) copper and gets skipped; the F.Cu ring must
    still be treated as equipotential, so the result must not depend on whether the
    dead ring exists. Adding a stub on B.Cu (still a dead end) must not change anything.
    """
    from kipi.core.model.board import Track

    hole = Point(5, 0).buffer(0.8, quad_segs=16)
    pth = Pad("pth", "N", "J1", "1", (5.0, 0.0), "pth", drill_mm=(1.0, 1.0),
              shapes={F: [poly(hole)], B: [poly(hole)]})

    def solve(with_stub):
        snap = two_layer([(F, box(0, -2, 10, 2))],
                         [pad("src", "U1", F, box(0, -2, 1, 2)),
                          pad("ld", "U2", F, box(9, -2, 10, 2)), pth])
        if with_stub:
            snap.tracks.append(Track("N", B, 0.3, (5.0, 0.0), (7.0, 0.0)))
            snap.invalidate_index()
        prob = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                          [CurrentLoad("L", ["ld"], 1.0)], SolveSettings(max_edge_mm=0.2))
        return run(snap, prob)

    plain, stub = solve(False), solve(True)
    assert len(plain.mesh.barrels) == 1 and plain.mesh.barrels[0].layers == [F]
    assert len(stub.mesh.barrels[0].layers) == 2
    drop = lambda r: 1.0 - r.fields.terminals[1].v_mean
    assert drop(plain) == pytest.approx(drop(stub), rel=1e-6)
    # A shorted hole conducts better than an open one of the same size.
    open_hole = two_layer([(F, box(0, -2, 10, 2).difference(Point(5, 0).buffer(0.5, quad_segs=16)))],
                          [pad("src", "U1", F, box(0, -2, 1, 2)), pad("ld", "U2", F, box(9, -2, 10, 2))])
    r_open = run(open_hole, NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                                       [CurrentLoad("L", ["ld"], 1.0)], SolveSettings(max_edge_mm=0.2)))
    assert drop(plain) < drop(r_open)


def test_result_scene_carries_fields_and_probes():
    from kipi.dc.viz.results import build_result_scene

    L, W = 20.0, 1.0
    snap = two_layer([(F, box(0, -W / 2, L, W / 2))],
                     [pad("src", "U1", F, box(0, -W / 2, 1, W / 2)),
                      pad("ld", "U2", F, box(L - 1, -W / 2, L, W / 2))])
    res = run(snap, NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                               [CurrentLoad("LOAD", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3)))
    scene = build_result_scene(snap, res)
    lr = scene.layers[F]
    assert lr.mesh.n_cells > 0 and lr.mesh.point_data["drop_mV"].max() > 0
    # Linear drop along the strip: halfway between the pads is half the total drop.
    total = (1.0 - res.fields.terminals[1].v_mean) * 1e3
    mid = scene.probe(F, L / 2, 0.1)
    assert mid["drop_mV"] == pytest.approx(total / 2, rel=1e-6)
    assert mid["J"] == pytest.approx(2.0 / (W * T_CU), rel=1e-6)
    assert scene.probe(F, L / 2, 5.0) is None                      # off the copper
    lo, hi = scene.ranges["voltage"]
    assert hi == pytest.approx(1.0) and lo == pytest.approx(1.0 - total / 1e3, rel=1e-9)


def test_current_arrows_follow_the_current():
    from kipi.dc.viz.results import build_result_scene
    L, W = 20.0, 1.0
    snap = two_layer([(F, box(0, -W / 2, L, W / 2))],
                     [pad("src", "U1", F, box(0, -W / 2, 1, W / 2)),
                      pad("ld", "U2", F, box(L - 1, -W / 2, L, W / 2))])
    res = run(snap, NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                               [CurrentLoad("LOAD", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3)))
    lr = build_result_scene(snap, res).layers[F]
    assert len(lr.arrow_pts) > 10
    # Current flows from the source (x=0) to the load (x=20): +x in KiCad and display.
    assert (lr.arrow_dir[:, 0] > 0.99).all()
    # Away from the pads |J| is I / (W t).
    mid = (lr.arrow_pts[:, 0] > 5) & (lr.arrow_pts[:, 0] < 15)
    assert lr.arrow_mag[mid] == pytest.approx(2.0 / (W * T_CU), rel=1e-6)


def test_current_arrows_drawn_on_both_copper_faces():
    from kipi.dc.viz.results import ARROW_LIFT, build_result_scene
    L, W = 20.0, 1.0
    snap = two_layer([(F, box(0, -W / 2, L, W / 2))],
                     [pad("src", "U1", F, box(0, -W / 2, 1, W / 2)),
                      pad("ld", "U2", F, box(L - 1, -W / 2, L, W / 2))])
    res = run(snap, NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                               [CurrentLoad("LOAD", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3)))
    lr = build_result_scene(snap, res).layers[F]
    cu = snap.stackup.layer(F)
    assert lr.arrow_pts[:, 2] == pytest.approx(cu.z_top_mm + ARROW_LIFT)
    assert lr.arrow_z_below == pytest.approx(cu.z_bottom_mm - ARROW_LIFT)


def test_flat_arrows_shape():
    from kipi.dc.viz.results import (
        ARROW_HEAD_HALF_WIDTH, ARROW_HEAD_LENGTH, flat_arrows,
    )
    pts = np.array([[1.0, 2.0, 0.0], [5.0, 5.0, 0.0]])
    dirs = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
    arrows = flat_arrows(pts, dirs, np.array([2.0, 1.0]), [0.1, -0.2])
    # Three triangles per arrow per z level, all flat at their level.
    assert arrows.n_cells == 2 * 2 * 3
    assert arrows.is_all_triangles
    assert sorted(set(np.round(arrows.points[:, 2], 9))) == [-0.2, 0.1]
    first = arrows.points[:7]
    # Centered on its point, from x = 0 to x = 2, head as wide as specified.
    assert first[:, 0].min() == pytest.approx(0.0) and first[:, 0].max() == pytest.approx(2.0)
    assert first[:, 1].max() - first[:, 1].min() == pytest.approx(2 * ARROW_HEAD_HALF_WIDTH * 2.0)
    assert first[5, :2] == pytest.approx([2.0, 2.0])                      # the tip
    assert first[4, 0] == pytest.approx(2.0 - ARROW_HEAD_LENGTH * 2.0)    # the neck
    # The second arrow points along -y: its tip is the lowest point.
    second = arrows.points[7:14]
    assert second[5, :2] == pytest.approx([5.0, 4.5])
    assert flat_arrows(np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0), [0.0]).n_points == 0


# ---------------------------------------------------------------- via rules
def _barrel_drop(via, rules):
    """Voltage across the one barrel of a two-layer disc fed on F and loaded on B."""
    disc = Point(0, 0).buffer(10.0, quad_segs=64)
    ring = disc.difference(Point(0, 0).buffer(9.0, quad_segs=64))
    snap = two_layer([(F, disc), (B, disc)],
                     [pad("src", "J1", F, ring), pad("ld", "J2", B, ring)], [via])
    settings = SolveSettings(max_edge_mm=0.4, circle_quad_segs=16, via_rules=rules)
    prob = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                      [CurrentLoad("LOAD", ["ld"], 1.0)], settings)
    res = run(snap, prob)
    (el,) = res.solution.barrel_elements
    return res.solution.v_merged[el.node_a] - res.solution.v_merged[el.node_b], res


def test_copper_filled_via_conducts_through_the_whole_hole():
    drill, dz = 0.3, DIEL + T_CU
    via = Via("v", "N", (0.0, 0.0), drill, 0.6, [F, B], filled=True)
    drop, _ = _barrel_drop(via, [ViaRule("any", 0.025, fill="copper")])
    assert drop == pytest.approx(RHO * dz / (math.pi * drill * drill / 4), rel=1e-7)


def test_epoxy_filled_via_conducts_through_its_wall_only():
    drill, tp, dz = 0.3, 0.025, DIEL + T_CU
    via = Via("v", "N", (0.0, 0.0), drill, 0.6, [F, B], filled=True)
    drop, _ = _barrel_drop(via, [ViaRule("any", tp)])
    assert drop == pytest.approx(RHO * dz / (math.pi * (drill * tp - tp * tp)), rel=1e-7)


def test_conductive_fill_adds_its_core_at_its_resistivity():
    drill, tp, dz, rho_fill = 0.3, 0.025, DIEL + T_CU, 100 * RHO
    via = Via("v", "N", (0.0, 0.0), drill, 0.6, [F, B], filled=True)
    drop, _ = _barrel_drop(via, [ViaRule("any", tp, fill="conductive", fill_rho_ohm_mm=rho_fill)])
    wall = math.pi * (drill * tp - tp * tp)
    core = math.pi * drill * drill / 4 - wall
    assert drop == pytest.approx(RHO * dz / (wall + core / 100), rel=1e-7)


def test_fill_not_stated_is_solved_unfilled_and_said_so():
    drill, tp, dz = 0.3, 0.025, DIEL + T_CU
    via = Via("v", "N", (0.0, 0.0), drill, 0.6, [F, B], filled=None)
    drop, res = _barrel_drop(via, [ViaRule("any", tp, fill="copper")])
    assert drop == pytest.approx(RHO * dz / (math.pi * (drill * tp - tp * tp)), rel=1e-7)
    assert any("does not say whether they are filled" in w for w in res.solution.warnings)


def test_first_rule_covering_type_and_drill_sets_the_plating():
    drill, dz = 0.3, DIEL + T_CU
    via = Via("v", "N", (0.0, 0.0), drill, 0.6, [F, B], via_type="through")
    rules = [ViaRule("micro", 0.012), ViaRule("through", 0.030, drill_max_mm=0.25),
             ViaRule("through", 0.018, drill_min_mm=0.25, drill_max_mm=0.5)]
    drop, _ = _barrel_drop(via, rules)
    assert drop == pytest.approx(RHO * dz / (math.pi * (drill * 0.018 - 0.018 ** 2)), rel=1e-7)


def test_hole_no_rule_covers_is_an_error():
    via = Via("v", "N", (0.0, 0.0), 0.3, 0.6, [F, B], via_type="through")
    with pytest.raises(SolveError, match="No via rule"):
        _barrel_drop(via, [ViaRule("micro", 0.012)])


def test_pad_diameter_per_layer():
    v = Via("v", "N", (0.0, 0.0), 0.3, 0.6, [F, B])
    assert v.pad_diameter(F) == 0.6 and v.pad_diameter(B) == 0.6       # older snapshots
    v.pad_mm = {F: 0.6}                                                # no pad on B
    assert v.pad_diameter(F) == 0.6 and v.pad_diameter(B) == 0.0
