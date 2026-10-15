"""Merge a net's copper on each layer into a single shapely geometry.

The copper for (net, layer) is the union of:

* tracks and arc tracks, buffered to their width with round caps
* pad copper shapes
* via annular rings on every layer the via spans
* zone fills

Via and PTH drill holes are then cut out.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from kipi.kicad_io.convert import arc_points as _arc_points
from kipi.model.board import BoardSnapshot, Pad, Poly
from kipi.progress import NULL, ProgressReporter

CIRCLE_QUAD_SEGS = 8   # 32 segments per full circle


def poly_to_shapely(p: Poly) -> Optional[Polygon]:
    if len(p.outline) < 3:
        return None
    g = Polygon(p.outline, [h for h in p.holes if len(h) >= 3])
    if not g.is_valid:
        g = shapely.make_valid(g)
    return g


def _polys(items: Iterable[Poly]) -> List[BaseGeometry]:
    out = []
    for p in items:
        g = poly_to_shapely(p)
        if g is not None and not g.is_empty:
            out.append(g)
    return out


def _only_polygons(g: BaseGeometry) -> BaseGeometry:
    """Drop stray lines/points that unions and differences can leave behind."""
    if isinstance(g, (Polygon, MultiPolygon)):
        return g
    parts = [x for x in getattr(g, "geoms", []) if isinstance(x, (Polygon, MultiPolygon))]
    return shapely.union_all(parts) if parts else Polygon()


def net_layer_copper(snap: BoardSnapshot, net: str, layer: int,
                     cut_drills: bool = True, quad_segs: int = CIRCLE_QUAD_SEGS) -> BaseGeometry:
    """Copper of ``net`` on ``layer``. ``quad_segs`` sets circle resolution (segments per
    quarter circle) for track caps, via rings and drill holes."""
    tracks, vias, pads, zones = snap.items_on_net(net)
    parts: List[BaseGeometry] = []

    straight = [t for t in tracks if t.layer == layer and not t.is_arc]
    if straight:
        lines = shapely.linestrings([[t.start, t.end] for t in straight])
        widths = np.array([t.width_mm for t in straight])
        parts.extend(shapely.buffer(lines, widths / 2.0, quad_segs=quad_segs))
    for t in tracks:
        if t.layer == layer and t.is_arc:
            pts = [t.start] + _arc_points(t.start, t.mid, t.end)
            parts.append(LineString(pts).buffer(t.width_mm / 2.0, quad_segs=quad_segs))

    for p in pads:
        parts.extend(_polys(p.shapes.get(layer, [])))

    span_vias = [v for v in vias if layer in v.layers and v.pad_diameter(layer) > 0]
    if span_vias:
        centers = shapely.points([v.position for v in span_vias])
        radii = np.array([v.pad_diameter(layer) / 2.0 for v in span_vias])
        parts.extend(shapely.buffer(centers, radii, quad_segs=quad_segs))

    for z in zones:
        parts.extend(_polys(z.filled.get(layer, [])))

    if not parts:
        return Polygon()
    geom = shapely.union_all(parts)

    if cut_drills:
        holes = drill_holes(snap, net, layer, quad_segs)
        if holes is not None:
            geom = geom.difference(holes)
    return _only_polygons(geom)


def drill_holes(snap: BoardSnapshot, net: str, layer: int,
                quad_segs: int = CIRCLE_QUAD_SEGS) -> Optional[BaseGeometry]:
    """Drill holes of the net's vias and through-hole pads that pass through ``layer``."""
    holes = []
    _, vias, pads, _ = snap.items_on_net(net)
    for v in vias:
        if layer in v.layers and v.drill_mm > 0:
            holes.append(shapely.Point(v.position).buffer(v.drill_mm / 2.0,
                                                          quad_segs=quad_segs))
    for p in pads:
        if p.pad_type == "pth" and layer in p.shapes:
            dx, dy = p.drill_mm
            if dx <= 0:
                continue
            if abs(dx - dy) < 1e-6:
                holes.append(shapely.Point(p.position).buffer(dx / 2.0,
                                                              quad_segs=quad_segs))
            else:  # oblong slot
                r = min(dx, dy) / 2.0
                half = (max(dx, dy) - min(dx, dy)) / 2.0
                ux, uy = slot_axis(p)
                x, y = p.position
                seg = [(x - half * ux, y - half * uy), (x + half * ux, y + half * uy)]
                holes.append(LineString(seg).buffer(r, quad_segs=quad_segs))
    return shapely.union_all(holes) if holes else None


SLOT_SHAPE_ASPECT = 1.05   # a pad this much longer than wide shows which way its slot runs


def slot_axis(p: Pad) -> Tuple[float, float]:
    """Unit vector (KiCad board axes, y down) along the long axis of a pad's slot.

    The drill size is in the pad's own frame, so the slot turns with the pad. Snapshots
    saved before pad angles were recorded take the long axis of the pad's copper (an
    oblong pad's slot runs along it), or failing that the unrotated drill axis.
    """
    dx, dy = p.drill_mm
    if p.angle_deg is not None:
        a = math.radians(p.angle_deg)
        # KiCad angles turn counter-clockwise on screen, i.e. with y pointing down the
        # pad's x axis maps to (cos a, -sin a) and its y axis to (sin a, cos a).
        return (math.cos(a), -math.sin(a)) if dx > dy else (math.sin(a), math.cos(a))
    copper = [g for polys in p.shapes.values() for g in _polys(polys)]
    if copper:
        rect = shapely.union_all(copper).minimum_rotated_rectangle
        c = np.asarray(rect.exterior.coords)[:3] if isinstance(rect, Polygon) else None
        if c is not None:
            e1, e2 = c[1] - c[0], c[2] - c[1]
            l1, l2 = float(np.hypot(*e1)), float(np.hypot(*e2))
            if min(l1, l2) > 0 and max(l1, l2) >= SLOT_SHAPE_ASPECT * min(l1, l2):
                e, n = (e1, l1) if l1 > l2 else (e2, l2)
                return float(e[0] / n), float(e[1] / n)
    return (1.0, 0.0) if dx > dy else (0.0, 1.0)


def net_copper(snap: BoardSnapshot, net: str,
               progress: ProgressReporter = NULL) -> Dict[int, BaseGeometry]:
    """Copper geometry of ``net`` on every copper layer that has any."""
    out: Dict[int, BaseGeometry] = {}
    for cu in snap.stackup.copper:
        g = net_layer_copper(snap, net, cu.id)
        if not g.is_empty:
            out[cu.id] = g
            progress.debug(f"  {net} / {cu.name}: {g.area:.2f} mm^2")
    return out


def iter_polygons(g: BaseGeometry):
    if isinstance(g, Polygon):
        if not g.is_empty:
            yield g
    else:
        for part in getattr(g, "geoms", []):
            yield from iter_polygons(part)
