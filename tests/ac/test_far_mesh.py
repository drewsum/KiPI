"""The AC mesh coarsens in ground far from the rail (#228): larger triangles, small holes
filled, dead-end islands left out, and every piece of copper that can carry current kept."""

import os
import pickle

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from ac_boards import plane_pair, plane_setup
from kipi.ac.model import CapModel
from kipi.ac.solver import solve_ac
from kipi.ac.solver.near_rail import near_rail
from kipi.core.mesh.mesher import MeshOptions, mesh_net, trim_far_holes
from kipi.core.model.board import Poly, Via, Zone
from synthetic import B, F, IN1, rect, square

RAIL = rect(0, 0, 20, 15)                  # +3V3 in one corner of a 70 x 40 mm ground
GROUND = rect(0, 0, 70, 40)
NEAR_ANTIPAD = (24, 8)
FAR_ANTIPADS = [(x, y) for x in range(32, 70, 6) for y in (6, 20, 34)]
STITCH = [(24, 5), (40, 12), (50, 26), (62, 14), (40, 30)]       # In1 to B.Cu
DEAD_END = (43, 37)                        # an F.Cu pour on one via: no current through it
BRIDGE = [(56, 37), (64, 37)]              # an F.Cu strip on two vias: kept
KW = dict(max_edge_mm=2.0, min_edge_mm=0.1, grading=0.3)


def _board():
    snap = plane_pair(probe_xy=(3, 12), caps=[("C1", "10u", (14, 3))], rail=RAIL,
                      ground=GROUND)
    holes = [square(x, y, 0.8) for x, y in [NEAR_ANTIPAD, *FAR_ANTIPADS]]
    snap.zones = [z for z in snap.zones if z.net != "GND"] + [
        Zone("z2", "GND", "", 0, filled={IN1: [Poly(GROUND, holes)]}),
        Zone("z3", "GND", "", 0, filled={B: [Poly(GROUND, [rect(52, 28, 58, 33)])]}),
        Zone("z4", "GND", "", 0, filled={F: [Poly(rect(55, 36, 65, 38))]}),
        Zone("z5", "GND", "", 0, filled={F: [Poly(rect(42, 35.5, 48, 38.5))]}),
    ]
    snap.vias += [Via(f"s{i}", "GND", xy, 0.3, 0.6, [IN1, B]) for i, xy in enumerate(STITCH)]
    snap.vias += [Via("dead", "GND", DEAD_END, 0.3, 0.6, [F, IN1])]
    snap.vias += [Via(f"b{i}", "GND", xy, 0.3, 0.6, [F, IN1]) for i, xy in enumerate(BRIDGE)]
    snap.invalidate_index()
    return snap


def _terminals(snap):
    return [p.id for p in snap.pads if p.footprint in ("U1", "C1")]


def _gnd_mesh(snap, coarse):
    zone = near_rail(snap, ["+3V3"], _terminals(snap), 2)
    gnd = [t for t in _terminals(snap) if t.endswith(".2")]
    return mesh_net(snap, "GND", gnd, graded=zone, far=zone if coarse else None,
                    far_max_edge_mm=6.0 if coarse else None, **KW)


def _nodes_near(mesh, layer, xy, r):
    on = mesh.node_layer == layer
    return int((np.hypot(*(mesh.points[on] - xy).T) < r).sum())


def test_far_ground_is_coarser_and_keeps_what_carries_current():
    snap = _board()
    fine, coarse = _gnd_mesh(snap, False), _gnd_mesh(snap, True)
    assert coarse.n_nodes < 0.6 * fine.n_nodes                    # 0.55 when written
    lo, hi = coarse.points.min(axis=0), coarse.points.max(axis=0)
    assert np.allclose(lo, [0, 0]) and np.allclose(hi, [70, 40])     # all the copper kept
    bars = {b.owner_id: b for b in coarse.barrels}
    for i, _ in enumerate(STITCH):                  # the stitching still joins the planes
        assert set(bars[f"s{i}"].ring_nodes) == {IN1, B}
    far_ring = bars["s3"].ring_nodes                # far: its hole filled, joined at the center
    assert all(len(n) == 1 for n in far_ring.values())
    assert np.allclose(coarse.points[far_ring[IN1][0]], STITCH[3])
    assert len(bars["s0"].ring_nodes[IN1]) > 1      # near: a ring of nodes, as before
    # The dead-end pad is left out, the strip on two vias kept.
    assert _nodes_near(fine, F, (45, 37), 1) and not _nodes_near(coarse, F, (45, 37), 3)
    assert set(bars["dead"].ring_nodes) == {IN1}
    assert set(bars["b0"].ring_nodes) == {F, IN1} and _nodes_near(coarse, F, (60, 37), 3)


