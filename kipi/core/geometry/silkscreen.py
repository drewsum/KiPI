"""The silkscreen's printing (#82): which side a silkscreen layer is on, and where its ink
lands on the board.

Board readers collect each side's silkscreen graphics and text as shapely geometries and
store them in ``BoardSnapshot.silkscreen``. Ink is printed only on the board and never
on the pads the solder mask leaves open (fabricators clip it there), so
:func:`printed_silk` trims it to both; the 3D view draws that and the thermal model
covers those cells with the silkscreen's ink.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

import shapely

from kipi.core.geometry.copper import poly_to_shapely, shapely_to_polys
from kipi.core.model.board import BoardSnapshot, Poly

#: Silkscreen layers by name, as board files (``F.SilkS``) and the KiCad API
#: (``BL_F_SilkS``) call them.
_SIDES = {"f.silks": "top", "b.silks": "bottom", "bl_f_silks": "top", "bl_b_silks": "bottom"}


def silk_side(layer_name: str) -> Optional[str]:
    """``"top"`` or ``"bottom"`` for a silkscreen layer, else None."""
    return _SIDES.get((layer_name or "").lower())


def merge(parts_by_side: Dict[str, Iterable]) -> Dict[str, List[Poly]]:
    """Each side's geometries merged into polygons, leaving out sides with none."""
    out = {}
    for side, parts in parts_by_side.items():
        parts = [g for g in parts if g is not None and not g.is_empty]
        polys = shapely_to_polys(shapely.union_all(parts)) if parts else []
        if polys:
            out[side] = polys
    return out


def _union(polys: Iterable[Poly]):
    parts = [g for g in (poly_to_shapely(p) for p in polys) if g is not None]
    return shapely.union_all(parts) if parts else None


def mask_openings(snap: BoardSnapshot, side: str):
    """The pads the solder mask on ``side`` leaves open: the pad copper on the outer
    layer under it. None if there is none."""
    st = snap.stackup
    if not st.copper:
        return None
    cu = st.copper[0] if side == "top" else st.copper[-1]
    return _union(q for pad in snap.pads for q in pad.shapes.get(cu.id, []))


def printed_silk(snap: BoardSnapshot, side: str, board=None):
    """Where the silkscreen on ``side`` puts ink: its printing, on the board (``board``,
    the outline as one geometry, else read from the snapshot) and off the open pads.
    None if nothing is printed there."""
    ink = _union(snap.silkscreen.get(side, []))
    if ink is None:
        return None
    if board is None:
        board = _union(snap.outline)
    if board is not None:
        ink = ink.intersection(board)
    openings = mask_openings(snap, side)
    if openings is not None:
        ink = ink.difference(openings)
    return None if ink.is_empty else ink
