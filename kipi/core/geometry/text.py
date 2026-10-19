"""Text as polygons, for board files that store text as a string and leave the drawing of
it to KiCad (#82).

KiCad draws board text in its own stroke font, which a board file does not carry. KiPI
draws it in matplotlib's DejaVu Sans instead, scaled so a capital is the text's height
and a character its width: the same place and about the same ink, not KiCad's exact
glyphs. The running KiCad draws its own text (see the KiCad API source).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import shapely
from shapely import affinity

CAP_HEIGHT_EM = 0.729       # DejaVu Sans: a capital's height, in ems
ADVANCE_EM = 0.62           # its mean character advance, in ems
LINE_SPACING = 1.62         # KiCad's line pitch, in text heights
_FONT_SIZE = 10.0           # glyphs are drawn at this size and scaled


@lru_cache(maxsize=512)
def _line(text: str):
    """One line of text as a shapely geometry, y up, starting at x = 0 on the baseline,
    drawn at ``_FONT_SIZE``. None if nothing of it is drawn."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    path = TextPath((0, 0), text, size=_FONT_SIZE, prop=FontProperties(family="DejaVu Sans"))
    geom = None
    for ring in path.to_polygons(closed_only=True):
        if len(ring) < 3:
            continue
        p = shapely.Polygon(ring)
        if not p.is_valid:
            p = p.buffer(0)
        # A glyph's holes are rings inside its outline: alternate in and out.
        geom = p if geom is None else geom.symmetric_difference(p)
    return geom


def text_geometry(text: str, at, height: float, width: float, thickness: float = 0.0,
                  angle_deg: float = 0.0, h_justify: str = "center",
                  v_justify: str = "center", mirror: bool = False) -> Optional[object]:
    """The ink of ``text`` on the board, in board coordinates (y down): anchored at
    ``at``, ``height`` and ``width`` per character (mm), stroked ``thickness`` wide,
    turned ``angle_deg`` counter-clockwise as KiCad shows it, justified as KiCad
    justifies (``left``/``center``/``right``, ``top``/``center``/``bottom``), and mirrored
    for text read from the far side. None if there is no ink."""
    lines = text.split("\n")
    if height <= 0 or not any(s.strip() for s in lines):
        return None
    scale_y = height / (CAP_HEIGHT_EM * _FONT_SIZE)
    scale_x = (width if width > 0 else height) / (ADVANCE_EM * _FONT_SIZE)
    parts = []
    pitch = height * LINE_SPACING
    for i, s in enumerate(lines):
        g = _line(s) if s.strip() else None
        if g is None:
            continue
        g = affinity.scale(g, scale_x, scale_y, origin=(0, 0))
        minx, _, maxx, _ = g.bounds
        w = maxx - minx
        dx = {"left": -minx, "right": -maxx}.get(h_justify, -(minx + w / 2))
        parts.append(affinity.translate(g, dx, -i * pitch))
    if not parts:
        return None
    geom = shapely.union_all(parts)
    # The block's vertical place: KiCad anchors the first line's top, the block's middle
    # or the last line's baseline.
    span = (len(lines) - 1) * pitch
    dy = {"top": -height, "bottom": span}.get(v_justify, span / 2 - height / 2)
    geom = affinity.translate(geom, 0, dy)
    if thickness > 0:
        # DejaVu's own stroke is about 0.13 of a capital; widen bolder text to match.
        extra = thickness - 0.13 * height
        if extra > 0:
            geom = geom.buffer(extra / 2, quad_segs=2)
    if mirror:
        geom = affinity.scale(geom, -1, 1, origin=(0, 0))
    geom = affinity.scale(geom, 1, -1, origin=(0, 0))          # y up -> KiCad's y down
    # KiCad's angles turn counter-clockwise as seen, with y down.
    geom = affinity.rotate(geom, -angle_deg, origin=(0, 0))
    geom = affinity.translate(geom, at[0], at[1])
    return None if geom.is_empty else geom
