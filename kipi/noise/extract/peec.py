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


def mutual_matrix(sources: Sequence[Segment], targets: Sequence[Segment]) -> np.ndarray:
    """Partial mutual inductances M[i, j] of every source segment with every target
    segment, henries: :func:`mutual` vectorized over the targets."""
    out = np.zeros((len(sources), len(targets)))
    if not len(sources) or not len(targets):
        return out
    a = np.array([t.a for t in targets])
    b = np.array([t.b for t in targets])
    d = b - a
    lt = np.linalg.norm(d, axis=1)
    ok = lt > 0
    ut = np.zeros_like(d)
    ut[ok] = d[ok] / lt[ok, None]
    rt = np.array([t.radius for t in targets])
    for i, s in enumerate(sources):
        ls = s.length
        if ls <= 0:
            continue
        us = (s.b - s.a) / ls
        dots = ut @ us
        pts = s.a + np.outer(0.5 * (_GL_X + 1.0) * ls, us)         # (G, 3)
        rel = pts[None, :, :] - a[:, None, :]                      # (T, G, 3)
        along = np.einsum("tgk,tk->tg", rel, ut)
        r0 = 0.5 * (s.radius + rt)[:, None]
        rho = np.sqrt(np.maximum(np.einsum("tgk,tgk->tg", rel, rel) - along ** 2, 0.0)
                      + r0 ** 2)
        vals = np.arcsinh((lt[:, None] - along) / rho) + np.arcsinh(along / rho)
        out[i] = MU0_4PI_H_PER_MM * dots * 0.5 * ls * (vals @ _GL_W)
    out[:, ~ok] = 0.0
    return out


def plane_shielding(f: np.ndarray, thickness_mm: float,
                    rho_ohm_mm: float = 1.724e-5) -> np.ndarray:
    """The share of a magnetic field a solid copper plane lets through at ``f`` once it
    mirrors it fully: 1/cosh((1+j)·t/δ), t its thickness and δ the skin depth."""
    delta = np.sqrt(2.0 * rho_ohm_mm / (2 * np.pi * np.asarray(f) * 4e-10 * np.pi))
    return 1.0 / np.cosh((1 + 1j) * thickness_mm / delta)


def plane_response(f: np.ndarray, thickness_mm: float, size_mm: float,
                   rho_ohm_mm: float = 1.724e-5):
    """How a copper plane answers the field of a loop ``size_mm`` across near it:
    (Γ, T), the share it mirrors (the image current's strength) and the share it lets
    through. A thin sheet's eddy currents build up with α = ω·μ0·(t/ρ)·ℓ/2: it is
    transparent while α ≪ 1 and a mirror once α ≫ 1, Γ = jα/(1 + jα); what it lets through,
    1 − Γ, is further cut by the skin effect in thick copper."""
    w = 2 * np.pi * np.asarray(f, float)
    alpha = w * MU0_4PI_H_PER_MM * 4 * np.pi * (thickness_mm / rho_ohm_mm) * size_mm / 2
    gamma = 1j * alpha / (1 + 1j * alpha)
    return gamma, (1 - gamma) * plane_shielding(f, thickness_mm, rho_ohm_mm)


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
