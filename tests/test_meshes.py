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
