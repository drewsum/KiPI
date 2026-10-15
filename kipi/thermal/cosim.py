"""Electro-thermal co-simulation: alternate the power-tree solve and the board heat map.

Each iteration solves every rail and ground group with copper resistivity at the
current temperature, gathers all the heat on the board (copper and barrel losses of
every group, DC-connection parts, VRM conversion losses and what each load dissipates)
onto one grid, and solves
the board temperature. The user chooses how many iterations to run; the change in
peak temperature per iteration is reported so convergence can be judged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from kipi.model.board import BoardSnapshot
from kipi.pi.model import PISetup
from kipi.pi.tree import TreeResult, solve_tree
from kipi.progress import NULL, ProgressReporter
from kipi.solver import pads_for
from kipi.thermal.model import BoardThermalModel, ThermalSettings


@dataclass
class ThermalResult:
    settings: ThermalSettings
    x: np.ndarray                 # grid cell centers, KiCad mm
    y: np.ndarray
    temps: np.ndarray             # (ny, nx) degC, NaN outside the board
    history: List[dict] = field(default_factory=list)   # per iteration: t_max, dt_max
    heat_w: float = 0.0           # total heat put into the board
    heat_by_kind: Dict[str, float] = field(default_factory=dict)   # copper, vias, loads...
    dielectrics: list = field(default_factory=list)      # (material, mm, Dielectric) used

    def heat_breakdown(self) -> str:
        """"copper 0.12 W, loads 1.4 W, ..." largest first."""
        return ", ".join(f"{k} {w:.3g} W" for k, w in
                         sorted(self.heat_by_kind.items(), key=lambda kv: -kv[1]))

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


class _Heat:
    """Point heat sources gathered by kind, so the total can be broken down."""

    def __init__(self):
        self.xy: List[np.ndarray] = []
        self.w: List[np.ndarray] = []
        self.by_kind: Dict[str, float] = {}

    def add(self, kind: str, pts, watts: float):
        """``watts`` spread evenly over the points ``pts``."""
        pts = np.asarray(pts, dtype=float).reshape(-1, 2)
        if watts <= 0 or len(pts) == 0:
            return
        self.xy.append(pts)
        self.w.append(np.full(len(pts), watts / len(pts)))
        self.by_kind[kind] = self.by_kind.get(kind, 0.0) + watts

    def add_field(self, kind: str, pts: np.ndarray, watts: np.ndarray):
        self.xy.append(pts)
        self.w.append(watts)
        self.by_kind[kind] = self.by_kind.get(kind, 0.0) + float(watts.sum())

    def arrays(self):
        if not self.xy:
            return np.zeros((0, 2)), np.zeros(0)
        return np.vstack(self.xy), np.concatenate(self.w)


def _copper_heat(heat: _Heat, tree: TreeResult, pad_pos: Dict[str, tuple]):
    """Copper and barrel losses of every group, and the DC-connection parts' losses."""
    for g in tree.groups.values():
        r = g.result
        heat.add_field("copper", r.mesh.points[r.mesh.tris].mean(axis=1),
                       np.nan_to_num(r.fields.tri_power))
        for v in r.fields.vias:
            heat.add("vias", [v.position], v.power)
        for e, sr in zip(r.solution.series_elements, r.fields.series):
            link = r.solution.problem.series[e.link]
            heat.add("DC connections",
                     [pad_pos[pid] for pid in link.pads_a + link.pads_b if pid in pad_pos],
                     sr.power)


def _vrm_heat(heat: _Heat, tree: TreeResult, snap: BoardSnapshot, setup: PISetup):
    vrm_by_name = {v.name: v for v in setup.vrms}
    for rep in tree.vrms:
        if rep.p_loss:
            # Each phase's conversion loss sits at its own output pins.
            phases = vrm_by_name[rep.name].all_phases()
            losses = [ph.p_loss for ph in rep.phases] if rep.phases else [rep.p_loss]
            for ph, loss in zip(phases, losses):
                pads = pads_for(snap, ph.output.net, ph.output.ref, ph.output.pins or None)
                heat.add("VRM conversion", [p.position for p in pads], loss)


def _part_body(snap: BoardSnapshot, ref: str, pad_pos: Dict[str, tuple]) -> List[tuple]:
    """Every pad of the part, so its heat spreads over the package, not one pin."""
    for fp in snap.footprints:
        if fp.reference == ref:
            return [pad_pos[pid] for pid in fp.pad_ids if pid in pad_pos]
    return [p.position for p in snap.pads if p.footprint == ref]


def _load_heat(heat: _Heat, tree: TreeResult, snap: BoardSnapshot, setup: PISetup,
               pad_pos: Dict[str, tuple]):
    """What each load turns into heat: its stated dissipation, else the power it takes
    from its supply pins (effective voltage x current)."""
    reports = {r.name: r for r in tree.loads}
    for l in setup.loads:
        rep = reports[l.name]
        watts = (l.dissipation_w if l.dissipation_w is not None
                 else abs(rep.v_effective * rep.current))
        heat.add("loads", _part_body(snap, l.supply.ref, pad_pos), watts)


def heat_sources(tree: TreeResult, snap: BoardSnapshot, setup: PISetup):
    """(xy (N, 2) KiCad mm, watts (N,), watts by kind) for every heat source on the board."""
    pad_pos = {p.id: p.position for p in snap.pads}
    heat = _Heat()
    _copper_heat(heat, tree, pad_pos)
    _vrm_heat(heat, tree, snap, setup)
    _load_heat(heat, tree, snap, setup, pad_pos)
    xy, w = heat.arrays()
    return xy, w, heat.by_kind


def solve_electrothermal(snap: BoardSnapshot, setup: PISetup, thermal: ThermalSettings,
                         progress: ProgressReporter = NULL) -> TreeResult:
    t0 = time.perf_counter()
    with progress.timed("Building the board thermal model"):
        model = BoardThermalModel(snap, thermal, progress)
    temps = np.where(model.inside, thermal.ambient_c, np.nan)
    history: List[dict] = []
    tree: Optional[TreeResult] = None
    meshes: dict = {}                 # the copper does not change between iterations
    heat_total = 0.0
    n = max(1, int(thermal.iterations))
    for it in range(1, n + 1):
        progress.check_cancel()
        progress.stage(f"Electro-thermal iteration {it}/{n}")
        # Iteration 1 solves the copper at ambient, not at the 20 degC reference.
        tree = solve_tree(snap, setup, progress, temperature=model.sampler(temps),
                          mesh_cache=meshes)
        xy, w, by_kind = heat_sources(tree, snap, setup)
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
    tree.thermal = ThermalResult(thermal, model.x, model.y, temps, history, heat_w=heat_total,
                                 heat_by_kind=by_kind, dielectrics=model.dielectrics)
    tree.solve_s = time.perf_counter() - t0
    if n > 1 and history[-1]["dt_max"] > 0.05 * max(history[-1]["t_max"] - thermal.ambient_c, 1e-9):
        tree.warnings.append(f"Electro-thermal solve may not have settled: the last iteration "
                             f"still changed the temperature by {history[-1]['dt_max']:.2f} °C; "
                             "try more iterations")
    return tree
