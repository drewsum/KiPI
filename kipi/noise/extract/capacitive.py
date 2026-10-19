"""Capacitive (electric-field) coupling from switch nodes onto a victim's traces (#69).

The victim's traces are cut into segments of at most ``segment_mm``. At each segment's
middle a line across it, ``window_mm`` to either side, is intersected with the board's
copper on every layer: the switch nodes' copper (the aggressors), the planes and power and
ground copper (held at 0 V: they are AC ground), and the segment itself. Those intervals,
at their layers' heights and with the stackup's dielectrics, make a 2D cross-section
(:mod:`kipi.noise.extract.xsection`) that gives the segment's capacitance C′, inductance
L′ and its mutual capacitance Cm′ to each switch node, per mm.

A switch node's charge reaches the victim as a current jω·Cm′·l·V_sw along each segment.
Cuts with the same copper in the same places give the same answer, so they are solved
once (a long run beside the same copper costs one solve).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.core.model.stackup import combined_epsilon_r
from kipi.noise.extract.xsection import GROUND, VICTIM, Band, Cache, Rect

MASK_ER = 3.5                       # a typical LPI solder mask
DEFAULT_ER = 4.3


@dataclass
class Segment:
    """A straight piece of a victim trace, with its per-mm line constants."""
    a: Tuple[float, float]
    b: Tuple[float, float]
    layer: int
    width_mm: float
    c_f_per_mm: float = 0.0
    l_h_per_mm: float = 0.0
    cm: Dict[str, float] = field(default_factory=dict)   # switch net -> Cm′, F/mm

    @property
    def length_mm(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    @property
    def z0_ohm(self) -> float:
        return math.sqrt(self.l_h_per_mm / self.c_f_per_mm) if self.c_f_per_mm else 0.0


def split_tracks(snap: BoardSnapshot, net: str, segment_mm: float) -> List[Segment]:
    """The net's tracks as straight segments of at most ``segment_mm`` (arcs as chords)."""
    from kipi.core.geometry.arcs import arc_points
    tracks, _, _, _ = snap.items_on_net(net)
    out = []
    for t in tracks:
        pts = [t.start] + (arc_points(t.start, t.mid, t.end) if t.is_arc else [t.end])
        for p, q in zip(pts, pts[1:]):
            length = math.hypot(q[0] - p[0], q[1] - p[1])
            if length < 1e-6:
                continue
            n = max(1, int(math.ceil(length / segment_mm)))
            for k in range(n):
                a = (p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n)
                b = (p[0] + (q[0] - p[0]) * (k + 1) / n, p[1] + (q[1] - p[1]) * (k + 1) / n)
                out.append(Segment(a, b, t.layer, t.width_mm))
    return out


class BoardCuts:
    """The board's copper by layer and role, for cutting cross-sections."""

    def __init__(self, snap: BoardSnapshot, victim: str, switch_nets: Sequence[str],
                 window_mm: float, default_er: float = DEFAULT_ER):
        import shapely
        from kipi.core.geometry.copper import net_layer_copper, poly_to_shapely
        from kipi.core.model.nets import classify_nets
        self.snap = snap
        self.window = window_mm
        self.stack = snap.stackup
        self.switch_nets = [n for n in dict.fromkeys(switch_nets) if n]
        kinds = classify_nets(snap)
        ref_nets = [n for n, k in kinds.items() if k in ("ground", "power")
                    and n != victim and n not in self.switch_nets]
        self.agg: Dict[Tuple[str, int], object] = {}
        self.ref: Dict[int, object] = {}
        for lay in self.stack.copper:
            for n in self.switch_nets:
                g = net_layer_copper(snap, n, lay.id, cut_drills=False)
                if not g.is_empty:
                    shapely.prepare(g)
                    self.agg[(n, lay.id)] = g
            parts = [net_layer_copper(snap, n, lay.id, cut_drills=False) for n in ref_nets]
            parts += [poly_to_shapely(p) for z in snap.zones
                      if z.net not in ref_nets and z.net != victim
                      and z.net not in self.switch_nets for p in z.filled.get(lay.id, [])]
            parts = [p for p in parts if p is not None and not p.is_empty]
            if parts:
                g = shapely.union_all(parts)
                shapely.prepare(g)
                self.ref[lay.id] = g
        self.bands = _bands(self.stack, default_er)
        zs = [c.z_bottom_mm for c in self.stack.copper] + [c.z_top_mm for c in self.stack.copper]
        margin = max(2.0, window_mm)
        self.z_range = (min(zs) - margin, max(zs) + margin)
        shapely_all = [g for g in self.agg.values()]
        self.agg_union = shapely.union_all(shapely_all) if shapely_all else None

    def near_aggressor(self, seg: Segment) -> bool:
        from shapely.geometry import LineString
        if self.agg_union is None:
            return False
        return LineString([seg.a, seg.b]).distance(self.agg_union) <= self.window

    def section(self, seg: Segment) -> Tuple[List[Rect], List[Band], tuple]:
        """The cross-section at the segment's middle, x along the cut from its centre."""
        from shapely.geometry import LineString
        mx, my = (seg.a[0] + seg.b[0]) / 2, (seg.a[1] + seg.b[1]) / 2
        ux, uy = (seg.b[0] - seg.a[0]) / seg.length_mm, (seg.b[1] - seg.a[1]) / seg.length_mm
        nx, ny = -uy, ux
        w = self.window
        cut = LineString([(mx - w * nx, my - w * ny), (mx + w * nx, my + w * ny)])
        rects: List[Rect] = []
        vl = self.stack.layer(seg.layer)
        rects.append(Rect(-seg.width_mm / 2, seg.width_mm / 2, vl.z_bottom_mm, vl.z_top_mm,
                          VICTIM))
        keep_out = (-seg.width_mm / 2, seg.width_mm / 2)
        for lay in self.stack.copper:
            pieces = []
            for n in self.switch_nets:
                g = self.agg.get((n, lay.id))
                if g is not None:
                    pieces += [(iv, n) for iv in _intervals(cut, g, mx, my, nx, ny)]
            g = self.ref.get(lay.id)
            if g is not None:
                pieces += [(iv, GROUND) for iv in _intervals(cut, g, mx, my, nx, ny)]
            for (x0, x1), group in pieces:
                if lay.id == seg.layer:          # not over the victim itself
                    x0, x1 = _clip_out(x0, x1, keep_out)
                    if x1 - x0 < 1e-4:
                        continue
                rects.append(Rect(x0, x1, lay.z_bottom_mm, lay.z_top_mm, group))
        box = (-w, w, *self.z_range)
        return rects, self.bands, box


