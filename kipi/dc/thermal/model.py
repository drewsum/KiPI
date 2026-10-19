"""Steady-state board temperature from copper losses, one temperature per copper layer.

Each copper layer is a sheet in the plane: heat spreads sideways through its copper
(weighted by how much of each cell is copper, on all nets) and its share of the laminate,
crosses the laminate to the copper layers above and below, and leaves the two outer
layers through their solder mask and silkscreen by convection to ambient:

    div(G_l grad T_l) + sum over neighbors m of (T_m - T_l) / R_lm
        - (T_l - T_amb) / (1/h + R_coat) [outer layers only] + q''_l = 0

G_l (W/K per square) = k * t * coverage of the layer's conductor (copper unless the
Stackup window says otherwise) + half the in-plane k * t of each dielectric it touches,
k looked up from each dielectric's material in the KiCad stackup (see
:mod:`kipi.dc.thermal.materials`). R_lm (K mm²/W) is the through-plane resistance of the
dielectrics between neighboring copper layers, from each material's through-plane k
(lower than in-plane for glass laminates). Plated holes on every net, vias and component
holes alike, carry heat between the layers they span alongside the laminate: a barrel
conducts k_cu * A / dz, A its copper (and conductive fill) cross-section from the via
rules.

Heat lands on the layer it is made on: a triangle's copper loss on its own layer, a
barrel's loss shared by the layers it spans, a part's heat on the layers of its pads. A
board with one copper layer loses heat from both faces of that layer.

R_coat (K mm²/W) is the solder mask and silkscreen on that face in series, t / k for each
(#82); the silkscreen is taken to cover the whole face. Both are thin and conduct about as
well as the laminate, so they hold heat in only a little.

Convection per face: h = 10 + 3.8 v W/(m^2 K), v the airflow speed in m/s (natural
convection plus radiation at v = 0; a flat-plate forced-convection fit above that).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np
import scipy.sparse as sp
import shapely
from scipy.interpolate import RegularGridInterpolator

from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.conductors import K_CU_W_MK, layer_k_w_mk
from kipi.core.progress import NULL, ProgressReporter

H_NATURAL_W_M2K = 10.0      # natural convection + radiation, per face
H_PER_M_S = 3.8             # added per m/s of airflow, per face


@dataclass
class ThermalSettings:
    enabled: bool = False
    ambient_c: float = 25.0
    iterations: int = 3
    airflow_m_s: float = 0.0
    grid_mm: float = 0.0        # 0 = automatic (~250 cells across the board)
    # Heat the loads themselves: each load dissipates the sum over its supply and ground
    # pins of pin current x pin voltage, spread over those pins (#62).
    load_power: bool = False
    # PASS / FAIL limits on the solved copper's temperature, per layer (None = no limit)
    max_temp_c: Optional[float] = None
    max_rise_c: Optional[float] = None      # above ambient

    def limits_set(self) -> bool:
        return self.max_temp_c is not None or self.max_rise_c is not None

    def temp_passed(self, t_c: float, ambient_c: float) -> Optional[bool]:
        """``t_c`` against the limits, with the rise taken over ``ambient_c``; None
        without limits."""
        if not self.limits_set():
            return None
        return ((self.max_temp_c is None or t_c <= self.max_temp_c)
                and (self.max_rise_c is None or t_c - ambient_c <= self.max_rise_c))

    def h_w_mm2_k(self) -> float:
        return (H_NATURAL_W_M2K + H_PER_M_S * max(self.airflow_m_s, 0.0)) * 1e-6


class LayerTemperatures:
    """Copper temperature from a heat map (layers, ny, nx), bilinear in the plane; points
    off the grid take the nearest value. Called with KiCad xy alone it gives the hottest
    layer at each point; ``on_layers`` reads each point on its own copper layer (see
    :mod:`kipi.core.temperature`). A layer it does not know reads as the hottest."""

    def __init__(self, x: np.ndarray, y: np.ndarray, temps: np.ndarray,
                 layers: Sequence[int], fill: float):
        temps = np.asarray(temps, dtype=float)
        if temps.ndim == 2:                  # one sheet: results saved before layers
            temps = temps[None]
        filled = np.where(np.isnan(temps), fill, temps)
        self._x, self._y = x, y
        self._f = [RegularGridInterpolator((y, x), t, bounds_error=False, fill_value=None)
                   for t in filled]
        self._index = ({int(lid): i for i, lid in enumerate(layers)}
                       if len(layers) == len(filled) else {})

    def _pts(self, xy) -> np.ndarray:
        xy = np.atleast_2d(np.asarray(xy, dtype=float))
        return np.column_stack([np.clip(xy[:, 1], self._y[0], self._y[-1]),
                                np.clip(xy[:, 0], self._x[0], self._x[-1])])

    def _hottest(self, pts: np.ndarray) -> np.ndarray:
        return np.max([f(pts) for f in self._f], axis=0)

    def __call__(self, xy) -> np.ndarray:
        return self._hottest(self._pts(xy))

    def on_layers(self, xy, layers) -> np.ndarray:
        pts = self._pts(xy)
        lay = np.broadcast_to(np.asarray(layers, dtype=np.int64), (len(pts),))
        out = np.empty(len(pts))
        for lid in np.unique(lay):
            sel = lay == lid
            k = self._index.get(int(lid))
            out[sel] = self._f[k](pts[sel]) if k is not None else self._hottest(pts[sel])
        return out


class BoardThermalModel:
    """Grid, conductances and factorization; reused for every heat map."""

    def __init__(self, snap: BoardSnapshot, settings: ThermalSettings,
                 progress: ProgressReporter = NULL,
                 conductor_by_layer: Optional[Dict[str, str]] = None,
                 via_rules=None):
        from kipi.dc.thermal.materials import (
            coating_conductance, coating_k, coating_resistance, laminate_conductance,
            laminate_layers,
        )
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
        self.layers: List[int] = [cu.id for cu in st.copper]     # top to bottom
        self._stack = {lid: i for i, lid in enumerate(self.layers)}

        # In-plane sheet conductance per layer and cell: its copper + its share of laminate.
        _, self.dielectrics = laminate_conductance(st)
        for material, t, info in self.dielectrics:
            note = " (not recognized; assumed FR4)" if info.assumed else ""
            progress.info(f"  dielectric {material or '(unnamed)'} {t:.3f} mm: {info.matched}, "
                          f"k = {info.k_w_mk:g} W/m·K in-plane, {info.k_through:g} through"
                          f"{note}")
            if info.assumed:
                progress.warn(f"Dielectric material {material!r} not recognized; using FR4 "
                              f"({info.k_w_mk:g} W/m·K). Set 'Laminate k' in the Thermal tab "
                              "to override.")
        for c in getattr(st, "coatings", []):
            progress.info(f"  {c.name.lower()} {c.thickness_mm * 1000:.4g} µm: "
                          f"k = {coating_k(c):g} W/m·K")
        g_lam, r_gap = laminate_layers(st)
        nl = len(g_lam)
        g_lam[0] += coating_conductance(st, "top")
        g_lam[-1] += coating_conductance(st, "bottom")
        g = np.array([np.full(X.shape, gl) for gl in g_lam])
        progress.info(f"  Mapping the copper of {len(st.copper)} layers onto a {nx} x {ny} "
                      "grid...")
        for li, cu in enumerate(st.copper):
            copper = _layer_copper(snap, cu.id)
            if copper is None:
                continue
            cov = shapely.contains_xy(copper, X, Y)
            k = layer_k_w_mk(conductor_by_layer or {}, cu.name) * 1e-3     # W/(mm K)
            g[li] += cov * k * cu.thickness_mm
            progress.debug(f"  thermal: {cu.name} copper covers {cov[self.inside].mean():.0%}")
        self.g = g

        # Through the laminate between neighboring layers, and along plated holes.
        gv = np.zeros((nl - 1, ny, nx))
        for li, r in enumerate(r_gap):
            gv[li] = d * d / r
        n_holes = self._add_barrels(gv, snap, via_rules)
        self.g_vertical = gv

        idx = -np.ones(X.shape, dtype=np.int64)
        idx[self.inside] = np.arange(int(self.inside.sum()))
        n_cells = int(self.inside.sum())
        n = n_cells * nl                        # the unknown of layer l in cell c: c * nl + l
        rows, cols, vals = [], [], []
        diag = np.zeros(n)

        def couple(ia, ib, gab):
            rows.extend([ia, ib])
            cols.extend([ib, ia])
            vals.extend([-gab, -gab])
            np.add.at(diag, ia, gab)
            np.add.at(diag, ib, gab)

        for li in range(nl):
            gl = g[li]
            for di, dj in ((0, 1), (1, 0)):          # right and down neighbors
                a = idx[: ny - di, : nx - dj]
                b = idx[di:, dj:]
                ga = gl[: ny - di, : nx - dj]
                gb = gl[di:, dj:]
                ok = (a >= 0) & (b >= 0)
                gab = (2 * ga * gb / (ga + gb))[ok]  # series of two half cells, square cells
                couple(a[ok] * nl + li, b[ok] * nl + li, gab)
        cells = idx[self.inside]
        for li in range(nl - 1):
            couple(cells * nl + li, cells * nl + li + 1, gv[li][self.inside])
        # W/K per cell leaving each layer by convection: the top and bottom faces, each
        # through its coatings.
        h = settings.h_w_mm2_k()
        self.g_conv = np.zeros(nl)
        self.g_conv[0] += d * d / (1 / h + coating_resistance(st, "top"))
        self.g_conv[-1] += d * d / (1 / h + coating_resistance(st, "bottom"))
        diag += np.tile(self.g_conv, n_cells)
        A = sp.coo_matrix((np.concatenate(vals + [diag]),
                           (np.concatenate(rows + [np.arange(n)]),
                            np.concatenate(cols + [np.arange(n)]))), shape=(n, n)).tocsc()
        from kipi.core.solver import direct
        self._solve = direct.factorize(A, progress.warn)
        progress.info(f"Thermal grid {nx} x {ny} x {nl} layer(s) ({n_cells} cells inside the "
                      f"board, {d:.2f} mm), {n_holes} plated hole(s) joining layers, "
                      f"h = {settings.h_w_mm2_k() * 1e6:.1f} W/m²K per face, "
                      f"direct solve ({self._solve.name})")

    def _add_barrels(self, gv: np.ndarray, snap: BoardSnapshot, via_rules) -> int:
        """Add each plated hole's barrel, between every pair of neighboring layers it
        spans, to the cell it is in. Returns how many holes were added."""
        if len(gv) == 0:
            return 0
        from kipi.dc.solver.setup import RHO_CU_OHM_MM
        from kipi.dc.solver.via_barrel import barrel_copper_area, default_via_rules, rule_for
        rules = via_rules if via_rules is not None else default_via_rules()
        z = np.array([cu.z_mid_mm for cu in snap.stackup.copper])
        holes = [(v.position, v.drill_mm, v.via_type, v.filled, v.layers) for v in snap.vias]
        holes += [(p.position, min(x for x in p.drill_mm if x > 0), "pth", False, p.layers)
                  for p in snap.pads if p.pad_type == "pth" and max(p.drill_mm) > 0]
        k_cu = K_CU_W_MK * 1e-3                             # W/(mm K)
        count = 0
        for (px, py), drill, kind, filled, layers in holes:
            span = [self._stack[lid] for lid in layers if lid in self._stack]
            rule = rule_for(rules, kind, drill)
            if len(span) < 2 or rule is None:
                continue
            i = int(np.clip((py - self.y[0]) / self.d + 0.5, 0, len(self.y) - 1))
            j = int(np.clip((px - self.x[0]) / self.d + 0.5, 0, len(self.x) - 1))
            area = barrel_copper_area(rule, drill, filled, RHO_CU_OHM_MM)
            for li in range(min(span), max(span)):
                gv[li, i, j] += k_cu * area / max(z[li] - z[li + 1], 1e-6)
            count += 1
        return count

    # ------------------------------------------------------------------ heat
    def empty_heat(self) -> np.ndarray:
        """Heat (W) per layer, top to bottom, and grid cell."""
        return np.zeros((len(self.g),) + self.inside.shape)

    def deposit(self, heat: np.ndarray, xy: np.ndarray, watts: np.ndarray, layers=None):
        """Add point heat sources (W) at KiCad xy (mm) to the cell containing them, on the
        copper layer id given per point (or one for all). Heat without a layer, or on one
        not in the stackup, is shared equally by every layer."""
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
        if layers is None:
            li = np.full(len(xy), -1)
        else:
            lay = np.broadcast_to(np.asarray(layers, dtype=np.int64), (len(xy),))
            li = np.array([self._stack.get(int(lid), -1) for lid in lay], dtype=np.int64)
        known = li >= 0
        np.add.at(heat, (li[known], i[known], j[known]), watts[known])
        for k in range(len(heat)):
            np.add.at(heat[k], (i[~known], j[~known]), watts[~known] / len(heat))

    def solve(self, heat: np.ndarray) -> np.ndarray:
        """Temperature (degC) per layer and grid cell; NaN outside the board."""
        nl = len(heat)
        rise = self._solve(heat[:, self.inside].T.ravel()).reshape(-1, nl).T
        t = np.full(heat.shape, np.nan)
        t[:, self.inside] = self.settings.ambient_c + rise
        return t

    def sampler(self, temps: np.ndarray) -> LayerTemperatures:
        """T(xy): the hottest layer at each point, or each point's own with ``on_layers``."""
        return LayerTemperatures(self.x, self.y, temps, self.layers, self.settings.ambient_c)


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
