"""Steady-state board temperature from copper losses (2D, whole board).

The board is treated as one sheet in the plane: heat spreads sideways through the
copper of every layer (weighted by how much of each cell is copper, on all nets) and
through the laminate, and leaves both faces by convection to ambient:

    div(G_th grad T) - 2 h (T - T_amb) + q'' = 0

G_th (W/K per square) = sum over copper layers of k_cu * t_cu * coverage + sum over
dielectric layers of k(material) * t, with k looked up from each dielectric's material in
the KiCad stackup (see :mod:`kipi.dc.thermal.materials`).
Through the thickness the board is taken as isothermal: for FR4 the through-plane
resistance t/k (~0.005 K m^2/W for 1.6 mm) is about a tenth of the convective 1/(2h),
so every layer sees the same temperature at a point. Heat from all nets and layers
lands on the same grid, so neighboring planes heat each other.

Convection per face: h = 10 + 3.8 v W/(m^2 K), v the airflow speed in m/s (natural
convection plus radiation at v = 0; a flat-plate forced-convection fit above that).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import scipy.sparse as sp
import shapely
from scipy.interpolate import RegularGridInterpolator

from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter

K_CU_W_MM_K = 0.39          # copper, 390 W/(m K)
H_NATURAL_W_M2K = 10.0      # natural convection + radiation, per face
H_PER_M_S = 3.8             # added per m/s of airflow, per face


@dataclass
class ThermalSettings:
    enabled: bool = False
    ambient_c: float = 25.0
    iterations: int = 3
    airflow_m_s: float = 0.0
    grid_mm: float = 0.0        # 0 = automatic (~250 cells across the board)
    laminate_k_w_mk: float = 0.0  # 0 = from the stackup's dielectric materials

    def h_w_mm2_k(self) -> float:
        return (H_NATURAL_W_M2K + H_PER_M_S * max(self.airflow_m_s, 0.0)) * 1e-6


class BoardThermalModel:
    """Grid, conductances and factorization; reused for every heat map."""

    def __init__(self, snap: BoardSnapshot, settings: ThermalSettings,
                 progress: ProgressReporter = NULL):
        self.settings = settings
        st = snap.stackup
        board = _board_geometry(snap)
        minx, miny, maxx, maxy = board.bounds
        span = max(maxx - minx, maxy - miny)
        d = settings.grid_mm if settings.grid_mm > 0 else max(span / 250.0, 0.2)
        nx, ny = max(2, int(math.ceil((maxx - minx) / d))), max(2, int(math.ceil((maxy - miny) / d)))
        self.d = d
        self.x = minx + (np.arange(nx) + 0.5) * d
        self.y = miny + (np.arange(ny) + 0.5) * d
        X, Y = np.meshgrid(self.x, self.y)          # (ny, nx)
        self.inside = shapely.contains_xy(board, X, Y)

        # In-plane sheet conductance per cell: copper coverage of every layer + laminate.
        from kipi.dc.thermal.materials import laminate_conductance
        g_lam, self.dielectrics = laminate_conductance(st, settings.laminate_k_w_mk or None)
        for material, t, info in self.dielectrics:
            note = " (not recognized; assumed FR4)" if info.assumed else ""
            progress.info(f"  dielectric {material or '(unnamed)'} {t:.3f} mm: {info.matched}, "
                          f"k = {info.k_w_mk:g} W/m·K{note}")
            if info.assumed:
                progress.warn(f"Dielectric material {material!r} not recognized; using FR4 "
                              f"({info.k_w_mk:g} W/m·K). Set 'Laminate k' in the Thermal tab "
                              "to override.")
        g = np.full(X.shape, g_lam)
        progress.info(f"  Mapping the copper of {len(st.copper)} layers onto a {nx} x {ny} "
                      "grid...")
        for cu in st.copper:
            copper = _layer_copper(snap, cu.id)
            if copper is None:
                continue
            cov = shapely.contains_xy(copper, X, Y)
            g += cov * K_CU_W_MM_K * cu.thickness_mm
            progress.debug(f"  thermal: {cu.name} copper covers {cov[self.inside].mean():.0%}")
        self.g = g

        idx = -np.ones(X.shape, dtype=np.int64)
        idx[self.inside] = np.arange(int(self.inside.sum()))
        self.idx = idx
        n = int(self.inside.sum())
        rows, cols, vals = [], [], []
        diag = np.zeros(n)
        for di, dj in ((0, 1), (1, 0)):          # right and down neighbors
            a = idx[: ny - di, : nx - dj]
            b = idx[di:, dj:]
            ga = g[: ny - di, : nx - dj]
            gb = g[di:, dj:]
            ok = (a >= 0) & (b >= 0)
            gab = (2 * ga * gb / (ga + gb))[ok]  # series of two half cells, square cells
            ia, ib = a[ok], b[ok]
            rows += [ia, ib]
            cols += [ib, ia]
            vals += [-gab, -gab]
            np.add.at(diag, ia, gab)
            np.add.at(diag, ib, gab)
        self.g_conv = 2.0 * settings.h_w_mm2_k() * d * d   # W/K per cell, both faces
        diag += self.g_conv
        A = sp.coo_matrix((np.concatenate(vals + [diag]),
                           (np.concatenate(rows + [np.arange(n)]),
                            np.concatenate(cols + [np.arange(n)]))), shape=(n, n)).tocsc()
        from kipi.core.solver import direct
        self._solve = direct.factorize(A, progress.warn)
        progress.info(f"Thermal grid {nx} x {ny} ({n} cells inside the board, {d:.2f} mm), "
                      f"h = {settings.h_w_mm2_k() * 1e6:.1f} W/m²K per face")

    # ------------------------------------------------------------------ heat
    def empty_heat(self) -> np.ndarray:
        return np.zeros(self.inside.shape)

    def deposit(self, heat: np.ndarray, xy: np.ndarray, watts: np.ndarray):
        """Add point heat sources (W) at KiCad xy (mm) to the cell containing them."""
        if len(xy) == 0:
            return
        i = np.clip(((xy[:, 1] - self.y[0]) / self.d + 0.5).astype(int), 0, len(self.y) - 1)
        j = np.clip(((xy[:, 0] - self.x[0]) / self.d + 0.5).astype(int), 0, len(self.x) - 1)
        inside = self.inside[i, j]
        # Heat that lands just outside the outline (edge copper) goes to the nearest
        # board cell instead of being lost.
        if not np.all(inside):
            ii, jj = np.nonzero(self.inside)
            from scipy.spatial import cKDTree
            tree = cKDTree(np.column_stack([ii, jj]))
            _, k = tree.query(np.column_stack([i[~inside], j[~inside]]))
            i = i.copy()
            j = j.copy()
            i[~inside], j[~inside] = ii[k], jj[k]
        np.add.at(heat, (i, j), watts)

    def solve(self, heat: np.ndarray) -> np.ndarray:
        """Temperature (degC) per grid cell; NaN outside the board."""
        rise = self._solve(heat[self.inside])
        t = np.full(self.inside.shape, np.nan)
        t[self.inside] = self.settings.ambient_c + rise
        return t

    def sampler(self, temps: np.ndarray) -> Callable[[np.ndarray], np.ndarray]:
        """T(xy) by bilinear interpolation; points off the grid use the nearest value."""
        filled = np.where(np.isnan(temps), self.settings.ambient_c, temps)
        f = RegularGridInterpolator((self.y, self.x), filled, bounds_error=False, fill_value=None)

        def T(xy: np.ndarray) -> np.ndarray:
            xy = np.atleast_2d(xy)
            pts = np.column_stack([np.clip(xy[:, 1], self.y[0], self.y[-1]),
                                   np.clip(xy[:, 0], self.x[0], self.x[-1])])
            return f(pts)
        return T


def _board_geometry(snap: BoardSnapshot):
    polys = [g for g in (poly_to_shapely(p) for p in snap.outline) if g is not None]
    if polys:
        return shapely.union_all(polys)
    # No outline: the bounding box of all copper, with a small margin.
    pts = [pt for p in snap.pads for polys in p.shapes.values() for q in polys for pt in q.outline]
    pts += [t.start for t in snap.tracks] + [t.end for t in snap.tracks]
    pts += [pt for z in snap.zones for polys in z.filled.values() for q in polys for pt in q.outline]
    a = np.asarray(pts)
    return shapely.box(*(a.min(axis=0) - 1.0), *(a.max(axis=0) + 1.0))


def _layer_copper(snap: BoardSnapshot, layer: int):
    """All copper on a layer (every net): zone fills, tracks and pads."""
    parts = []
    for z in snap.zones:
        parts += [g for g in (poly_to_shapely(p) for p in z.filled.get(layer, [])) if g is not None]
    tracks = [t for t in snap.tracks if t.layer == layer]
    if tracks:
        lines = shapely.linestrings([[t.start, t.end] for t in tracks])
        parts += list(shapely.buffer(lines, np.array([t.width_mm for t in tracks]) / 2, quad_segs=2))
    for p in snap.pads:
        parts += [g for g in (poly_to_shapely(q) for q in p.shapes.get(layer, [])) if g is not None]
    if not parts:
        return None
    geom = shapely.union_all(parts)
    shapely.prepare(geom)
    return geom
