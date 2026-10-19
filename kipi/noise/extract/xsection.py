"""Quasi-static 2D cross-section field solve: per-length capacitance and inductance (#69).

A cut across a victim trace is a 2D problem: conductors are rectangles (the copper each
layer has on the cut line), the dielectrics horizontal bands (each with its εr; air
outside them), and a grounded box closes the domain far from the trace. Laplace's
equation ∇·(ε∇φ) = 0 is solved with P1 triangles (Shewchuk's Triangle), holding each
group of conductors at its own potential: the victim at 1 V, everything else at 0. The
charge each group then carries, per unit length, is its row of the capacitance matrix;
it is read from the stiffness matrix's residual at the group's boundary nodes.

Solved once with the dielectrics and once in vacuum, a cut gives the victim's
capacitance C′, its mutual capacitance to the aggressor Cm′, and its inductance
L′ = μ0·ε0 / C0′ (the TEM relation of a line over its references), so also its
characteristic impedance √(L′/C′) and velocity 1/√(L′C′).

The dielectric interfaces are segments of the mesh, so no triangle straddles two
dielectrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from kipi.ac.solver.physics import EPS0_F_PER_MM, MU0_H_PER_MM

VICTIM, AGGRESSOR, GROUND = "victim", "aggressor", "ground"
# Mesh grading: element size grows by this share of the distance from the victim and
# aggressor, over this many refinement passes.
GRADE = 0.3
REFINE_PASSES = 2


@dataclass(frozen=True)
class Rect:
    """A conductor's cross-section: x across the cut, z up, mm."""
    x0: float
    x1: float
    z0: float
    z1: float
    group: str = GROUND


@dataclass(frozen=True)
class Band:
    """A dielectric layer between z0 and z1."""
    z0: float
    z1: float
    er: float


@dataclass
class CrossSectionResult:
    c_f_per_mm: float               # victim to everything else (C′)
    cm: Dict[str, float]            # victim to each aggressor group (Cm′), ≥ 0
    c0_f_per_mm: float              # C′ in vacuum
    triangles: int = 0

    @property
    def cm_f_per_mm(self) -> float:
        """To every aggressor together."""
        return float(sum(self.cm.values()))

    @property
    def l_h_per_mm(self) -> float:
        return MU0_H_PER_MM * EPS0_F_PER_MM / self.c0_f_per_mm

    @property
    def z0_ohm(self) -> float:
        return float(np.sqrt(self.l_h_per_mm / self.c_f_per_mm))

    @property
    def er_eff(self) -> float:
        return self.c_f_per_mm / self.c0_f_per_mm


def _ring(x0, z0, x1, z1, step: Optional[float]):
    """A rectangle's outline, with points every ``step`` along its edges."""
    from shapely.geometry import LineString
    pts = []
    for (ax, az), (bx, bz) in (((x0, z0), (x1, z0)), ((x1, z0), (x1, z1)),
                               ((x1, z1), (x0, z1)), ((x0, z1), (x0, z0))):
        n = max(1, int(np.ceil(max(abs(bx - ax), abs(bz - az)) / step))) if step else 1
        for k in range(n):
            t = k / n
            pts.append((ax + (bx - ax) * t, az + (bz - az) * t))
    pts.append(pts[0])
    return LineString(pts)


def _distance(xy: np.ndarray, rects: Sequence[Rect]) -> np.ndarray:
    """Each point's distance to the nearest of ``rects``."""
    d = np.full(len(xy), np.inf)
    for r in rects:
        dx = np.maximum(np.maximum(r.x0 - xy[:, 0], xy[:, 0] - r.x1), 0.0)
        dz = np.maximum(np.maximum(r.z0 - xy[:, 1], xy[:, 1] - r.z1), 0.0)
        d = np.minimum(d, np.hypot(dx, dz))
    return d


def mesh(rects: Sequence[Rect], bands: Sequence[Band], box: Tuple[float, float, float, float],
         fine_mm: float, coarse_mm: float, grade: float = GRADE):
    """Triangles of the box minus the conductors, graded from ``fine_mm`` at the victim's
    and aggressor's copper to ``coarse_mm``: the field is singular at their corners, and a
    uniform mesh converges slowly there."""
    import triangle
    from shapely.geometry import LineString
    from kipi.core.mesh.mesher import _linework
    bx0, bx1, bz0, bz1 = box
    active = [r for r in rects if r.group != GROUND]
    lines = [_ring(bx0, bz0, bx1, bz1, coarse_mm)]
    for b in bands:
        for z in (b.z0, b.z1):
            if bz0 < z < bz1:
                lines.append(LineString([(bx0, z), (bx1, z)]))
    for r in rects:
        lines.append(_ring(max(r.x0, bx0), r.z0, min(r.x1, bx1), r.z1,
                           fine_mm if r.group != GROUND else coarse_mm))
    verts, segs = _linework(lines)
    holes = [((max(r.x0, bx0) + min(r.x1, bx1)) / 2, (r.z0 + r.z1) / 2) for r in rects]
    data = {"vertices": verts, "segments": segs}
    if holes:
        data["holes"] = np.asarray(holes)
    out = triangle.triangulate(data, f"pq28a{coarse_mm ** 2 * 0.5:.6g}")
    for _ in range(REFINE_PASSES):
        cent = out["vertices"][out["triangles"]].mean(axis=1)
        size = np.clip(fine_mm + grade * _distance(cent, active), fine_mm, coarse_mm)
        out = triangle.triangulate(
            {**{k: out[k] for k in ("vertices", "triangles", "segments") if k in out},
             **({"holes": data["holes"]} if holes else {}),
             "triangle_max_area": 0.43 * size ** 2}, "rpq28a")
    return out["vertices"], out["triangles"]


