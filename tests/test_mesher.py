"""Mesher robustness: inputs that used to hang or crash Triangle."""

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


def test_steiner_cap_stops_runaway_refinement():
    data = {"vertices": np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float),
            "segments": np.array([[0, 1], [1, 2], [2, 3], [3, 0]])}
    with pytest.raises(M.MeshCapHit):
        M._triangulate(data, "pq28a0.0001Q", cap=50)
