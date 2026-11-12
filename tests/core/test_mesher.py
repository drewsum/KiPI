"""Mesher robustness: inputs that used to hang or crash Triangle."""

import locale
import math

import numpy as np
import pytest
from shapely.geometry import LineString, box

import kipi.core.mesh.mesher as M

MAX_AREA = math.sqrt(3) / 4 * 0.5 ** 2


def min_angle(pts, tris):
    p = pts[tris]
    out = []
    for a, b, c in ((0, 1, 2), (1, 2, 0), (2, 0, 1)):
        u, v = p[:, b] - p[:, a], p[:, c] - p[:, a]
        cosang = (u * v).sum(1) / np.linalg.norm(u, axis=1) / np.linalg.norm(v, axis=1)
        out.append(np.degrees(np.arccos(np.clip(cosang, -1, 1))))
    return float(np.min(out))


def test_decimal_comma_locale_does_not_break_triangle(comma_locale):
    """Under a decimal comma Triangle read the maximum area as 0 and failed (#236)."""
    pts, tris = M.mesh_polygon(box(0, 0, 10, 4), [], 0.01, 28.5)
    assert locale.localeconv()["decimal_point"] == "."
    assert len(tris) > 4000 and min_angle(pts, tris) > 28


@pytest.mark.parametrize("gap", [1e-3, 2e-4, 5e-5, 0.0])
def test_pad_edge_running_along_copper_edge_does_not_hang(gap):
    """A pad outline a hair inside the copper edge used to send Triangle's quality
    refinement into a near-endless loop (Audio_Visualiser_DSP, +12Vin, J201)."""
    copper = box(0, 0, 10, 4)
    pad = box(0.5, gap, 2.0, 4 - gap)
    pts, tris = M.mesh_polygon(copper, [pad.boundary], MAX_AREA, 28.0)
    assert 0 < len(tris) < 5000
    assert min_angle(pts, tris) > 27.0
    # The pad's inner edge still exists as mesh edges right across the copper.
    on_edge = np.isclose(pts[:, 0], 2.0) & (pts[:, 1] > -1e-9) & (pts[:, 1] < 4 + 1e-9)
    assert pts[on_edge, 1].min() == pytest.approx(0.0) and pts[on_edge, 1].max() == pytest.approx(4.0)


def test_linework_is_always_clean_for_triangle():
    """Noding with float noise used to leave vertices just off the segments they split;
    Triangle segfaults on that. Snap-rounded noding must give a clean PSLG."""
    rng = np.random.default_rng(1)
    lines = [box(0, 0, 10, 10).exterior]
    for _ in range(60):
        x0, y0 = rng.uniform(0.5, 9.5, 2)
        ang = rng.uniform(0, math.pi)
        d = np.array([math.cos(ang), math.sin(ang)]) * rng.uniform(0.5, 6)
        # nearly coincident and nearly parallel lines with sub-nm noise
        for eps in (0.0, 3e-7, 7e-8):
            lines.append(LineString([(x0 + eps, y0), (x0 + d[0], y0 + d[1] + eps)]))
    verts, segs = M._linework(lines)
    assert M.pslg_problems(verts, segs) == 0
    # ...and Triangle accepts it (a malformed PSLG would crash the process here).
    out = M._triangulate({"vertices": verts, "segments": segs}, "pQ", cap=200_000)
    assert len(out["triangles"]) > 0
    # Quality refinement of sub-nm slivers cannot finish; the cap turns that into an
    # exception the mesher can recover from instead of a hang.
    with pytest.raises(M.MeshCapHit):
        M._triangulate({"vertices": verts, "segments": segs}, "pq28Q", cap=20_000)


def test_pslg_check_catches_unnoded_crossing():
    verts = np.array([[0, 0], [2, 2], [0, 2], [2, 0]], float)
    segs = np.array([[0, 1], [2, 3]])                 # an X with no shared middle vertex
    assert M.pslg_problems(verts, segs) > 0


def test_zero_area_hole_ring_does_not_void_the_polygon():
    """A fill ring with no area cannot seed a hole. Used to lose the whole polygon:
    Triangle found no region at the seed, carved away everything and returned no
    triangles, so every pad on that plane read as DISCONNECTED (D-Wing AIO, B.Cu GND)."""
    outer = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    real_hole = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
    # collinear ring: four coordinates, two of them duplicates -> zero area
    flat_hole = [(8.0, 8.0), (8.5, 8.5), (8.5, 8.5), (8.0, 8.0)]
    poly = M.Polygon(outer, [real_hole, flat_hole])

    pts, tris = M.mesh_polygon(poly, [], MAX_AREA, 28.0)
    assert len(tris) > 0, "degenerate hole ring voided the polygon"
    # The real hole is still cut out; the degenerate one encloses nothing to cut.
    p = pts[tris]
    meshed_area = float(np.abs(
        (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
        - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])).sum() / 2)
    expected = 10 * 10 - 2 * 2
    assert meshed_area == pytest.approx(expected, abs=0.2)


