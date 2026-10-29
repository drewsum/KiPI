"""The sort-based stand-ins for np.unique, and point-in-copper tests in threads (#216):
exactly what the slow ways give."""

import numpy as np
import pytest
import shapely

from kipi.core import sorting
from kipi.core.geometry import copper


def _grid_tris(nx=30, ny=20, holes=True):
    """A triangulated grid, with some triangles taken out to make inner boundaries."""
    idx = np.arange((nx + 1) * (ny + 1)).reshape(ny + 1, nx + 1)
    a, b = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
    c, d = idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    tris = np.concatenate([np.stack([a, b, d], 1), np.stack([a, d, c], 1)])
    if holes:
        keep = np.random.default_rng(1).random(len(tris)) > 0.1
        tris = tris[keep]
    return tris, (nx + 1) * (ny + 1)


def _old_boundary(tris):
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    e.sort(axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    return np.unique(uniq[counts == 1].ravel())


@pytest.mark.parametrize("holes", [False, True])
def test_boundary_nodes_match_unique_rows(holes):
    tris, n = _grid_tris(holes=holes)
    assert np.array_equal(sorting.boundary_nodes(tris, n), _old_boundary(tris))


def test_boundary_nodes_of_nothing():
    assert len(sorting.boundary_nodes(np.zeros((0, 3), dtype=np.int64), 0)) == 0


def test_single_mask_matches_counts():
    keys = np.random.default_rng(2).integers(0, 500, 2000)
    _, inv, counts = np.unique(keys, return_inverse=True, return_counts=True)
    assert np.array_equal(sorting.single_mask(keys), counts[inv.ravel()] == 1)


def _entries(n=300, m=20_000):
    rng = np.random.default_rng(3)
    return rng.integers(0, n, m), rng.integers(0, n, m), n


def test_pattern_matches_unique():
    r, c, n = _entries()
    uniq, inv = np.unique(r.astype(np.int64) * n + c, return_inverse=True)
    u2, i2 = sorting.pattern(r, c, n)
    assert np.array_equal(uniq, u2) and np.array_equal(inv.ravel(), i2)


def test_pattern_falls_back_to_unique(monkeypatch):
    from scipy.sparse import _sparsetools

    def broken(*a):
        raise RuntimeError("no such routine")
    monkeypatch.setattr(_sparsetools, "coo_tocsr", broken)
    r, c, n = _entries()
    uniq, inv = np.unique(r.astype(np.int64) * n + c, return_inverse=True)
    u2, i2 = sorting.pattern(r, c, n)
    assert np.array_equal(uniq, u2) and np.array_equal(inv.ravel(), i2)


def test_points_in_copper_in_threads_match_one(monkeypatch):
    monkeypatch.setattr(copper, "THREADED_POINTS", 1000)
    monkeypatch.setattr("kipi.core.cores.cores", lambda: 8)
    rng = np.random.default_rng(4)
    g = shapely.union_all(shapely.buffer(shapely.points(rng.uniform(0, 50, (200, 2))), 2.0))
    shapely.prepare(g)
    xy = rng.uniform(0, 50, (20_000, 2))
    assert np.array_equal(copper.contains_xy(g, xy[:, 0], xy[:, 1]),
                          shapely.contains_xy(g, xy[:, 0], xy[:, 1]))
