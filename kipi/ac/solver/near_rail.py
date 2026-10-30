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

Beyond the same margin the mesh also coarsens (#228, ``ACSettings.coarsen_far``): its
edges grow from max edge, small holes are filled, far ground vias joined at their centers,
and islands that touch at most one plated hole (no current passes through them) left out
(:func:`kipi.core.mesh.mesher.trim_far_holes`, :func:`~kipi.core.mesh.mesher.far_dead_ends`).
Every other piece of copper is kept, so those shortcuts are too. A small rail on a 10-layer
board's ground solved in a third of the time, its impedance moved by at most 0.05 %; one
spread over the board, with little far ground, gained little (0.02 % to 100 MHz, 0.3 % at
a 270 MHz resonance).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import shapely
from shapely.geometry import Polygon

from kipi.core.geometry.copper import iter_polygons, net_layer_copper, poly_to_shapely
from kipi.core.model.board import BoardSnapshot

# The holes graded are those within this many board thicknesses of the rail, and at
# least MARGIN_MIN_MM: the return current under copper at height h falls off as
# 1/(1+(x/h)²) away from it, and no copper is farther than the board's thickness from
# any other.
MARGIN_THICKNESSES = 5.0
MARGIN_MIN_MM = 5.0
ZONE_SIMPLIFY_MM = 0.05


def margin_mm(snap: BoardSnapshot) -> float:
    return max(MARGIN_MIN_MM, MARGIN_THICKNESSES * snap.stackup.board_thickness_mm)


class RailZone:
    """Where the AC mesh is near the rail: within :func:`margin_mm` of its copper on any
    layer or of its ports' pads. Calling it tests points ((N, 2) mm) for being near;
    :meth:`distance` gives how far they are. It pickles, without its index, for the
    mesher's worker processes."""

    def __init__(self, parts, margin: float):
        self.parts, self.margin = list(parts), margin
        self._tree = None

    def __getstate__(self):
        return {"parts": self.parts, "margin": self.margin, "_tree": None}

    def distance(self, xy: np.ndarray) -> np.ndarray:
        """Each point's distance (mm) from the rail's copper and ports, 0 on them."""
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        out = np.full(len(xy), np.inf)
        if len(xy) and self.parts:
            if self._tree is None:
                self._tree = shapely.STRtree(self.parts)
            (i, _), d = self._tree.query_nearest(shapely.points(xy), return_distance=True)
            np.minimum.at(out, i, d)
        return out

    def __call__(self, xy: np.ndarray) -> np.ndarray:
        return self.distance(xy) <= self.margin


def near_rail(snap: BoardSnapshot, rail_nets: Sequence[str], terminal_ids: Sequence[str],
              quad_segs: int) -> RailZone:
    """The zone within :func:`margin_mm` of the rail's copper on any layer or of the pads
    in ``terminal_ids``."""
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
    # Only distances from it matter, to a few mm: its outlines, simplified, are as good
    # and much quicker to measure from than copper full of antipads.
    parts = [Polygon(g.exterior).simplify(ZONE_SIMPLIFY_MM) if isinstance(g, Polygon) else g
             for g in parts]
    return RailZone(parts, margin_mm(snap))