def test_hole_seed_that_voids_the_domain_falls_back_to_carving(monkeypatch):
    """If a seed still leaves Triangle nothing (one in the copper eats it all), the
    mesher meshes without seeds and cuts the holes out, rather than return an empty
    mesh or one with its holes filled."""
    outer = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    poly = M.Polygon(outer, [[(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]])
    monkeypatch.setattr(M, "_hole_seeds", lambda _p, _w=None: [M.Point(1, 1), M.Point(5, 5)])
    notes = []
    pts, tris = M.mesh_polygon(poly, [], MAX_AREA, 28.0, warn=notes.append)
    assert len(tris) > 0 and notes
    assert len(np.unique(tris)) == len(pts)     # no vertices left in the hole
    p = pts[tris]
    meshed_area = float(np.abs(
        (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
        - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])).sum() / 2)
    assert meshed_area == pytest.approx(10 * 10 - 2 * 2, abs=0.2)


def test_steiner_cap_stops_runaway_refinement():
    data = {"vertices": np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float),
            "segments": np.array([[0, 1], [1, 2], [2, 3], [3, 0]])}
    with pytest.raises(M.MeshCapHit):
        M._triangulate(data, "pq28a0.0001Q", cap=50)


def test_a_pad_outline_is_clipped_the_same_with_the_edge_band_built_once():
    """The polygon's edge band is built once for all its pad outlines (a plane with
    thousands of holes rebuilt it per pad: minutes); the clipped outline is the same, and
    an outline whose box misses the polygon changes nothing."""
    from shapely.geometry import Point, box
    from kipi.core.mesh.mesher import CONSTRAINT_CLEARANCE_MM, _clip_constraint, mesh_polygon
    plane = box(0, 0, 20, 10).difference(Point(15, 5).buffer(1.0))
    pad = box(2, 2, 4, 3).boundary
    edge = plane.boundary
    band = edge.buffer(CONSTRAINT_CLEARANCE_MM, quad_segs=2)
    assert _clip_constraint(pad, plane).equals(_clip_constraint(pad, plane, edge, band))
    far = box(50, 50, 51, 51).boundary
    a = mesh_polygon(plane, [pad], 0.5, 28.0)
    b = mesh_polygon(plane, [pad, far], 0.5, 28.0)
    assert len(a[1]) == len(b[1]) and len(a[0]) == len(b[0])


def test_a_slit_in_the_copper_does_not_empty_the_mesh():
    """A hole ring that encloses no area (a slit a zone fill or the simplification
    leaves) put its seed in the copper, and Triangle then removed every triangle around
    it: pads on a 6-layer board's ground pour were cut off from it (2026-11-03)."""
    from shapely.geometry import Polygon
    slit = [(2.0, 2.0), (2.0, 2.04), (2.0 + 1e-9, 2.02), (2.0, 2.0)]
    hole = [(4.0, 4.0), (5.0, 4.0), (5.0, 5.0), (4.0, 5.0)]
    copper = Polygon([(0, 0), (6, 0), (6, 6), (0, 6)], [slit, hole])
    pts, tris = M.mesh_polygon(copper, [], MAX_AREA, 28.0)
    p = pts[tris]
    area = 0.5 * np.abs((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
                        - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])).sum()
    assert area == pytest.approx(copper.area, rel=1e-6)    # the real hole stays a hole


def test_meshed_without_pad_outlines_each_pad_still_holds_a_node(monkeypatch):
    """When the pad outlines will not mesh the polygon is meshed without them; a pad
    smaller than the mesh then held no node and its terminal was lost ("Pad U75.1 on
    net VCC has no meshed copper": a 10-layer board with the coarse mesh). One node
    is placed inside each pad on the polygon, and none for a pad off it."""
    from shapely.geometry import Point
    plane = box(0, 0, 30, 30)
    pads = [box(10.1, 10.1, 10.4, 10.4), box(21.3, 4.6, 21.5, 4.9)]
    off = box(40, 40, 40.3, 40.3)
    coarse = math.sqrt(3) / 4 * 1.5 ** 2
    # Without the fix's nodes the coarse mesh misses both pads.
    pts, _ = M.mesh_polygon(plane, [], coarse, 28.0)
    assert not any(p.contains(Point(x, y)) for p in pads for x, y in pts)

    real = M._triangulate
    calls = {"n": 0}

    def fail_first(data, opts, cap):
        calls["n"] += 1
        if calls["n"] == 1:
            raise M.MeshCapHit()        # the pass with the pad outlines does not converge
        return real(data, opts, cap)

    monkeypatch.setattr(M, "_triangulate", fail_first)
    notes = []
    pts, tris = M.mesh_polygon(plane, [p.boundary for p in pads + [off]], coarse, 28.0,
                               warn=notes.append)
    assert notes and "meshing without them" in notes[0]
    assert len(tris) > 0
    for p in pads:
        assert sum(p.contains(Point(x, y)) for x, y in pts) >= 1
    assert all(-1e-9 <= x <= 30 + 1e-9 and -1e-9 <= y <= 30 + 1e-9 for x, y in pts)


def test_a_pad_node_stays_clear_of_the_copper_edge():
    """A pad overlapping the copper's edge gets its node inside the copper, clear of
    the edge, so it never sits on a boundary segment."""
    from shapely.geometry import Point
    plane = box(0, 0, 10, 10)
    pad = box(9.8, 4, 10.3, 4.5)
    (x, y), = M._pad_nodes(plane, [pad.boundary])
    assert plane.contains(Point(x, y)) and 10 - x >= M.CONSTRAINT_CLEARANCE_MM - 1e-9
    assert len(M._pad_nodes(plane, [box(20, 20, 21, 21).boundary])) == 0
