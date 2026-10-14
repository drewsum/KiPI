"""Electro-thermal co-simulation: alternate the power-tree solve and the board heat map.

Each iteration solves every rail and ground group with copper resistivity at the
current temperature, gathers all the heat on the board (copper and barrel losses of
every group, DC-connection parts and VRM conversion losses) onto one grid, and solves
the board temperature. The user chooses how many iterations to run; the change in
peak temperature per iteration is reported so convergence can be judged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.model import PISetup
from kipi.dc.pi.tree import TreeResult, solve_tree
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.solver import pads_for
from kipi.dc.thermal.model import BoardThermalModel, ThermalSettings


@dataclass
class ThermalResult:
    settings: ThermalSettings
    x: np.ndarray                 # grid cell centers, KiCad mm
    y: np.ndarray
    temps: np.ndarray             # (ny, nx) degC, NaN outside the board
    history: List[dict] = field(default_factory=list)   # per iteration: t_max, dt_max
    heat_w: float = 0.0           # total heat put into the board
    dielectrics: list = field(default_factory=list)      # (material, mm, Dielectric) used

    @property
    def t_max(self) -> float:
        return float(np.nanmax(self.temps))

    def hottest(self):
        i, j = np.unravel_index(np.nanargmax(self.temps), self.temps.shape)
        return float(self.x[j]), float(self.y[i]), float(self.temps[i, j])

    def sampler(self):
        from scipy.interpolate import RegularGridInterpolator
        filled = np.where(np.isnan(self.temps), self.settings.ambient_c, self.temps)
        f = RegularGridInterpolator((self.y, self.x), filled, bounds_error=False, fill_value=None)

        def T(xy):
            xy = np.atleast_2d(xy)
            return f(np.column_stack([np.clip(xy[:, 1], self.y[0], self.y[-1]),
                                      np.clip(xy[:, 0], self.x[0], self.x[-1])]))
        return T


def heat_sources(tree: TreeResult, snap: BoardSnapshot, setup: PISetup):
    """(xy (N, 2) KiCad mm, watts (N,)) for every loss on the board."""
    xy, w = [], []
    pos = {p.id: p.position for p in snap.pads}
    for g in tree.groups.values():
        r = g.result
        tri_xy = r.mesh.points[r.mesh.tris].mean(axis=1)
        p = np.nan_to_num(r.fields.tri_power)
        xy.append(tri_xy)
        w.append(p)
        for v in r.fields.vias:
            if v.power > 0:
                xy.append(np.asarray([v.position]))
                w.append(np.asarray([v.power]))
        for e, sr in zip(r.solution.series_elements, r.fields.series):
            if sr.power > 0:
                link = r.solution.problem.series[e.link]
                pts = np.asarray([pos[pid] for pid in link.pads_a + link.pads_b if pid in pos])
                if len(pts):
                    xy.append(pts)
                    w.append(np.full(len(pts), sr.power / len(pts)))
    vrm_by_name = {v.name: v for v in setup.vrms}
    for rep in tree.vrms:
        if rep.p_loss and rep.p_loss > 0:
            # Each phase's conversion loss sits at its own output pins.
            phases = vrm_by_name[rep.name].all_phases()
            losses = [ph.p_loss for ph in rep.phases] if rep.phases else [rep.p_loss]
            for ph, loss in zip(phases, losses):
                pads = pads_for(snap, ph.output.net, ph.output.ref, ph.output.pins or None)
                if loss > 0 and pads:
                    pts = np.asarray([p.position for p in pads])
                    xy.append(pts)
                    w.append(np.full(len(pts), loss / len(pts)))
    if not xy:
        return np.zeros((0, 2)), np.zeros(0)
    return np.vstack(xy), np.concatenate(w)


def solve_electrothermal(snap: BoardSnapshot, setup: PISetup, thermal: ThermalSettings,
                         progress: ProgressReporter = NULL) -> TreeResult:
    t0 = time.perf_counter()
    with progress.timed("Building the board thermal model"):
        model = BoardThermalModel(snap, thermal, progress)
    temps = np.where(model.inside, thermal.ambient_c, np.nan)
    history: List[dict] = []
    tree: Optional[TreeResult] = None
    meshes: dict = {}                 # the copper does not change between iterations
    factors: dict = {}                # each group's last solver; AMG hierarchies are reused
    heat_total = 0.0
    n = max(1, int(thermal.iterations))
    for it in range(1, n + 1):
        progress.check_cancel()
        progress.stage(f"Electro-thermal iteration {it}/{n}")
        # Iteration 1 solves the copper at ambient, not at the 20 degC reference.
        tree = solve_tree(snap, setup, progress, temperature=model.sampler(temps),
                          mesh_cache=meshes, factor_cache=factors)
        xy, w = heat_sources(tree, snap, setup)
        heat = model.empty_heat()
        model.deposit(heat, xy, w)
        heat_total = float(w.sum())
        progress.info(f"Solving the board temperature for {heat_total * 1e3:.1f} mW of losses")
        new = model.solve(heat)
        dt = float(np.nanmax(np.abs(new - temps)))
        temps = new
        history.append({"iteration": it, "heat_w": heat_total, "t_max": float(np.nanmax(temps)),
                        "dt_max": dt})
        progress.info(f"Iteration {it}: {heat_total * 1e3:.1f} mW into the board, peak "
                      f"{np.nanmax(temps):.2f} °C (ambient {thermal.ambient_c:g} °C), "
                      f"max change {dt:.3f} °C")
    tree.thermal = ThermalResult(thermal, model.x, model.y, temps, history, heat_total,
                                 model.dielectrics)
    tree.solve_s = time.perf_counter() - t0
    if n > 1 and history[-1]["dt_max"] > 0.05 * max(history[-1]["t_max"] - thermal.ambient_c, 1e-9):
        tree.warnings.append(f"Electro-thermal solve may not have settled: the last iteration "
                             f"still changed the temperature by {history[-1]['dt_max']:.2f} °C; "
                             "try more iterations")
    return tree
