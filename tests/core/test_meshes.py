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


# --- how copper is triangulated (#202) ---------------------------------------------------

def _area(pts, tris):
    a = pts[tris]
    return 0.5 * abs((a[:, 1, 0] - a[:, 0, 0]) * (a[:, 2, 1] - a[:, 0, 1])
                     - (a[:, 2, 0] - a[:, 0, 0]) * (a[:, 1, 1] - a[:, 0, 1])).sum()


def _plane(n):
    """A plane with n x n round antipads, and an island apart from it."""
    import shapely
    from shapely.geometry import Point, box
    holes = [Point(i + 0.5, j + 0.5).buffer(0.2, quad_segs=4) for i in range(n) for j in range(n)]
    return shapely.union_all([box(0, 0, n, n).difference(shapely.union_all(holes)),
                              box(n + 1, 0, n + 2, 1)])


def test_triangle_meshes_a_plane_without_adding_points():
    from kipi.core.viz import meshes
    pytest.importorskip("triangle")
    g = _plane(12)
    got = meshes._triangle_fast(g)
    assert got is not None
    pts, tris = got
    assert len(pts) == len(meshes._outline(g)[0])          # no points added
    assert _area(pts, tris) == pytest.approx(g.area, rel=1e-12)


def test_rings_that_touch_are_checked_before_triangle(monkeypatch):
    """A hole touching the outline at a vertex of both is safe for Triangle; one touching
    the middle of an outline edge is left to GEOS."""
    from shapely.geometry import Polygon
    from kipi.core.viz import meshes
    pytest.importorskip("triangle")
    at_vertex = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(0, 0), (1, 2), (2, 1)]])
    mid_edge = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(2, 0), (3, 1), (1, 1)]])
    for poly, safe in ((at_vertex, True), (mid_edge, False)):
        assert poly.is_valid
        assert meshes._safe_for_triangle(poly, *meshes._outline(poly)) is safe
        pts, tris = meshes.triangulate(poly)
        assert _area(pts, tris) == pytest.approx(poly.area, rel=1e-9)


def test_without_triangle_geos_meshes_the_same(monkeypatch):
    from kipi.core.viz import meshes
    g = _plane(6)
    fast = meshes.triangulate(g)
    monkeypatch.setattr(meshes, "_triangle", None)
    slow = meshes.triangulate(g)
    assert _area(*slow) == pytest.approx(_area(*fast), rel=1e-6)
    a = meshes.slab_from_triangles(*fast, 0.0, 0.035)[0]
    b = meshes.slab_from_triangles(*slow, 0.0, 0.035)[0]
    assert a.volume == pytest.approx(b.volume, rel=1e-6)
    assert a.n_open_edges == b.n_open_edges == 0
