import pytest

pv = pytest.importorskip("pyvista")

from kipi.core.geometry.copper import net_copper
from kipi.core.viz.meshes import build_scene, triangulate
from synthetic import F, IN1, make_board


def test_triangulation_preserves_area():
    snap = make_board()
    g = net_copper(snap, "GND")[IN1]
    pts, tris = triangulate(g)
    a = pts[tris]
    area = 0.5 * abs((a[:, 1, 0] - a[:, 0, 0]) * (a[:, 2, 1] - a[:, 0, 1])
                     - (a[:, 2, 0] - a[:, 0, 0]) * (a[:, 1, 1] - a[:, 0, 1])).sum()
    assert area == pytest.approx(g.area, rel=1e-9)


def test_scene_builds_closed_slabs():
    snap = make_board()
    copper = {n: net_copper(snap, n) for n in ("+3V3", "GND")}
    scene = build_scene(snap, copper)
    assert {n: len(ns.copper) for n, ns in scene.nets.items()} == {"+3V3": 4, "GND": 2}
    cu = next(lm for lm in scene.nets["+3V3"].copper if lm.layer_id == F)
    layer = snap.stackup.layer(F)
    assert cu.mesh.volume == pytest.approx(cu.area_mm2 * layer.thickness_mm, rel=1e-6)
    assert cu.mesh.bounds[4] == pytest.approx(layer.z_bottom_mm)
    assert scene.nets["+3V3"].vias is not None and scene.nets["+3V3"].vias.n_cells > 0
    assert scene.nets["GND"].vias is None         # GND has no vias
    assert set(scene.nets["+3V3"].pads) == {F}
    assert scene.board is not None


def test_board_body_clears_every_copper_face():
    """A body face coplanar with copper z-fights with it: speckle that moves as the view
    rotates (issue #36)."""
    from kipi.core.viz.meshes import board_body
    snap = make_board()
    body, _ = board_body(snap)
    z_lo, z_hi = body.bounds[4], body.bounds[5]
    faces = [z for c in snap.stackup.copper for z in (c.z_bottom_mm, c.z_top_mm)]
    assert min(abs(z - zf) for zf in faces for z in (z_lo, z_hi)) > 1e-3
    outer = sorted(snap.stackup.copper, key=lambda c: c.z_bottom_mm)
    assert outer[0].z_top_mm < z_lo and z_hi < outer[-1].z_bottom_mm
