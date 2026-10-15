"""Derived quantities from a :class:`~kipi.dc.solver.fem.Solution`.

Units: volts, amps, watts, millimeters. Current density is reported per unit
cross-section (A/mm^2) and is uniform through the copper thickness in this 2.5D
model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from kipi.dc.solver.fem import Solution, triangle_geometry


@dataclass
class TerminalResult:
    name: str
    kind: str                     # "source" or "load"
    current: float                # A (source: delivered, load: drawn)
    v_pads: List[float]           # V at each of its pads
    v_min: float
    v_max: float
    # Per pad (#71): its id, and the A it draws from the copper (negative: delivered).
    # A source's pad currents are solved; a load's current is shared equally by its pads.
    pad_ids: List[str] = field(default_factory=list)
    i_pads: List[float] = field(default_factory=list)
    i_solved: bool = False

    def __setstate__(self, state):
        # Results saved before pin currents were kept (#71).
        self.__dict__.update({"pad_ids": [], "i_pads": [], "i_solved": False, **state})

    @property
    def v_mean(self) -> float:
        return float(np.mean(self.v_pads))


@dataclass
class ViaResult:
    owner_id: str
    kind: str                     # "via" or "pth"
    position: tuple
    drill_mm: float
    current: float                # largest |I| through any of its barrel segments, A
    power: float                  # W dissipated in the barrel


@dataclass
class SeriesResult:
    name: str
    resistance: float             # ohms (0 for a short)
    current: float                # A from side A to side B (NaN for a short)
    v_a: float
    v_b: float
    power: float                  # W dissipated in the part
    pads_a: List[str] = field(default_factory=list)   # pad ids of each side (#71)
    pads_b: List[str] = field(default_factory=list)

    def __setstate__(self, state):
        self.__dict__.update({"pads_a": [], "pads_b": [], **state})


@dataclass
class Fields:
    """Per-triangle and per-via results plus a summary."""
    grad: np.ndarray              # (M, 2) dV/dx, dV/dy in V/mm (KiCad axes, y down)
    j_density: np.ndarray         # (M,) |J| in A/mm^2
    power_density: np.ndarray     # (M,) W/mm^2 of board area
    tri_power: np.ndarray         # (M,) W per triangle
    tri_area: np.ndarray          # (M,) mm^2
    vias: List[ViaResult]
    terminals: List[TerminalResult]
    v_ref: float                  # the reference for drop (below it) and boost (above it)
    summary: Dict[str, float] = field(default_factory=dict)
    series: List[SeriesResult] = field(default_factory=list)

    def drop(self, v: np.ndarray) -> np.ndarray:
        """v_ref - v: positive is a drop, negative a boost."""
        return self.v_ref - v


def compute_fields(sol: Solution, v_ref: Optional[float] = None) -> Fields:
    """``v_ref``: the nominal voltage drop and boost are measured from (default: the
    highest source voltage)."""
    mesh, prob = sol.mesh, sol.problem
    b, c, two_a = triangle_geometry(mesh.points, mesh.tris)
    v = np.nan_to_num(sol.node_v[mesh.tris])          # floating copper -> zero field
    gx = (b * v).sum(axis=1) / two_a
    gy = (c * v).sum(axis=1) / two_a
    grad = np.column_stack([gx, gy])
    g2 = gx * gx + gy * gy
    gs = np.array([sol.sheet_g[l] for l in mesh.tri_layer])
    rho = np.array([sol.layer_rho.get(l, prob.settings.rho_ohm_mm) for l in mesh.tri_layer])
    area = np.abs(two_a) / 2
    factor = sol.tri_rho_factor if sol.tri_rho_factor is not None else 1.0
    gs = gs / factor                                  # sheet conductance at temperature
    j = np.sqrt(g2) / (rho * factor)                  # A/mm^2
    pd = gs * g2                                      # W/mm^2
    tri_power = pd * area

    # Barrels.
    per_barrel: Dict[int, List[float]] = {}
    barrel_power: Dict[int, float] = {}
    total_barrel_power = 0.0
    for e in sol.barrel_elements:
        dv = sol.v_merged[e.node_a] - sol.v_merged[e.node_b]
        if np.isnan(dv):
            continue
        i = e.g * dv
        per_barrel.setdefault(e.barrel, []).append(abs(i))
        p = e.g * dv * dv
        barrel_power[e.barrel] = barrel_power.get(e.barrel, 0.0) + p
        total_barrel_power += p
    vias = []
    for bi, bar in enumerate(mesh.barrels):
        currents = per_barrel.get(bi, [0.0])
        vias.append(ViaResult(bar.owner_id, bar.kind, bar.position, bar.drill_mm,
                              max(currents), barrel_power.get(bi, 0.0)))

    # Terminals.
    def pad_v(pid):
        nodes = next(iter(mesh.pad_nodes[pid].values()))
        return float(sol.node_v[nodes[0]])

    # What the system was actually solved with (a power tree overrides the problem's
    # placeholder currents); solutions pickled before these were kept lack them.
    amps = getattr(sol, "load_current", None) or {}
    volts = getattr(sol, "source_voltage", None) or {}
    terminals = []
    pad_amps = getattr(sol, "source_pad_current", None) or {}
    for src in prob.sources:
        vs = [pad_v(p) for p in src.pad_ids]
        i = sol.source_current[src.name]
        solved = src.name in pad_amps
        per_pad = ([-a for a in pad_amps[src.name]] if solved
                   else [-i / len(src.pad_ids)] * len(src.pad_ids))
        terminals.append(TerminalResult(src.name, "source", i, vs, min(vs), max(vs),
                                        list(src.pad_ids), per_pad, solved))
    for load in prob.loads:
        vs = [pad_v(p) for p in load.pad_ids]
        i = amps.get(load.name, load.current)
        terminals.append(TerminalResult(load.name, "load", i, vs, min(vs), max(vs),
                                        list(load.pad_ids),
                                        [i / len(load.pad_ids)] * len(load.pad_ids)))

    v_src = max(volts.get(s.name, s.voltage) for s in prob.sources)
    if v_ref is None:
        v_ref = v_src
    copper_power = float(tri_power.sum())
    # Power delivered into the copper, measured relative to v_src so it is not the small
    # difference of two large products (sum(I_src) == sum(I_load) is checked separately).
    delivered = sum(t.current * (v_src - t.v_mean) for t in terminals if t.kind == "load") - \
        sum(t.current * (v_src - t.v_mean) for t in terminals if t.kind == "source")
    i_src = sum(t.current for t in terminals if t.kind == "source")
    i_load = sum(t.current for t in terminals if t.kind == "load")
    series = []
    series_power = 0.0
    for e in sol.series_elements:
        link = prob.series[e.link]
        va, vb = float(sol.v_merged[e.node_a]), float(sol.v_merged[e.node_b])
        i = sol.series_current.get(e.name, float("nan"))
        pw = 0.0 if e.g is None else e.g * (va - vb) ** 2
        series_power += pw
        series.append(SeriesResult(e.name, 0.0 if e.g is None else link.resistance, i, va, vb,
                                   pw, list(link.pads_a), list(link.pads_b)))
    loss = copper_power + total_barrel_power + series_power
    live = ~np.isnan(sol.node_v)
    summary = {
        "v_ref": v_ref,
        "v_min": float(np.nanmin(sol.node_v)),
        "v_max": float(np.nanmax(sol.node_v)),
        "max_drop": float(max(v_ref - np.nanmin(sol.node_v), 0.0)),
        "max_boost": float(max(np.nanmax(sol.node_v) - v_ref, 0.0)),
        "j_max": float(j.max()) if len(j) else 0.0,
        "power_copper": copper_power,
        "power_barrels": total_barrel_power,
        "power_loss": loss,
        "power_series": series_power,
        "power_delivered": delivered,
        "energy_balance_error": abs(loss - delivered) / max(abs(delivered), 1e-30),
        "kcl_error": abs(i_src - i_load) / max(abs(i_load), 1e-30),
        "via_current_max": max((x.current for x in vias), default=0.0),
        "source_current_total": i_src,
        "load_current_total": i_load,
        "live_fraction": float(live.mean()),
    }
    return Fields(grad=grad, j_density=j, power_density=pd, tri_power=tri_power,
                  tri_area=area, vias=vias, terminals=terminals, v_ref=v_ref, summary=summary, series=series)
