"""Drawn shapes to copper and board outlines: stroked paths, filled areas, beziers.

Board readers turn each file's graphic items into these shapely geometries the same way,
so a board reads the same whichever reader it went through.
"""

from __future__ import annotations

from typing import List, Sequence

from kipi.core.geometry.copper import shapely_to_polys
from kipi.core.model.board import Poly
from kipi.core.progress import NULL, ProgressReporter

BEZIER_SEGMENTS = 32
SHAPE_QUAD_SEGS = 8          # stroke end caps and circles: 32 segments per circle


def bezier_points(p0, p1, p2, p3) -> List[tuple]:
    pts = []
    for i in range(BEZIER_SEGMENTS + 1):
        t = i / BEZIER_SEGMENTS
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def stroke(line_pts: Sequence[tuple], width_mm: float):
    """A stroked open path: the path widened by its stroke, with round ends (as KiCad)."""
    from shapely.geometry import LineString
    if width_mm <= 0 or len(line_pts) < 2:
        return None
    return LineString(line_pts).buffer(width_mm / 2.0, quad_segs=SHAPE_QUAD_SEGS)


def stroke_segments(segments: Sequence[tuple], width_mm: float) -> List:
    """Stroked segments ``((x1, y1), (x2, y2))`` of one width, as :func:`stroke` draws
    each. Consecutive segments where one ends and the next starts (a stroke-font glyph's
    strokes) are stroked as one path, which merges several times faster."""
    import shapely
    if width_mm <= 0 or not segments:
        return []
    paths: List[list] = []
    for start, end in segments:
        if paths and paths[-1][-1] == start:
            paths[-1].append(end)
        else:
            paths.append([start, end])
    lines = [shapely.LineString(p) for p in paths]
    return list(shapely.buffer(lines, width_mm / 2.0, quad_segs=SHAPE_QUAD_SEGS))


def area(shape, width_mm: float, filled: bool):
    """A closed shape: its interior when filled, plus its stroke along the boundary."""
    parts = []
    if filled:
        parts.append(shape)
    if width_mm > 0:
        parts.append(shape.boundary.buffer(width_mm / 2.0, quad_segs=SHAPE_QUAD_SEGS))
    if not parts:
        return None
    from shapely.ops import unary_union
    return unary_union(parts)


def board_outline(lines: Sequence, closed: Sequence,
                  progress: ProgressReporter = NULL) -> List[Poly]:
    """The board from its edge drawings: ``lines`` (shapely LineStrings that join into
    loops) and ``closed`` (shapely Polygons: circles, rectangles, polygons). The largest
    loop is the board and the others inside it are cutouts (even-odd)."""
    from shapely.ops import polygonize, unary_union
    polys = list(polygonize(unary_union(list(lines)))) if lines else []
    polys += list(closed)
    if not polys:
        progress.warn("No closed board outline found on Edge.Cuts")
        return []
    polys.sort(key=lambda p: p.area, reverse=True)
    board_poly = polys[0]
    for p in polys[1:]:
        board_poly = board_poly.symmetric_difference(p)
    return shapely_to_polys(board_poly)