def _intervals(cut, geom, mx, my, nx, ny) -> List[Tuple[float, float]]:
    """Where ``cut`` crosses ``geom``, as positions along the cut from its middle."""
    import shapely
    inter = cut.intersection(geom)
    out = []
    for part in shapely.get_parts(inter):
        if part.geom_type != "LineString" or part.length < 1e-6:
            continue
        cs = np.asarray(part.coords)
        s = (cs[:, 0] - mx) * nx + (cs[:, 1] - my) * ny
        out.append((float(s.min()), float(s.max())))
    return out


def _clip_out(x0, x1, keep):
    """Shorten [x0, x1] so it does not overlap ``keep`` (the victim's own copper)."""
    k0, k1 = keep
    if x1 <= k0 or x0 >= k1:
        return x0, x1
    if x0 < k0:
        return x0, k0 - 1e-3
    if x1 > k1:
        return k1 + 1e-3, x1
    return 0.0, 0.0


def _bands(stack, default_er: float) -> List[Band]:
    """Each dielectric between two coppers, the coppers' own heights (filled with the
    dielectric beside them) and the solder mask outside the outer coppers."""
    bands = []
    cu = stack.copper
    for upper, lower in zip(cu, cu[1:]):
        subs = [(d.thickness_mm, d.epsilon_r) for d in stack.dielectrics
                if d.above_layer == upper.id and d.below_layer == lower.id]
        er = combined_epsilon_r(subs) or default_er
        bands.append(Band(lower.z_top_mm, upper.z_bottom_mm, er))
    for i, c in enumerate(cu):
        if 0 < i < len(cu) - 1:
            er = bands[i].er if i < len(bands) else default_er
            bands.append(Band(c.z_bottom_mm, c.z_top_mm, er))
    for side in ("top", "bottom"):
        mask = next((c for c in stack.coatings_on(side) if c.kind == "mask"), None)
        if mask is None or not cu:
            continue
        outer = cu[0] if side == "top" else cu[-1]
        if side == "top":
            bands.append(Band(outer.z_bottom_mm, outer.z_top_mm + mask.thickness_mm, MASK_ER))
        else:
            bands.append(Band(outer.z_bottom_mm - mask.thickness_mm, outer.z_top_mm, MASK_ER))
    return bands


@dataclass
class Extraction:
    segments: List[Segment]
    solves: int = 0
    cached: int = 0


def extract(snap: BoardSnapshot, net: str, switch_nets: Sequence[str], segment_mm: float,
            window_mm: float, default_er: float = DEFAULT_ER, cache: Optional[Cache] = None,
            check_cancel=None) -> Extraction:
    """Every segment of ``net`` with its C′, L′ and Cm′ to each switch net."""
    segs = split_tracks(snap, net, segment_mm)
    cuts = BoardCuts(snap, net, switch_nets, window_mm, default_er)
    cache = cache or Cache()
    h0, m0 = cache.hits, cache.misses
    for s in segs:
        if check_cancel:
            check_cancel()
        rects, bands, box = cuts.section(s)
        res = cache.solve(rects, bands, box)
        s.c_f_per_mm, s.l_h_per_mm = res.c_f_per_mm, res.l_h_per_mm
        s.cm = {n: v for n, v in res.cm.items() if v > 0}
    return Extraction(segs, cache.misses - m0, cache.hits - h0)
