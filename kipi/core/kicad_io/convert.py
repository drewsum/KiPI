"""KiCad API geometry (nanometer vectors, polylines with arcs) to KiPI millimeter tuples
and :class:`~kipi.core.model.board.Poly`. Shared by every reader of kipy items."""

from __future__ import annotations

import math
from typing import List

from kipi.core.model.board import Poly

NM_PER_MM = 1_000_000.0
ARC_SEGMENTS_PER_CIRCLE = 64


def mm(v) -> float:
    return float(v) / NM_PER_MM


def pt(vec) -> tuple:
    return (mm(vec.x), mm(vec.y))


def arc_points(start, mid, end, include_start: bool = False) -> List[tuple]:
    """Discretize a start/mid/end arc (mm tuples) into points, excluding start."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return ([start] if include_start else []) + [end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1)
          + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3)
          + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def ccw_delta(a, b):
        return (b - a) % (2 * math.pi)

    # Sweep in whichever direction passes through the mid point.
    sweep = ccw_delta(a1, a3)
    if ccw_delta(a1, a2) > sweep:
        sweep -= 2 * math.pi
    n = max(2, int(math.ceil(abs(sweep) / (2 * math.pi) * ARC_SEGMENTS_PER_CIRCLE)))
    pts = [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n))
           for i in range(0 if include_start else 1, n)]
    pts.append(end)
    return pts


def polyline_points(polyline) -> List[tuple]:
    pts: List[tuple] = []
    for node in polyline.nodes:
        if node.has_point:
            pts.append(pt(node.point))
        elif node.has_arc:
            arc = node.arc
            s, m, e = pt(arc.start), pt(arc.mid), pt(arc.end)
            if not pts or pts[-1] != s:
                pts.append(s)
            pts.extend(arc_points(s, m, e))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def poly(pwh) -> Poly:
    return Poly(outline=polyline_points(pwh.outline),
                holes=[polyline_points(h) for h in pwh.holes])


def shapely_to_polys(geom) -> List[Poly]:
    """The polygons of a shapely geometry (Polygon or a collection) as KiPI polys."""
    if geom is None or geom.is_empty:
        return []
    return [Poly(outline=list(g.exterior.coords)[:-1],
                 holes=[list(r.coords)[:-1] for r in g.interiors])
            for g in getattr(geom, "geoms", [geom])
            if g.geom_type == "Polygon" and not g.is_empty]
