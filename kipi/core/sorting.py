"""Sort-based stand-ins for ``np.unique`` on the large integer keys meshes produce: edges
of triangles and entries of sparse matrices (#216). They give exactly what ``np.unique``
would, several times faster: one sort of 1-D keys in place of a sort of rows
(``axis=0``), and a bucket sort by row in place of one sort of all the entries."""

from __future__ import annotations

from typing import Tuple

import numpy as np


def edge_keys(tris: np.ndarray, n: int) -> Tuple[np.ndarray, np.ndarray]:
    """Each triangle's three edges (i->j, j->k, k->i, all of one before the next) and one
    int64 key per edge whatever its direction, for ``n`` nodes."""
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    lo = np.minimum(e[:, 0], e[:, 1]).astype(np.int64)
    hi = np.maximum(e[:, 0], e[:, 1]).astype(np.int64)
    return e, lo * n + hi


def _single(sorted_keys: np.ndarray) -> np.ndarray:
    """For sorted keys: which appear only once."""
    differs = np.ones(len(sorted_keys) + 1, dtype=bool)
    np.not_equal(sorted_keys[1:], sorted_keys[:-1], out=differs[1:-1])
    return differs[:-1] & differs[1:]


def boundary_nodes(tris: np.ndarray, n: int) -> np.ndarray:
    """The nodes, sorted, on edges that belong to only one triangle."""
    if len(tris) == 0:
        return np.zeros(0, dtype=np.int64)
    _, key = edge_keys(tris, n)
    key.sort()
    once = key[_single(key)]
    return np.unique(np.concatenate([once // n, once % n]))


def single_mask(keys: np.ndarray) -> np.ndarray:
    """Per key: True where no other key equals it."""
    if len(keys) == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(keys)
    out = np.empty(len(keys), dtype=bool)
    out[order] = _single(keys[order])
    return out


def pattern(rows: np.ndarray, cols: np.ndarray, n: int) -> Tuple[np.ndarray, np.ndarray]:
    """``np.unique(rows * n + cols, return_inverse=True)`` for the entries of an n x n
    sparse matrix: the sorted distinct keys and, per entry, the index of its key. The
    entries are bucketed by row (scipy's COO to CSR conversion, linear in their number)
    and only sorted within each row; where that private scipy routine is missing or
    fails, ``np.unique`` itself."""
    rows = np.asarray(rows, dtype=np.int64)
    cols = np.asarray(cols, dtype=np.int64)
    m = len(rows)
    try:
        if m == 0 or m >= 2**53:          # entry numbers ride in float64 data
            raise ValueError("nothing to bucket")
        import scipy.sparse as sp
        from scipy.sparse import _sparsetools
        indptr = np.empty(n + 1, dtype=np.int64)
        indices = np.empty(m, dtype=np.int64)
        which = np.empty(m, dtype=np.float64)
        _sparsetools.coo_tocsr(n, n, m, rows, cols, np.arange(m, dtype=np.float64),
                               indptr, indices, which)
        A = sp.csr_matrix((which, indices, indptr), shape=(n, n))
        A.has_sorted_indices = False
        A.sort_indices()                      # within rows; duplicates stay apart
        perm = A.data.astype(np.int64)
        if A.nnz != m:
            raise ValueError("entries lost")
    except Exception:                         # noqa: BLE001 - any trouble: the slow way
        uniq, inv = np.unique(rows * n + cols, return_inverse=True)
        return uniq, inv.ravel()
    key = rows[perm] * n + cols[perm]
    new = np.empty(m, dtype=bool)
    new[0] = True
    np.not_equal(key[1:], key[:-1], out=new[1:])
    inv = np.empty(m, dtype=np.int64)
    inv[perm] = np.cumsum(new) - 1
    return key[new], inv
