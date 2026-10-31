"""Electro-thermal co-simulation: alternate the power-tree solve and the board heat map.

Each iteration solves every rail and ground group with copper resistivity at the
current temperature, gathers all the heat on the board (copper and barrel losses of
every group, DC-connection parts, VRM conversion losses and, if asked for, the heat the
loads put out) onto one grid, and solves the board temperature, with any edge boundaries
(#221) held at their temperatures. The user chooses how many
iterations to run; the change in peak temperature per iteration is reported so
convergence can be judged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.model import PISetup
from kipi.dc.pi.tree import TreeResult, _group_load_name, solve_tree
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.solver import pads_for
from kipi.dc.thermal.model import BoardThermalModel, LayerTemperatures, ThermalSettings


@dataclass
class ThermalResult:
    settings: ThermalSettings
    x: np.ndarray                 # grid cell centers, KiCad mm
    y: np.ndarray
    temps: np.ndarray             # (copper layers, ny, nx) degC, NaN outside the board
    history: List[dict] = field(default_factory=list)   # per iteration: t_max, dt_max
    heat_w: float = 0.0           # total heat put into the board
    heat_by_kind: Dict[str, float] = field(default_factory=dict)   # copper, vias, loads...
    dielectrics: list = field(default_factory=list)      # (material, mm, Dielectric) used
    load_heat_w: Dict[str, float] = field(default_factory=dict)   # per load, if included
    layers: List[int] = field(default_factory=list)     # copper layer id of each temps layer
    edge_w: List[tuple] = field(default_factory=list)   # (name, degC, W taken) per edge boundary

    def __setstate__(self, state):
        # Results saved before load heat was an option (#62), broken down by kind, or
        # solved per layer: those have one (ny, nx) sheet for every layer.
        self.__dict__.update({"load_heat_w": {}, "heat_by_kind": {}, "layers": [], "edge_w": [],
                              **state})
        if "temps" in state and np.ndim(self.temps) == 2:
            self.temps = np.asarray(self.temps)[None]

    def heat_breakdown(self) -> str:
        """"copper 0.12 W, loads 1.4 W, ..." largest first."""
        return ", ".join(f"{k} {w:.3g} W" for k, w in
                         sorted(self.heat_by_kind.items(), key=lambda kv: -kv[1]))

    def edges_text(self) -> str:
        """"; edges held: Frame at 60 °C takes 1.2 W" (#221), or "" without any."""
        if not self.edge_w:
            return ""
        return "; edges held: " + ", ".join(
            f"{name} at {t:g} °C {'takes' if w >= 0 else 'gives'} {abs(w):.3g} W"
            for name, t, w in self.edge_w)

    @property
    def t_max(self) -> float:
        return float(np.nanmax(self.temps))

    def layer_temps(self, layer: Optional[int] = None) -> np.ndarray:
        """(ny, nx) degC on one copper layer, or the hottest layer at each cell (None, or a
        layer this result does not have)."""
        if layer in self.layers and len(self.layers) == len(self.temps):
            return self.temps[self.layers.index(layer)]
        return np.nanmax(self.temps, axis=0) if len(self.temps) > 1 else self.temps[0]

    def hottest(self, layer: Optional[int] = None):
        """(x, y, degC) of the hottest cell, on one layer or on any."""
        t = self.layer_temps(layer)
        i, j = np.unravel_index(np.nanargmax(t), t.shape)
        return float(self.x[j]), float(self.y[i]), float(t[i, j])

    def peak_layer(self, stackup) -> str:
        """" on F.Cu": the copper layer of the hottest cell, or "" for a result without
        layers."""
        if len(self.layers) != len(self.temps) or len(self.layers) < 2:
            return ""
        k = int(np.unravel_index(np.nanargmax(self.temps), self.temps.shape)[0])
        cu = stackup.layer(self.layers[k])
        return f" on {cu.name}" if cu is not None else ""

    def sampler(self) -> LayerTemperatures:
        """T(xy): the hottest layer at each point, or each point's own with ``on_layers``."""
        return LayerTemperatures(self.x, self.y, self.temps, self.layers,
                                 self.settings.ambient_c)


def load_terminal_names(load) -> List[str]:
    """The solver terminals a setup load became: its supply (one per pin group, if it has
    groups) and its ground return."""
    names = ([_group_load_name(load, i) for i in range(len(load.groups))] if load.groups
             else [load.name])
    return names + ([f"{load.name} (return)"] if load.ground else [])


def load_powers(tree: TreeResult, snap: BoardSnapshot, setup: PISetup) -> Dict[str, tuple]:
    """Per load: (watts, ids of the pads it heats) with watts the sum over every solved supply and
    ground pin of the current it draws times its voltage (#62); the ground pins return
    the current, so this is I x (V supply - V ground) pin by pin. A load without ground
    pins is taken as returning at 0 V. Loads whose
    supply was not solved are left out."""
    on_board = {p.id for p in snap.pads}
    terminals = {}
    for g in tree.groups.values():
        for t in g.result.fields.terminals:
            if t.kind == "load":
                terminals[t.name] = t
    out = {}
    for l in setup.loads:
        found = [terminals[n] for n in load_terminal_names(l) if n in terminals]
        if not any(not t.name.endswith(" (return)") for t in found):
            continue
        watts, pads = 0.0, []
        for t in found:
            pids = getattr(t, "pad_ids", [])
            amps = getattr(t, "i_pads", [])
            if pids and len(amps) == len(pids):
                # Unconnected pins (#88) draw nothing and have no voltage.
                on = [k for k, a in enumerate(amps) if a and not np.isnan(t.v_pads[k])]
                watts += float(sum(amps[k] * t.v_pads[k] for k in on))
                pads += [pids[k] for k in on if pids[k] in on_board]
            else:                       # no per-pin record: the load's current shared equally
                sign = -1.0 if t.name.endswith(" (return)") else 1.0
                watts += sign * t.current * t.v_mean
        out[l.name] = (watts, pads)
    return out


class _Heat:
    """Point heat sources, each on a copper layer, gathered by kind so the total can be
    broken down."""

    def __init__(self, snap: BoardSnapshot):
        self.pads = {p.id: p for p in snap.pads}
        self.holes = {v.id: v.layers for v in snap.vias}
        self.order = {cu.id: i for i, cu in enumerate(snap.stackup.copper)}
        self.xy: List[np.ndarray] = []
        self.w: List[np.ndarray] = []
        self.layer: List[np.ndarray] = []
        self.by_kind: Dict[str, float] = {}

    def add_field(self, kind: str, pts, watts, layers):
        """``watts`` at the points ``pts`` on copper layer ids ``layers`` (per point, or
        one for all; -1: shared by every layer)."""
        pts = np.asarray(pts, dtype=float).reshape(-1, 2)
        self.xy.append(pts)
        self.w.append(np.asarray(watts, dtype=float))
        self.layer.append(np.broadcast_to(np.asarray(layers, dtype=np.int64), (len(pts),)))
        self.by_kind[kind] = self.by_kind.get(kind, 0.0) + float(np.sum(watts))

    def add_layers(self, kind: str, xy, layers: List[int], watts: float):
        """``watts`` at one point, shared equally by ``layers`` (by every layer, if none)."""
        layers = [lid for lid in layers if lid in self.order] or [-1]
        self.add_field(kind, np.tile(np.asarray(xy, dtype=float), (len(layers), 1)),
                       np.full(len(layers), watts / len(layers)), layers)

    def add_pads(self, kind: str, pad_ids, watts: float):
        """``watts`` spread evenly over the pads, each pad's share over its copper layers."""
        pads = [self.pads[pid] for pid in pad_ids if pid in self.pads]
        if watts <= 0 or not pads:
            return
        for p in pads:
            self.add_layers(kind, p.position, p.layers, watts / len(pads))

    def add_hole(self, kind: str, owner_id: str, xy, watts: float):
        """A plated hole's loss, shared by the layers its barrel spans."""
        if watts <= 0:
            return
        layers = self.holes.get(owner_id)
        if layers is None:
            pad = self.pads.get(owner_id)
            layers = pad.layers if pad is not None else []
        span = sorted(self.order[lid] for lid in layers if lid in self.order)
        ids = list(self.order)
        self.add_layers(kind, xy, ids[span[0]:span[-1] + 1] if span else [], watts)

    def arrays(self):
        if not self.xy:
            return np.zeros((0, 2)), np.zeros(0), np.zeros(0, dtype=np.int64)
        return np.vstack(self.xy), np.concatenate(self.w), np.concatenate(self.layer)


def _copper_heat(heat: _Heat, tree: TreeResult):
    """Copper and barrel losses of every group, and the DC-connection parts' losses."""
    for g in tree.groups.values():
        r = g.result
        heat.add_field("copper", r.mesh.points[r.mesh.tris].mean(axis=1),
                       np.nan_to_num(r.fields.tri_power), r.mesh.tri_layer)
        for v in r.fields.vias:
            heat.add_hole("vias", v.owner_id, v.position, v.power)
        for e, sr in zip(r.solution.series_elements, r.fields.series):
            link = r.solution.problem.series[e.link]
            pads = ([p for group in link.groups for p in group] if link.groups
                    else link.pads_a + link.pads_b)         # all of a multi-pin part's
            heat.add_pads("DC connections", pads, sr.power)


def _vrm_heat(heat: _Heat, tree: TreeResult, snap: BoardSnapshot, setup: PISetup):
    vrm_by_name = {v.name: v for v in setup.vrms}
    for rep in tree.vrms:
        if rep.p_loss:
            # Each phase's conversion loss sits at its own output pins.
            phases = vrm_by_name[rep.name].all_phases()
            losses = [ph.p_loss for ph in rep.phases] if rep.phases else [rep.p_loss]
            for ph, loss in zip(phases, losses):
                pads = pads_for(snap, ph.output.net, ph.output.ref, ph.output.pins or None)
                heat.add_pads("VRM conversion", [p.id for p in pads], loss)


def heat_sources(tree: TreeResult, snap: BoardSnapshot, setup: PISetup,
                 thermal: Optional[ThermalSettings] = None):
    """(xy (N, 2) KiCad mm, watts (N,), copper layer id (N,; -1: every layer), watts by
    kind) for every loss on the board, with the heat of each load if the thermal settings
    (default: the setup's) include it."""
    heat = _Heat(snap)
    _copper_heat(heat, tree)
    _vrm_heat(heat, tree, snap, setup)
    if (thermal or setup.thermal).load_power:
        for watts, pads in load_powers(tree, snap, setup).values():
            heat.add_pads("loads", pads, watts)
    xy, w, layers = heat.arrays()
    return xy, w, layers, heat.by_kind


def solve_electrothermal(snap: BoardSnapshot, setup: PISetup, thermal: ThermalSettings,
                         progress: ProgressReporter = NULL) -> TreeResult:
    t0 = time.perf_counter()
    with progress.timed("Building the board thermal model"):
        model = BoardThermalModel(snap, thermal, progress,
                                  setup.settings.conductor_by_layer, setup.settings.via_rules)
    temps = np.where(model.inside, thermal.ambient_c, np.nan)[None].repeat(len(model.g), 0)
    history: List[dict] = []
    tree: Optional[TreeResult] = None
    meshes: dict = {}                 # the copper does not change between iterations
    factors: dict = {}                # each group's last solver; AMG hierarchies are reused
    heat_total = 0.0
    loads_w: Dict[str, float] = {}
    n = max(1, int(thermal.iterations))
    for it in range(1, n + 1):
        progress.check_cancel()
        progress.stage(f"Electro-thermal iteration {it}/{n}")
        # Iteration 1 solves the copper at ambient, not at the 20 degC reference.
        tree = solve_tree(snap, setup, progress, temperature=model.sampler(temps),
                          mesh_cache=meshes, factor_cache=factors)
        xy, w, layers, by_kind = heat_sources(tree, snap, setup, thermal)
        heat = model.empty_heat()
        model.deposit(heat, xy, w, layers)
        heat_total = float(w.sum())
        if thermal.load_power:
            loads_w = {k: v[0] for k, v in load_powers(tree, snap, setup).items()}
            progress.info(f"  loads: {sum(max(v, 0.0) for v in loads_w.values()) * 1e3:.1f} mW "
                          f"({len(loads_w)} load(s))")
        progress.info(f"Solving the board temperature for {heat_total * 1e3:.1f} mW of heat")
        new = model.solve(heat)
        dt = float(np.nanmax(np.abs(new - temps)))
        temps = new
        edge_w = model.edge_heat(temps)
        for name, t, w in edge_w:
            progress.info(f"  edge {name} at {t:g} °C takes {w * 1e3:.1f} mW")
        history.append({"iteration": it, "heat_w": heat_total, "t_max": float(np.nanmax(temps)),
                        "dt_max": dt})
        progress.info(f"Iteration {it}: {heat_total * 1e3:.1f} mW into the board, peak "
                      f"{np.nanmax(temps):.2f} °C (ambient {thermal.ambient_c:g} °C), "
                      f"max change {dt:.3f} °C")
    tree.thermal = ThermalResult(thermal, model.x, model.y, temps, history, heat_w=heat_total,
                                 heat_by_kind=by_kind, dielectrics=model.dielectrics,
                                 load_heat_w=loads_w, layers=model.layers, edge_w=edge_w)
    tree.solve_s = time.perf_counter() - t0
    if n > 1 and history[-1]["dt_max"] > 0.05 * max(history[-1]["t_max"] - thermal.ambient_c, 1e-9):
        tree.warnings.append(f"Electro-thermal solve may not have settled: the last iteration "
                             f"still changed the temperature by {history[-1]['dt_max']:.2f} °C; "
                             "try more iterations")
    return tree
