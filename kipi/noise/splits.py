"""A victim crossing a split in its reference plane (#146).

A trace's return current flows in the plane under it. Where the plane stops (a slot, a
gap between two planes, the edge of a pour) the return has to go round: the trace's
impedance jumps there and the detour adds inductance in series with the signal. Where the
reference changes from one plane to another (a ground plane to a power plane, say) the
return has to pass through whatever joins the two, the nearest capacitor between those
nets, and that detour's inductance and the capacitor's own add in series.

The cross-sections already give each segment the capacitance and inductance of the copper
actually under it (a segment over a gap sees the far planes), and the inductive coupling
counts the loops' real current round a gap rather than a solid plane's images
(:mod:`.extract.inductive`). This module adds the detour: each crossing is found where
two joined segments have different references, and its inductance is Johnson's estimate
for a trace crossing a slot, L ≈ 0.197 nH/mm · D · ln(D/w), D the distance the return
current detours and w the trace width, put in series with the segment after the crossing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.noise.extract.capacitive import Segment

SNAP_MM = 1e-3
DETOUR_CAP_MM = 50.0                    # the longest detour counted
NH_PER_MM = 0.197                       # Johnson: 5 nH per inch · D · ln(D/w)
FALLBACK_ESL_H = 0.5e-9


@dataclass
class SplitCrossing:
    """One crossing: where, between which references, how far the return detours and the
    inductance that puts in series with segment ``segment``."""
    x_mm: float
    y_mm: float
    layer: str                          # the victim's layer
    from_ref: str                       # e.g. "GND (In1.Cu)", "no plane"
    to_ref: str
    detour_mm: float
    l_h: float
    segment: int
    note: str = ""

    def describe(self) -> str:
        return (f"crosses from {self.from_ref} to {self.to_ref} at ({self.x_mm:.1f}, "
                f"{self.y_mm:.1f}) mm on {self.layer}: the return detours "
                f"{self.detour_mm:.3g} mm, {self.l_h * 1e9:.3g} nH in series"
                + (f" ({self.note})" if self.note else ""))


def slot_inductance(detour_mm: float, width_mm: float) -> float:
    """Johnson's estimate for a trace crossing a slot, henries."""
    if detour_mm <= width_mm * 1.01:
        return 0.0
    return NH_PER_MM * 1e-9 * detour_mm * math.log(detour_mm / max(width_mm, 1e-3))


class _Planes:
    """Zone copper by layer and net, and the gaps in it, for one net's crossings."""

    def __init__(self, snap: BoardSnapshot, exclude: Sequence[str]):
        import shapely
        from kipi.core.geometry.copper import poly_to_shapely
        self.snap = snap
        self.stack = snap.stackup
        self.order = [c.id for c in self.stack.copper]
        self.z = {c.id: c.z_mid_mm for c in self.stack.copper}
        self.names = {c.id: c.name for c in self.stack.copper}
        skip = set(exclude)
        self.by_layer: Dict[int, List[Tuple[str, object]]] = {}
        for z in snap.zones:
            if z.net in skip or not z.net:
                continue
            for lid, polys in z.filled.items():
                geoms = [poly_to_shapely(p) for p in polys]
                geoms = [g for g in geoms if g is not None and not g.is_empty]
                if geoms:
                    g = shapely.union_all(geoms)
                    shapely.prepare(g)
                    self.by_layer.setdefault(lid, []).append((z.net, g))
        self._gaps: Dict[int, object] = {}
        self._outline = None

    def reference(self, layer: int, xy) -> Optional[Tuple[int, str]]:
        """The nearest copper layer (either way, nearest first) whose zone copper covers
        ``xy``, and that copper's net; None where none does."""
        import shapely
        if layer not in self.order:
            return None
        i = self.order.index(layer)
        others = sorted((j for j in range(len(self.order)) if j != i),
                        key=lambda j: (abs(self.z[self.order[j]] - self.z[layer]), j))
        for j in others:
            lid = self.order[j]
            for net, g in self.by_layer.get(lid, []):
                if shapely.contains_xy(g, xy[0], xy[1]):
                    return lid, net
        return None

    def label(self, ref: Optional[Tuple[int, str]]) -> str:
        if ref is None:
            return "no plane"
        return f"{ref[1]} ({self.names.get(ref[0], ref[0])})"

    def gap_detour(self, layer: int, xy) -> Tuple[float, str]:
        """How far the return current detours round the gap in ``layer``'s copper at
        ``xy``: half the gap's longest extent, at most DETOUR_CAP_MM."""
        import shapely
        from kipi.core.geometry.copper import poly_to_shapely
        if layer not in self._gaps:
            if self._outline is None:
                polys = [poly_to_shapely(p) for p in self.snap.outline]
                polys = [g for g in polys if g is not None and not g.is_empty]
                self._outline = shapely.union_all(polys) if polys else None
            cover = shapely.union_all([g for _, g in self.by_layer.get(layer, [])]) \
                if self.by_layer.get(layer) else None
            if self._outline is None:
                self._gaps[layer] = None
            elif cover is None:
                self._gaps[layer] = self._outline
            else:
                self._gaps[layer] = self._outline.difference(cover)
        gaps = self._gaps[layer]
        if gaps is None or gaps.is_empty:
            return DETOUR_CAP_MM, "the board's outline is not known"
        pt = shapely.Point(xy[0], xy[1])
        parts = [g for g in shapely.get_parts(gaps) if g.contains(pt)]
        if not parts:
            near = min(shapely.get_parts(gaps), key=lambda g: g.distance(pt), default=None)
            if near is None:
                return DETOUR_CAP_MM, ""
            parts = [near]
        x0, y0, x1, y1 = parts[0].bounds
        d = 0.5 * max(x1 - x0, y1 - y0)
        if d > DETOUR_CAP_MM:
            return DETOUR_CAP_MM, f"the gap is {2 * d:.3g} mm across; counted as {DETOUR_CAP_MM:g}"
        return max(d, 0.1), ""


