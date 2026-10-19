"""Partial inductances of straight conductor segments (PEEC; Ruehli 1972).

A segment is a straight bar from A to B with a rectangular cross-section w × t. Two
segments' partial mutual inductance is Neumann's integral

    M_ij = μ0/4π · (u_i·u_j) ∫_i ∫_j dl·dl' / R,

whose inner integral along a straight segment is closed-form (asinh), the outer one
Gauss–Legendre. A bar's own cross-section enters as a regularising radius: its geometric
mean distance, a = 0.2235·(w + t), which makes a segment's self term the bar formula
μ0·l/2π·(ln(2l/(w+t)) + 1/2) for l ≫ w + t.

A closed loop's inductance is Σ_ij M_ij over its segments, directions included. A solid
plane near the loop is taken as a perfect conductor by images: each segment mirrored in
the plane, carrying the opposite horizontal current, so L = Σ_ij (M(i, j) - M(i, j')).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

MU0_4PI_H_PER_MM = 1e-10                # μ0/4π = 10⁻⁷ H/m
GMD_FACTOR = 0.2235                     # GMD of a thin rectangle ≈ 0.2235·(w + t)
_GL_X, _GL_W = np.polynomial.legendre.leggauss(24)


@dataclass
class Segment:
    a: np.ndarray                       # (3,) start, mm
    b: np.ndarray                       # (3,) end, mm
    w: float                            # width, mm
    t: float                            # thickness, mm
    label: str = ""

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.b - self.a))

    @property
    def radius(self) -> float:
        return GMD_FACTOR * (self.w + self.t)


def segment(a: Sequence[float], b: Sequence[float], w: float, t: float,
            label: str = "") -> Segment:
    return Segment(np.asarray(a, float), np.asarray(b, float), float(w), float(t), label)


def _inner(p: np.ndarray, a: np.ndarray, b: np.ndarray, r0: float) -> np.ndarray:
    """∫ dl'/R along segment a→b from each point p (n, 3), with R² floored by r0²."""
    d = b - a
    length = np.linalg.norm(d)
    u = d / length
    rel = p - a
    s = rel @ u                                      # position along the segment
    rho2 = np.maximum(np.sum(rel * rel, axis=1) - s * s, 0.0) + r0 * r0
    rho = np.sqrt(rho2)
    return np.arcsinh((length - s) / rho) + np.arcsinh(s / rho)


def mutual(si: Segment, sj: Segment) -> float:
    """Partial mutual inductance of two segments, henries (the self term when si is sj)."""
    li, lj = si.length, sj.length
    if li <= 0 or lj <= 0:
        return 0.0
    ui = (si.b - si.a) / li
    uj = (sj.b - sj.a) / lj
    dot = float(ui @ uj)
    if abs(dot) < 1e-12:
        return 0.0
    s = 0.5 * (_GL_X + 1.0) * li
    pts = si.a + np.outer(s, ui)
    r0 = 0.5 * (si.radius + sj.radius) if si is not sj else si.radius
    vals = _inner(pts, sj.a, sj.b, r0)
    return MU0_4PI_H_PER_MM * dot * 0.5 * li * float(_GL_W @ vals)


def mirror(seg: Segment, z_plane: float) -> Segment:
    """``seg`` reflected in the plane z = z_plane (endpoints only; see loop_inductance)."""
    a, b = seg.a.copy(), seg.b.copy()
    a[2] = 2 * z_plane - a[2]
    b[2] = 2 * z_plane - b[2]
    return Segment(a, b, seg.w, seg.t, seg.label + "'")


def loop_inductance(segs: List[Segment], z_plane: Optional[float] = None) -> float:
    """Inductance of the closed loop the segments form, in order, henries. With
    ``z_plane``, a perfectly conducting plane at that height carries the image currents.

    The image of a current in a conducting plane flows the opposite way horizontally and
    the same way vertically: the reflected segment's direction negated.
    """
    total = 0.0
    images = [mirror(s, z_plane) for s in segs] if z_plane is not None else []
    for i, si in enumerate(segs):
        for j, sj in enumerate(segs):
            total += mutual(si, si) if i == j else mutual(si, sj)
        for sj in images:
            total -= mutual(si, sj)
    return total


def polyline(points: Sequence[Sequence[float]], widths: Sequence[float], t: float,
             labels: Sequence[str] = ()) -> List[Segment]:
    """Segments joining ``points`` in order and closing back to the first; ``widths`` (and
    ``labels``) has one entry per segment."""
    pts = [np.asarray(p, float) for p in points]
    out = []
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        if np.linalg.norm(b - a) > 1e-9:
            out.append(Segment(a, b, float(widths[i]), t, labels[i] if labels else ""))
    return out
