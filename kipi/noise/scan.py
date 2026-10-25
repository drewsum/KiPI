"""Scanning the nets around an aggressor for victims (#69).

Every signal net with a trace within ``scan_radius_mm`` of the aggressor's parts is a
candidate. Each is ranked by a quick estimate of the noise it picks up:

* **capacitive**: its traces' mutual capacitance to the nearest switch-node copper, from
  Maxwell's potential coefficients for two wires a distance d apart at height h over a
  plane, Cm′ = 2πε·p12 / (p11² − p12²) with p11 = ln(2h/a) and p12 = ½·ln(1 + 4h²/d²)
  (a = w/4, the strip's equivalent radius). The edge's current Cm·dV/dt flows through
  the scan's victim type's resistance R (driver and receiver in parallel) and charges the
  net's own capacitance Cg (each trace's 2πε/p11 and the receiver's) through it: over an
  edge of t = ΔV/(dV/dt), v = Cm·R·dV/dt·(1 − e^(−t/τ)), τ = R·(Cm + Cg). A low R gives
  Cm·R·dV/dt; an undriven net (#158) the charge divider ΔV·Cm/(Cm + Cg);
* **inductive**: the EMF the capacitor loops induce along its traces, with the inductive
  model on the parts of each loop carrying most of its current, its peak over a period.

The nets with the highest estimates are then solved in full, as victims of the scan's
type and logic level (:func:`kipi.noise.model.scan_victim`), every mechanism included.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.model import PISetup
from kipi.noise.extract.capacitive import split_tracks
from kipi.noise.extract.inductive import loop_sources, victim_emf

EPS0_F_PER_MM = 8.8541878128e-15


@dataclass
class ScanRow:
    net: str
    estimate_v: float               # the quick estimate's peak
    capacitive_v: float
    inductive_v: float
    distance_mm: float              # closest approach to the aggressor's parts
    solved: bool = False            # solved in full (its VictimResult is in the result)


def aggressor_parts(agg) -> List[str]:
    refs = []
    for ph in agg.phases:
        refs += [r.ref for r in (ph.high_side, ph.low_side) if r]
        refs += [ph.inductor, *ph.input_caps, *ph.output_caps]
    return [r for r in dict.fromkeys(refs) if r]


def candidates(snap: BoardSnapshot, agg, radius_mm: float, exclude: Sequence[str]
               ) -> Dict[str, float]:
    """Signal nets with a trace within ``radius_mm`` of the aggressor's pads: net ->
    closest distance."""
    import shapely
    from kipi.core.model.nets import classify_nets
    refs = set(aggressor_parts(agg))
    pts = [p.position for p in snap.pads if p.footprint in refs]
    if not pts:
        return {}
    area = shapely.MultiPoint(pts).convex_hull
    kinds = classify_nets(snap)
    skip = set(exclude)
    out: Dict[str, float] = {}
    for t in snap.tracks:
        if t.net in skip or kinds.get(t.net) != "signal":
            continue
        d = shapely.LineString([t.start, t.end]).distance(area)
        if d <= radius_mm and d < out.get(t.net, math.inf):
            out[t.net] = float(d)
    return out


def _height_over_plane(stack, layer: int) -> float:
    """The dielectric between a copper layer and its nearer neighbour, mm."""
    lay = stack.layer(layer)
    gaps = []
    for other in stack.copper:
        if other.id == layer:
            continue
        if other.z_top_mm <= lay.z_bottom_mm:
            gaps.append(lay.z_bottom_mm - other.z_top_mm)
        elif other.z_bottom_mm >= lay.z_top_mm:
            gaps.append(other.z_bottom_mm - lay.z_top_mm)
    return min(gaps) if gaps else 0.2


def estimate(snap: BoardSnapshot, setup: PISetup, agg, wf, layouts,
             nets: Dict[str, float]) -> List[ScanRow]:
    """The quick estimate for each candidate net, largest first."""
    import shapely
    from kipi.core.geometry.copper import net_layer_copper
    s = setup.noise
    switch = [ph.switch_net for ph in agg.phases if ph.switch_net]
    sw_by_layer = {}
    for lay in snap.stackup.copper:
        parts = [net_layer_copper(snap, n, lay.id, cut_drills=False) for n in switch]
        parts = [p for p in parts if not p.is_empty]
        if parts:
            sw_by_layer[lay.id] = (lay, shapely.union_all(parts))
    from kipi.noise.model import scan_victim
    n = wf.samples_per_period()
    dvdt = max((float(np.max(np.abs(np.diff(p.switch_node.samples(n)))) * n * wf.fsw_hz)
                for p in wf.phases), default=0.0)
    swing = max((float(np.ptp(p.switch_node.samples(n))) for p in wf.phases), default=0.0)
    t_edge = swing / dvdt if dvdt > 0 else 0.0
    v = scan_victim(s, "")
    r = 1.0 / sum(1.0 / x for x in (v.driver_ohm, v.receiver_ohm, v.termination_ohm) if x)
    sources = loop_sources(layouts, wf, estimate=True)
    freqs = wf.freqs()
    er = setup.ac.default_er
    rows = []
    for net, dist in nets.items():
        segs = split_tracks(snap, net, max(s.segment_mm, 1.0))
        cm = 0.0
        cg = v.receiver_pf * 1e-12
        for seg in segs:
            lay = snap.stackup.layer(seg.layer)
            line = shapely.LineString([seg.a, seg.b])
            h = _height_over_plane(snap.stackup, seg.layer)
            a = max(seg.width_mm / 4, 0.01)
            cg += 2 * math.pi * EPS0_F_PER_MM * er / math.log(max(2 * h / a, 1.01)) \
                * seg.length_mm
            for lay2, geom in sw_by_layer.values():
                dz = abs(lay2.z_mid_mm - lay.z_mid_mm)
                d = math.hypot(line.distance(geom), dz)
                if d > s.window_mm or d <= 0:
                    continue
                p11 = math.log(max(2 * h / a, 1.01))
                p12 = 0.5 * math.log(1 + 4 * h * h / (d * d))
                if p12 < p11:
                    cm += 2 * math.pi * EPS0_F_PER_MM * er * p12 / (p11 ** 2 - p12 ** 2) \
                        * seg.length_mm
        tau = r * (cm + cg)
        v_c = cm * r * dvdt * (-math.expm1(-t_edge / tau)) if tau > 0 else 0.0
        v_l = 0.0
        if sources and segs:
            emf, _ = victim_emf(snap, sources, segs, freqs, net)
            from kipi.noise.waveforms import ifft_periodic
            v_l = float(np.max(np.abs(ifft_periodic(0.0, emf.sum(axis=1), n))))
        rows.append(ScanRow(net, v_c + v_l, v_c, v_l, dist))
    return sorted(rows, key=lambda r: -r.estimate_v)