def test_far_holes_are_filled_or_outlined_coarsely():
    snap = _board()
    zone = near_rail(snap, ["+3V3"], _terminals(snap), 2)
    arc = Point(50, 20).buffer(2.0, quad_segs=64).exterior          # a large, finely traced hole
    poly = Polygon(rect(0, 0, 70, 40), [square(*NEAR_ANTIPAD, 0.8), square(40, 6, 0.8),
                                        square(62, 14, 0.4), arc])
    own = np.array([[62, 14, 0.15], [24, 5, 0.15]])
    out, pts = trim_far_holes(poly, zone, MeshOptions(**KW, far_max_edge_mm=6.0), own)
    kept = sorted(out.interiors, key=lambda r: Polygon(r).area)
    assert len(kept) == 2                                           # the near one, the arc
    assert Polygon(kept[0]).equals(Polygon(square(*NEAR_ANTIPAD, 0.8)))
    assert 4 <= len(kept[1].coords) < len(arc.coords) / 4
    assert Polygon(kept[1]).symmetric_difference(Polygon(arc)).area < 0.5     # of 12.6 mm²
    assert pts.tolist() == [[62, 14]]                               # the far drill, filled
    # Without a far zone in its options the mesher leaves holes alone.
    assert mesh_net(snap, "GND", [], far=zone, **KW).n_nodes == mesh_net(snap, "GND", [],
                                                                         **KW).n_nodes


def test_the_zone_pickles_for_the_mesh_workers():
    snap = _board()
    zone = near_rail(snap, ["+3V3"], _terminals(snap), 2)
    xy = np.array([(10, 10), (30, 20), (69, 39)])
    back = pickle.loads(pickle.dumps(zone))
    assert np.allclose(back.distance(xy), zone.distance(xy))
    assert zone.distance(xy)[0] == 0 and back(xy).tolist() == [True, False, False]


def test_coarsening_far_leaves_the_impedance_alone():
    snap = _board()
    out = {}
    for coarse in (False, True):
        setup = plane_setup(snap, points_per_decade=20, field_points_per_decade=5,
                            coarsen_far=coarse, far_max_edge_mm=6.0, **KW)
        setup.caps = [CapModel("10u", 10e-6, 0.5e-9, 0.005, ["C1"])]
        out[coarse] = solve_ac(snap, setup, "VRM1", "GND")
    assert out[True].stats.n_nodes < 0.6 * out[False].stats.n_nodes
    a, b = out[False].probes[0].z, out[True].probes[0].z
    # 2.5e-3 when written, at the top of the sweep, on this coarse a mesh (0.05 % on a
    # 10-layer board at the default 1 mm).
    assert np.max(np.abs(b - a) / np.abs(a)) < 5e-3


def test_the_checkbox_is_ticked_by_default_and_for_older_setups():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.ac.gui.panel import ACPanel
    from kipi.dc.pi.model import PISetup
    QApplication.instance() or QApplication([])
    older = PISetup.from_dict({"ac": {"max_edge_mm": 1.0}})     # saved before #228
    assert older.ac.coarsen_far and older.ac.far_max_edge_mm == 5.0
    p = ACPanel()
    p.set_board(_board(), older)
    assert p.chk_far.isChecked() and p.spin_far_edge.isEnabled()
    p.chk_far.setChecked(False)
    p.spin_far_edge.setValue(8.0)
    assert not p.spin_far_edge.isEnabled()
    back = PISetup.from_dict(p.setup.to_dict()).ac
    assert not back.coarsen_far and back.far_max_edge_mm == 8.0
    p.close()
