"""Copper drawn as graphic shapes: segments, arcs, circles, rectangles, polygons and
beziers on a copper layer, on the board or inside a footprint.

KiCad conducts through a copper graphic shape that carries a net, so it is copper of
that net like a track or a zone fill. Each shape becomes a :class:`Zone` holding its
copper polygons; the rest of KiPI merges zone fills with tracks and pads per net and
layer, so no other code needs to know where the copper came from.

A copper shape with no net conducts nothing KiPI can attribute; those are counted and
reported, not guessed onto a neighboring pad's net.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from kipi.core.kicad_io.convert import arc_points, mm, poly, pt, shapely_to_polys
from kipi.core.model.board import Zone
from kipi.core.progress import ProgressReporter

BEZIER_SEGMENTS = 32
SHAPE_QUAD_SEGS = 8          # stroke end caps and circles: 32 segments per circle


def _bezier_points(p0, p1, p2, p3) -> List[tuple]:
    pts = []
    for i in range(BEZIER_SEGMENTS + 1):
        t = i / BEZIER_SEGMENTS
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def _stroke(line_pts: Sequence[tuple], width_mm: float):
    """A stroked open path: the path widened by its stroke, with round ends (as KiCad)."""
    from shapely.geometry import LineString
    if width_mm <= 0 or len(line_pts) < 2:
        return None
    return LineString(line_pts).buffer(width_mm / 2.0, quad_segs=SHAPE_QUAD_SEGS)


def _area(area, width_mm: float, filled: bool):
    """A closed shape: its interior when filled, plus its stroke along the boundary."""
    parts = []
    if filled:
        parts.append(area)
    if width_mm > 0:
        parts.append(area.boundary.buffer(width_mm / 2.0, quad_segs=SHAPE_QUAD_SEGS))
    if not parts:
        return None
    from shapely.ops import unary_union
    return unary_union(parts)


def shape_geometry(shape) -> Optional[object]:
    """Copper of one kipy board shape as a shapely geometry, or None if it has no area."""
    from kipy.board_types import (BoardArc, BoardBezier, BoardCircle, BoardPolygon,
                                  BoardRectangle, BoardSegment)
    from shapely.geometry import Point as SPoint, Polygon as SPolygon
    from shapely.ops import unary_union

    attrs = shape.attributes
    width = mm(attrs.stroke.width)
    filled = attrs.fill.filled
    if isinstance(shape, BoardSegment):
        return _stroke([pt(shape.start), pt(shape.end)], width)
    if isinstance(shape, BoardArc):
        st, md, en = pt(shape.start), pt(shape.mid), pt(shape.end)
        return _stroke([st] + arc_points(st, md, en), width)
    if isinstance(shape, BoardBezier):
        return _stroke(_bezier_points(pt(shape.start), pt(shape.control1),
                                      pt(shape.control2), pt(shape.end)), width)
    if isinstance(shape, BoardCircle):
        disc = SPoint(pt(shape.center)).buffer(mm(shape.radius()), quad_segs=SHAPE_QUAD_SEGS)
        return _area(disc, width, filled)
    if isinstance(shape, BoardRectangle):
        (x1, y1), (x2, y2) = pt(shape.top_left), pt(shape.bottom_right)
        return _area(SPolygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)]), width, filled)
    if isinstance(shape, BoardPolygon):
        parts = []
        for pwh in shape.polygons:
            p = poly(pwh)
            if len(p.outline) >= 3:
                parts.append(_area(SPolygon(p.outline, p.holes), width, filled))
        parts = [g for g in parts if g is not None]
        return unary_union(parts) if parts else None
    return None


def copper_shape_zones(shapes: Iterable, copper_layer_ids: Sequence[int],
                       progress: ProgressReporter) -> List[Zone]:
    """One zone per copper graphic shape that carries a net."""
    cu = set(copper_layer_ids)
    zones: List[Zone] = []
    no_net = 0
    for s in shapes:
        if s.layer not in cu:
            continue
        net = s.net.name if s.net is not None else ""
        if not net:
            no_net += 1
            continue
        polys = shapely_to_polys(shape_geometry(s))
        if polys:
            zones.append(Zone(id=s.id.value, net=net, name="graphic shape", priority=0,
                              filled={s.layer: polys}))
    if zones:
        progress.info(f"{len(zones)} copper graphic shapes with a net")
    if no_net:
        progress.warn(f"{no_net} copper graphic shapes have no net and carry no current "
                      "in the model")
    return zones
