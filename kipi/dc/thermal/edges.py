"""Picking stretches of the board edge for edge boundaries (#221): the board's edges as
closed rings, and the run of a ring between two points on it or the straight side through
one. Distances along a ring are mm from its first point, as shapely's ``project`` gives
them."""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np
import shapely
from shapely.ops import substring

from kipi.core.model.board import BoardSnapshot
from kipi.dc.thermal.model import board_geometry

TURN_DEG = 10.0             # a side ends where the edge turns by more than this


def edge_rings(snap: BoardSnapshot) -> List[shapely.LineString]:
    """Every edge of the board, outer edges and cutouts, as closed lines; longest first."""
    board = board_geometry(snap)
    polys = getattr(board, "geoms", [board])
    rings = [shapely.LineString(r.coords) for p in polys if p.geom_type == "Polygon"
             for r in (p.exterior, *p.interiors) if r.length > 0]
    return sorted(rings, key=lambda r: -r.length)


def nearest_ring(rings, xy) -> Tuple[int, float]:
    """(index, distance in mm) of the ring nearest ``xy``; (-1, inf) without rings."""
    if not rings:
        return -1, math.inf
    pt = shapely.Point(xy)
    d = [r.distance(pt) for r in rings]
    k = int(np.argmin(d))
    return k, float(d[k])


def _points(line) -> List[List[float]]:
    return [[round(x, 4), round(y, 4)] for x, y in np.asarray(line.coords)[:, :2]]


def forward(ring, a: float, b: float) -> List[List[float]]:
    """The ring's points from ``a`` to ``b`` along its direction, past its start if
    ``b`` < ``a``."""
    if b >= a:
        return _points(substring(ring, a, b))
    head, tail = _points(substring(ring, a, ring.length)), _points(substring(ring, 0, b))
    return head + tail[1:]


def span(ring, a: float, b: float) -> List[List[float]]:
    """The shorter way along the ring between ``a`` and ``b``."""
    lo, hi = min(a, b), max(a, b)
    return forward(ring, lo, hi) if hi - lo <= ring.length / 2 else forward(ring, hi, lo)


def side_at(ring, s: float, turn_deg: float = TURN_DEG) -> List[List[float]]:
    """The straight side of the ring through ``s``: the segment there, run on through each
    neighbour that turns by less than ``turn_deg`` (a whole ring that never turns more)."""
    c = np.asarray(ring.coords)[:, :2]
    seg = np.diff(c, axis=0)
    keep = np.hypot(*seg.T) > 1e-9
    c = np.vstack([c[:-1][keep], c[-1:]])
    seg = np.diff(c, axis=0)
    n = len(seg)
    if n == 0:
        return _points(ring)
    ang = np.arctan2(seg[:, 1], seg[:, 0])
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(*seg.T))])
    i = int(np.clip(np.searchsorted(cum, s, side="right") - 1, 0, n - 1))

    def turn(j):                       # between segment j and the next
        d = ang[(j + 1) % n] - ang[j]
        return abs((d + math.pi) % (2 * math.pi) - math.pi) > math.radians(turn_deg)

    first, last = i, i
    for _ in range(n - 1):
        if turn(last):
            break
        last = (last + 1) % n
    else:
        return _points(ring)
    for _ in range(n - 1):
        if turn((first - 1) % n):
            break
        first = (first - 1) % n
    return forward(ring, float(cum[first]), float(cum[last + 1]))
