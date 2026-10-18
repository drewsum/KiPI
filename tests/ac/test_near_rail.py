"""The AC mesh is graded only at plated holes near the rail (#123)."""

import os

import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.model import CapModel
from kipi.ac.solver import solve_ac
from kipi.ac.solver.near_rail import margin_mm, near_rail
from kipi.core.mesh.mesher import mesh_net
from kipi.core.model.board import Via
from synthetic import IN1, rect

RAIL = rect(0, 0, 20, 15)                  # +3V3 in one corner of a 70 x 40 mm ground
GROUND = rect(0, 0, 70, 40)
FAR_VIAS = [(x, y) for x in range(35, 70, 5) for y in range(5, 40, 5)]


def _board():
    snap = plane_pair(probe_xy=(3, 12), caps=[("C1", "10u", (14, 3))], rail=RAIL,
                      ground=GROUND)
    snap.vias += [Via(f"s{i}", "GND", xy, 0.3, 0.6, [IN1]) for i, xy in enumerate(FAR_VIAS)]
    snap.invalidate_index()
    return snap


def _terminals(snap):
    return [p.id for p in snap.pads]


def test_holes_near_the_rail_or_its_parts_are_near():
    snap = _board()
    near = near_rail(snap, ["+3V3"], _terminals(snap), 2)
    m = margin_mm(snap)
    assert m == pytest.approx(max(5.0, 5 * snap.stackup.board_thickness_mm))
    pts = np.array([(10, 10), (20 + m - 0.1, 5), (20 + m + 0.1, 5), (60, 30)])
    assert near(pts).tolist() == [True, True, False, False]
    assert near(np.zeros((0, 2))).tolist() == []


def test_far_holes_are_not_graded_but_still_meshed():
    snap = _board()
    kw = dict(max_edge_mm=2.0, min_edge_mm=0.1, grading=0.3)
    whole = mesh_net(snap, "GND", ["U1.2", "C1.2"], **kw)
    near = mesh_net(snap, "GND", ["U1.2", "C1.2"],
                    graded=near_rail(snap, ["+3V3"], _terminals(snap), 2), **kw)
    assert near.n_nodes < 0.6 * whole.n_nodes
    rings = {b.owner_id for b in near.barrels}
    assert {f"s{i}" for i in range(len(FAR_VIAS))} <= rings     # every via still joined
    lo, hi = near.points.min(axis=0), near.points.max(axis=0)
    assert np.allclose(lo, [0, 0]) and np.allclose(hi, [70, 40])     # all the copper kept


def test_grading_only_near_the_rail_leaves_the_impedance_alone():
    snap = _board()
    out = {}
    for only_near in (False, True):
        setup = plane_setup(snap, max_edge_mm=2.0, min_edge_mm=0.1, grading=0.3,
                            points_per_decade=20, field_points_per_decade=5,
                            grade_near_rail_only=only_near)
        setup.caps = [CapModel("10u", 10e-6, 0.5e-9, 0.005, ["C1"])]
        out[only_near] = solve_ac(snap, setup, "VRM1", "GND")
    assert out[True].stats.n_nodes < 0.6 * out[False].stats.n_nodes
    a, b = out[False].probes[0].z, out[True].probes[0].z
    assert np.max(np.abs(b - a) / np.abs(a)) < 1e-4            # 2e-6 when written


def test_the_checkbox_is_ticked_by_default_and_for_older_setups():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.ac.gui.panel import ACPanel
    from kipi.dc.pi.model import PISetup
    QApplication.instance() or QApplication([])
    snap = _board()
    older = PISetup.from_dict({"ac": {"max_edge_mm": 1.0}})     # saved before #123
    assert older.ac.grade_near_rail_only
    p = ACPanel()
    assert p.chk_near.isChecked()                               # a new panel
    p.set_board(snap, older, "board")
    assert p.chk_near.isChecked()
    p.chk_near.setChecked(False)                                # unticking is kept
    assert not p.setup.ac.grade_near_rail_only
    assert not PISetup.from_dict(p.setup.to_dict()).ac.grade_near_rail_only
    p.close()
