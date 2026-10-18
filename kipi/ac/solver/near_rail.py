"""Which plated holes the AC mesh is graded at (#123).

The mesh is refined toward plated holes, from the AC settings' min edge, because that is
where via inductance is decided. On a board stitched with hundreds of ground vias, that
refinement is most of the mesh, though the vias far from the rail carry almost none of
its current. Grading only the holes within :func:`margin_mm` of the rail's copper or a
port's pads (on any layer) keeps every piece of copper, so the low-frequency return
still takes every path it has, and coarsens only the mesh around far vias. On an
8-layer board with 745 ground holes this halved the solve and moved no probe's
impedance by more than 0.002 %.

Clipping the far ground away instead was tried and dropped: it changed the impedance
by several percent, because the low-frequency return takes shortcuts through ground
copper nowhere near the rail.
"""

from __future__ import annotations

from typing import Callable, Sequence

import numpy as np
import shapely

from kipi.core.geometry.copper import iter_polygons, net_layer_copper, poly_to_shapely
from kipi.core.model.board import BoardSnapshot

# The holes graded are those within this many board thicknesses of the rail, and at
# least MARGIN_MIN_MM: the return current under copper at height h falls off as
# 1/(1+(x/h)²) away from it, and no copper is farther than the board's thickness from
# any other.
MARGIN_THICKNESSES = 5.0
MARGIN_MIN_MM = 5.0


def margin_mm(snap: BoardSnapshot) -> float:
    return max(MARGIN_MIN_MM, MARGIN_THICKNESSES * snap.stackup.board_thickness_mm)


def near_rail(snap: BoardSnapshot, rail_nets: Sequence[str], terminal_ids: Sequence[str],
              quad_segs: int) -> Callable[[np.ndarray], np.ndarray]:
    """A test of points ((N, 2) mm) for being within :func:`margin_mm` of the rail's
    copper on any layer or of the pads in ``terminal_ids``."""
    parts = []
    for net in rail_nets:
        for cu in snap.stackup.copper:
            g = net_layer_copper(snap, net, cu.id, cut_drills=False, quad_segs=quad_segs)
            parts += list(iter_polygons(g))
    wanted = set(terminal_ids)
    for p in snap.pads:
        if p.id in wanted:
            parts += [g for polys in p.shapes.values() for g in map(poly_to_shapely, polys)
                      if g is not None and not g.is_empty]
    tree = shapely.STRtree(parts)
    margin = margin_mm(snap)

    def test(xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        near = np.zeros(len(xy), dtype=bool)
        if len(xy) and parts:
            hit, _ = tree.query(shapely.points(xy), predicate="dwithin", distance=margin)
            near[hit] = True
        return near
    return test