def _eps(cent_z: np.ndarray, bands: Sequence[Band], vacuum: bool) -> np.ndarray:
    eps = np.full(len(cent_z), EPS0_F_PER_MM)
    if vacuum:
        return eps
    for b in bands:
        eps[(cent_z > b.z0) & (cent_z < b.z1)] = EPS0_F_PER_MM * b.er
    return eps


def _stiffness(p: np.ndarray, t: np.ndarray, eps: np.ndarray):
    from scipy.sparse import coo_matrix
    x, z = p[t, 0], p[t, 1]
    b = np.stack([z[:, 1] - z[:, 2], z[:, 2] - z[:, 0], z[:, 0] - z[:, 1]], axis=1)
    c = np.stack([x[:, 2] - x[:, 1], x[:, 0] - x[:, 2], x[:, 1] - x[:, 0]], axis=1)
    area = 0.5 * np.abs(b[:, 0] * c[:, 1] - b[:, 1] * c[:, 0])
    ke = (b[:, :, None] * b[:, None, :] + c[:, :, None] * c[:, None, :]) \
        * (eps / (4 * area))[:, None, None]
    rows = np.repeat(t, 3, axis=1).ravel()
    cols = np.tile(t, (1, 3)).ravel()
    n = len(p)
    return coo_matrix((ke.ravel(), (rows, cols)), shape=(n, n)).tocsr()


def _groups(p: np.ndarray, rects: Sequence[Rect], box, tol: float,
            order: Dict[str, int]) -> np.ndarray:
    """Each node's conductor group index (``order``), -1 for a free node."""
    g = np.full(len(p), -1)
    bx0, bx1, bz0, bz1 = box
    on_box = ((np.abs(p[:, 0] - bx0) < tol) | (np.abs(p[:, 0] - bx1) < tol)
              | (np.abs(p[:, 1] - bz0) < tol) | (np.abs(p[:, 1] - bz1) < tol))
    g[on_box] = order[GROUND]
    for r in rects:
        inside = ((p[:, 0] > r.x0 - tol) & (p[:, 0] < r.x1 + tol)
                  & (p[:, 1] > r.z0 - tol) & (p[:, 1] < r.z1 + tol))
        g[inside] = order[r.group]
    return g


def solve(rects: Sequence[Rect], bands: Sequence[Band],
          box: Tuple[float, float, float, float],
          fine_mm: Optional[float] = None) -> CrossSectionResult:
    """C′, Cm′ and C0′ of the victim in a cross-section."""
    from scipy.sparse.linalg import splu
    victims = [r for r in rects if r.group == VICTIM]
    if not victims:
        raise ValueError("cross-section has no victim conductor")
    w = min(r.x1 - r.x0 for r in victims)
    fine = fine_mm or float(np.clip(w / 30, 0.001, 0.02))
    coarse = max((box[1] - box[0]) / 40, 4 * fine)
    p, t = mesh(rects, bands, box, fine, coarse)
    names = [VICTIM, GROUND] + sorted({r.group for r in rects} - {VICTIM, GROUND})
    order = {n: i for i, n in enumerate(names)}
    groups = _groups(p, rects, box, 1e-6, order)
    free = np.flatnonzero(groups < 0)
    fixed = np.flatnonzero(groups >= 0)
    phi_d = (groups[fixed] == 0).astype(float)             # the victim at 1 V
    cent_z = p[t, 1].mean(axis=1)
    out = []
    for vacuum in (False, True):
        k = _stiffness(p, t, _eps(cent_z, bands, vacuum))
        kff = k[free][:, free].tocsc()
        rhs = -(k[free][:, fixed] @ phi_d)
        phi = np.zeros(len(p))
        phi[fixed] = phi_d
        phi[free] = splu(kff).solve(rhs)
        q = k @ phi                                       # charge at each node
        out.append([float(q[groups == i].sum()) for i in range(len(names))])
    (c, *rest), (c0, *_) = out
    cm = {n: max(-rest[i - 1], 0.0) for i, n in enumerate(names) if i >= 2}
    return CrossSectionResult(c, cm, c0, len(t))


class Cache:
    """Cross-sections already solved, by their geometry rounded to ``grid_mm``."""

    def __init__(self, grid_mm: float = 0.01, size: int = 4096):
        self.grid = grid_mm
        self.size = size
        self._d: Dict[tuple, CrossSectionResult] = {}
        self.hits = self.misses = 0

    def key(self, rects: Iterable[Rect], bands: Iterable[Band], box) -> tuple:
        q = self.grid

        def r(v):
            return round(v / q)
        return (tuple(sorted((r(x.x0), r(x.x1), round(x.z0, 6), round(x.z1, 6), x.group)
                             for x in rects)),
                tuple((round(b.z0, 6), round(b.z1, 6), round(b.er, 4)) for b in bands),
                tuple(r(v) for v in box))

    def solve(self, rects: List[Rect], bands: List[Band], box) -> CrossSectionResult:
        k = self.key(rects, bands, box)
        hit = self._d.get(k)
        if hit is not None:
            self.hits += 1
            return hit
        self.misses += 1
        res = solve(rects, bands, box)
        if len(self._d) >= self.size:
            self._d.pop(next(iter(self._d)))
        self._d[k] = res
        return res