def _joining_parts(snap: BoardSnapshot, net_a: str, net_b: str) -> List[Tuple[float, float, str]]:
    """Capacitors with a pad on each of two nets: (x, y, reference)."""
    from kipi.core.model.parts import is_capacitor_ref
    nets: Dict[str, set] = {}
    pos: Dict[str, List[Tuple[float, float]]] = {}
    for p in snap.pads:
        if p.footprint and is_capacitor_ref(p.footprint):
            nets.setdefault(p.footprint, set()).add(p.net)
            pos.setdefault(p.footprint, []).append(p.position)
    out = []
    for ref, ns in nets.items():
        if net_a in ns and net_b in ns:
            xs = [q[0] for q in pos[ref]]
            ys = [q[1] for q in pos[ref]]
            out.append((sum(xs) / len(xs), sum(ys) / len(ys), ref))
    return out


def plane_splits(snap: BoardSnapshot, segs: Sequence[Segment], exclude: Sequence[str] = (),
                 setup=None) -> List[SplitCrossing]:
    """The crossings along ``segs`` (a victim's segments, joined where their ends meet),
    each with its series inductance set on its segment (:attr:`Segment.series_l_h`).
    ``exclude`` are nets whose copper is no reference (the victim's own, the switch
    nodes); ``setup`` gives the capacitor models' ESL for a plane-to-plane crossing."""
    if not segs:
        return []
    planes = _Planes(snap, exclude)
    refs = [planes.reference(s.layer, ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2))
            for s in segs]
    nodes: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}       # node -> [(seg, end)]

    def key(p):
        return (round(p[0] / SNAP_MM), round(p[1] / SNAP_MM))
    for i, s in enumerate(segs):
        nodes.setdefault(key(s.a), []).append((i, 0))
        nodes.setdefault(key(s.b), []).append((i, 1))
    esl = {}
    if setup is not None:
        from kipi.ac.model import caps_dict
        esl = {ref: m.esl_h for ref, m in caps_dict(setup.caps).items()}
    out: List[SplitCrossing] = []
    counted_gaps: set = set()
    for (kx, ky), ends in nodes.items():
        if len(ends) < 2:
            continue
        xy = (kx * SNAP_MM, ky * SNAP_MM)
        pairs = [(i, j) for a, (i, _) in enumerate(ends) for (j, _) in ends[a + 1:]
                 if refs[i] != refs[j]]
        if not pairs:
            continue
        i, j = pairs[0]
        # The crossing's inductance goes on the segment entered: the one whose start is
        # here, else the second of the pair.
        after = j if any(e == 0 and k == j for k, e in ends) else i
        before = i if after == j else j
        s = segs[after]
        if refs[after] is None or refs[before] is None:
            gone, kept = (after, before) if refs[after] is None else (before, after)
            run = _gap_run(segs, refs, nodes, gone, key)
            if run in counted_gaps:
                continue
            counted_gaps.add(run)
            lid = refs[kept][0]
            mid = ((segs[gone].a[0] + segs[gone].b[0]) / 2, (segs[gone].a[1] + segs[gone].b[1]) / 2)
            d, note = planes.gap_detour(lid, mid)
            l_h = slot_inductance(d, s.width_mm)
        elif refs[after][1] != refs[before][1]:
            joins = _joining_parts(snap, refs[before][1], refs[after][1])
            if joins:
                x, y, ref = min(joins, key=lambda q: math.hypot(q[0] - xy[0], q[1] - xy[1]))
                d = max(math.hypot(x - xy[0], y - xy[1]), 0.1)
                extra = esl.get(ref, FALLBACK_ESL_H)
                note = f"through {ref}, {d:.3g} mm away"
                if d > DETOUR_CAP_MM:
                    d, note = DETOUR_CAP_MM, note + f"; counted as {DETOUR_CAP_MM:g} mm"
                l_h = slot_inductance(d, s.width_mm) + extra
            else:
                d = DETOUR_CAP_MM
                note = (f"no capacitor joins {refs[before][1]} and {refs[after][1]}; counted "
                        f"as {DETOUR_CAP_MM:g} mm")
                l_h = slot_inductance(d, s.width_mm)
        else:
            continue                        # the same net on another layer: no split
        s.series_l_h += l_h
        out.append(SplitCrossing(xy[0], xy[1], planes.names.get(s.layer, str(s.layer)),
                                 planes.label(refs[before]), planes.label(refs[after]), d, l_h,
                                 after, note))
    return out


def _gap_run(segs, refs, nodes, start: int, key) -> frozenset:
    """The segments without a reference joined to ``start`` through nodes of two."""
    run = {start}
    todo = [start]
    while todo:
        i = todo.pop()
        for p in (segs[i].a, segs[i].b):
            ends = nodes.get(key(p), [])
            if len(ends) != 2:
                continue
            for j, _ in ends:
                if j not in run and refs[j] is None:
                    run.add(j)
                    todo.append(j)
    return frozenset(run)
