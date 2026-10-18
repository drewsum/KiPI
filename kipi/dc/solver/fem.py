"""2.5D DC conduction: P1 finite elements on each copper sheet, plated barrels between
layers, voltage sources and current loads on pads.

On a copper sheet of thickness t and resistivity rho the potential obeys
div(G_s grad V) = 0 with sheet conductance G_s = t / rho (siemens per square). A
linear triangle contributes G_s / (4A) * (b b^T + c c^T), where b, c are the usual
shape-function coefficients. A barrel segment between two layers is a resistor of
rho * dz / A, A the copper the hole carries (kipi.dc.solver.via_barrel: the plated wall
of a drill d with plating tp, pi * (d * tp - tp^2), plus a filled core that conducts).

Equipotential regions (a drill ring on one layer, a terminal pad) are merged into a
single unknown, which is exact for "perfect conductor" boundaries and keeps the
matrix well conditioned (no huge penalty conductances). Sources are eliminated as
Dirichlet unknowns, so the reduced system stays symmetric positive definite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components

from kipi.core.mesh.mesher import NetMesh
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.core.solver.spd import SpdSolver
from kipi.dc.solver.setup import REF_TEMP_C, SHORT_OHMS, NetProblem, SolveSettings
from kipi.dc.solver.via_barrel import barrel_copper_area, rule_for

class SolveError(RuntimeError):
    pass


@dataclass
class BarrelElement:
    barrel: int        # index into mesh.barrels
    layer_a: int
    layer_b: int
    node_a: int        # merged node ids
    node_b: int
    g: float           # siemens


@dataclass
class SeriesElement:
    link: int          # index into problem.series
    name: str
    node_a: int        # merged unknowns of side A and side B
    node_b: int
    g: Optional[float]  # siemens; None when the part is a short (sides merged)


@dataclass
class Solution:
    problem: NetProblem
    mesh: NetMesh
    node_v: np.ndarray                  # (N,) volts per mesh node; NaN on floating copper
    merged_of: np.ndarray               # (N,) mesh node -> merged unknown
    v_merged: np.ndarray                # (Nm,) volts per merged unknown
    sheet_g: Dict[int, float]           # layer -> sheet conductance (S/square)
    barrel_elements: List[BarrelElement]
    source_current: Dict[str, float]    # A delivered by each source
    stats: Dict[str, float] = field(default_factory=dict)
    series_elements: List[SeriesElement] = field(default_factory=list)
    series_current: Dict[str, float] = field(default_factory=dict)   # A from side A to B
    tri_rho_factor: Optional[np.ndarray] = None   # rho(T)/rho(20 C) per triangle, if heated
    layer_rho: Dict[int, float] = field(default_factory=dict)   # layer -> rho(20 C), ohm*mm
    warnings: List[str] = field(default_factory=list)
    # The source voltages and load currents this solution was solved with; they can
    # differ from the problem's own (VRM sense correction, power-tree VRM input currents).
    source_voltage: Dict[str, float] = field(default_factory=dict)
    load_current: Dict[str, float] = field(default_factory=dict)
    # A each source delivers at each of its pads, in the source's pad order (#71).
    source_pad_current: Dict[str, List[float]] = field(default_factory=dict)
    # Optional loads' pads with no copper path to a source, which draw nothing (#88).
    dead_pads: Dict[str, List[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def triangle_geometry(points: np.ndarray, tris: np.ndarray):
    """Shape-function coefficients b, c (M,3) and signed doubled area (M,)."""
    x = points[tris, 0]
    y = points[tris, 1]
    b = np.stack([y[:, 1] - y[:, 2], y[:, 2] - y[:, 0], y[:, 0] - y[:, 1]], axis=1)
    c = np.stack([x[:, 2] - x[:, 1], x[:, 0] - x[:, 2], x[:, 1] - x[:, 0]], axis=1)
    two_a = (x * b).sum(axis=1)
    return b, c, two_a


def _merge_groups(n: int, groups: List[np.ndarray]) -> Tuple[np.ndarray, int]:
    """Union equipotential node groups. Returns (merged id per node, count)."""
    rows, cols = [], []
    for g in groups:
        if len(g) > 1:
            rows.append(np.full(len(g) - 1, g[0]))
            cols.append(g[1:])
    if not rows:
        return np.arange(n), n
    r = np.concatenate(rows)
    c = np.concatenate(cols)
    graph = sp.coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n))
    count, labels = connected_components(graph, directed=False)
    return labels, count


def _rho_factor(problem: NetProblem, xy: np.ndarray, tcr=None) -> Optional[np.ndarray]:
    """rho(T) / rho(REF_TEMP_C) at the given points, or None when no temperature. ``tcr`` is
    the temperature coefficient at each point (default: the setup's)."""
    if problem.temperature is None:
        return None
    t = np.asarray(problem.temperature(xy), dtype=float)
    tcr = problem.settings.tcr_per_c if tcr is None else tcr
    return np.maximum(1.0 + tcr * (t - REF_TEMP_C), 0.05)


def _barrel_area(s: SolveSettings, bar) -> float:
    """Copper cross-section of a plated hole, from the via rule that covers it."""
    rule = rule_for(s.via_rules, bar.hole_type, bar.drill_mm)
    if rule is None:
        raise SolveError(f"No via rule covers a {bar.hole_type} hole of {bar.drill_mm:g} mm "
                         "drill; add one in the Mesh tab under Via plating")
    return barrel_copper_area(rule, bar.drill_mm, bar.filled, s.rho_ohm_mm)


def _fill_not_stated(s: SolveSettings, mesh: NetMesh) -> int:
    """Vias whose rule fills them with a conductor but KiCad does not say whether they are
    filled; they are solved unfilled (less copper, more drop)."""
    n = 0
    for bar in mesh.barrels:
        rule = rule_for(s.via_rules, bar.hole_type, bar.drill_mm)
        if bar.kind == "via" and bar.filled is None and rule and rule.fill != "nonconductive":
            n += 1
    return n


def assemble(snap: BoardSnapshot, mesh: NetMesh, problem: NetProblem):
    s = problem.settings
    st = snap.stackup
    sheet_g = {lid: st.layer(lid).thickness_mm / s.rho_for(st.layer(lid).name)
               for lid in mesh.layers}

    # Equipotential groups: drill rings per layer and terminal pads (all their layers).
    groups = [nodes for b in mesh.barrels for nodes in b.ring_nodes.values()]
    pad_groups = {pid: np.concatenate(list(per_layer.values()))
                  for pid, per_layer in mesh.pad_nodes.items()}
    groups.extend(pad_groups.values())

    # Series parts: each side's pads are one node (the part's lead/body ties them); a
    # short (R at or below SHORT_OHMS) merges both sides into one node.
    def side_nodes(pads):
        missing = [p for p in pads if p not in pad_groups]
        if missing:
            raise SolveError(f"Series part pads have no meshed copper: {missing}")
        return np.concatenate([pad_groups[p] for p in pads])
    for link in problem.series:
        a, b = side_nodes(link.pads_a), side_nodes(link.pads_b)
        if link.resistance <= SHORT_OHMS:
            groups.append(np.concatenate([a, b]))
        else:
            groups.extend([a, b])
    merged_of, nm = _merge_groups(mesh.n_nodes, groups)

    b, c, two_a = triangle_geometry(mesh.points, mesh.tris)
    area = np.abs(two_a) / 2
    if np.any(area <= 0):
        raise SolveError("Degenerate triangles in mesh")
    gs = np.array([sheet_g[l] for l in mesh.tri_layer]) if len(set(mesh.tri_layer)) > 1 \
        else np.full(len(mesh.tris), sheet_g[mesh.tri_layer[0]])
    tcr = {lid: s.tcr_for(st.layer(lid).name) for lid in mesh.layers}
    # One value for a single layer (or several alike), else one per triangle.
    tri_tcr = (np.array([tcr[l] for l in mesh.tri_layer]) if len(set(tcr.values())) > 1
               else next(iter(tcr.values()), None))
    rho_factor = _rho_factor(problem, mesh.points[mesh.tris].mean(axis=1), tri_tcr)
    if rho_factor is not None:
        gs = gs / rho_factor
    coef = gs / (4 * area)
    ke = coef[:, None, None] * (b[:, :, None] * b[:, None, :] + c[:, :, None] * c[:, None, :])
    m = merged_of[mesh.tris]
    rows = np.repeat(m, 3, axis=1).ravel()
    cols = np.tile(m, (1, 3)).ravel()
    data = ke.ravel()

    # Barrels: resistors between ring unknowns on consecutive present layers.
    order = {cu.id: i for i, cu in enumerate(st.copper)}
    elements: List[BarrelElement] = []
    for bi, bar in enumerate(mesh.barrels):
        layers = sorted(bar.ring_nodes, key=order.get)
        a_barrel = _barrel_area(s, bar)
        for la, lb in zip(layers[:-1], layers[1:]):
            dz = abs(st.layer(la).z_mid_mm - st.layer(lb).z_mid_mm)
            g = a_barrel / (s.rho_ohm_mm * max(dz, 1e-6))
            if problem.temperature is not None:
                g /= float(_rho_factor(problem, np.asarray([bar.position]))[0])
            na = merged_of[bar.ring_nodes[la][0]]
            nb = merged_of[bar.ring_nodes[lb][0]]
            if na == nb:
                continue   # e.g. a terminal PTH pad already merged across layers
            elements.append(BarrelElement(bi, la, lb, int(na), int(nb), g))
    if elements:
        ea = np.array([e.node_a for e in elements])
        eb = np.array([e.node_b for e in elements])
        eg = np.array([e.g for e in elements])
        rows = np.concatenate([rows, ea, eb, ea, eb])
        cols = np.concatenate([cols, ea, eb, eb, ea])
        data = np.concatenate([data, eg, eg, -eg, -eg])

    series: List[SeriesElement] = []
    for i, link in enumerate(problem.series):
        na = int(merged_of[pad_groups[link.pads_a[0]][0]])
        nb = int(merged_of[pad_groups[link.pads_b[0]][0]])
        if link.resistance <= SHORT_OHMS or na == nb:
            series.append(SeriesElement(i, link.name, na, nb, None))
            continue
        g = 1.0 / link.resistance
        series.append(SeriesElement(i, link.name, na, nb, g))
        rows = np.concatenate([rows, [na, nb, na, nb]])
        cols = np.concatenate([cols, [na, nb, nb, na]])
        data = np.concatenate([data, [g, g, -g, -g]])

    K = sp.coo_matrix((data, (rows, cols)), shape=(nm, nm)).tocsr()
    K.sum_duplicates()
    pad_unknown = {pid: int(merged_of[nodes[0]]) for pid, nodes in pad_groups.items()}
    return K, merged_of, nm, sheet_g, elements, pad_unknown, series, rho_factor


# ---------------------------------------------------------------------------
# Solve
# ---------------------------------------------------------------------------

class LinearSystem:
    """Assembled, factorized system for one problem's copper and terminals.

    Construction does the expensive work (assembly, connectivity checks,
    factorization). :meth:`solve` then takes source voltages and load currents, which
    may change between calls (VRM sense correction, VRM input currents in a power
    tree), and returns a :class:`Solution`.

    ``previous`` is the ``factor`` of a system for the same mesh and terminals with
    slightly different copper resistivity; its factorization or AMG hierarchy is reused
    as the preconditioner (see :class:`SpdSolver`).
    """

    def __init__(self, snap: BoardSnapshot, mesh: NetMesh, problem: NetProblem,
                 progress: ProgressReporter = NULL, previous: Optional[SpdSolver] = None):
        if not problem.sources:
            raise SolveError(f"{problem.net} needs a voltage source (a VRM output)")
        t0 = time.perf_counter()
        self.snap, self.mesh, self.problem = snap, mesh, problem
        self.stats: Dict[str, float] = {}
        self.warnings: List[str] = []
        with progress.timed("Assembling"):
            (self.K, self.merged_of, nm, self.sheet_g, self.elements, self.pad_unknown,
             self.series, self.rho_factor) = assemble(snap, mesh, problem)
            progress.info(f"{nm} unknowns after merging pads and drill rings, "
                          f"{self.K.nnz} non-zeros, {len(self.elements)} barrel segments, "
                          f"{len(self.series)} series part(s)")
        self.nm = nm
        self.layer_rho = {lid: problem.settings.rho_for(snap.stackup.layer(lid).name)
                          for lid in mesh.layers}
        self.stats.update(n_nodes=mesh.n_nodes, n_tris=mesh.n_tris, n_unknowns=nm,
                          nnz=self.K.nnz)
        unstated = _fill_not_stated(problem.settings, mesh)
        if unstated:
            self.warnings.append(f"{unstated} via(s) whose rule has a conducting fill: the board "
                                 "does not say whether they are filled, so they were solved "
                                 "unfilled")

        # Which unknowns are held by which source (structure only; voltages come later).
        self.source_unknowns: Dict[str, List[int]] = {}
        owner: Dict[int, str] = {}
        for src in problem.sources:
            us = sorted({self.pad_unknown[pid] for pid in src.pad_ids})
            for u in us:
                if u in owner and owner[u] != src.name:
                    raise SolveError(f"Sources {owner[u]} and {src.name} are connected "
                                     "directly (same copper node)")
                owner[u] = src.name
            self.source_unknowns[src.name] = us

        # Copper not connected to any source cannot be solved (and carries no current).
        ncomp, comp = connected_components(self.K, directed=False)
        live_comps = {comp[u] for u in owner}
        live = np.isin(comp, list(live_comps))
        # load name -> its pads with no copper path to a source (optional loads only)
        self.dead_pads: Dict[str, List[str]] = {}
        for load in problem.loads:
            dead = [pid for pid in load.pad_ids if not live[self.pad_unknown[pid]]]
            if dead and load.optional:
                self.dead_pads[load.name] = dead
            elif dead:
                srcs = ", ".join(s.name for s in problem.sources)
                raise SolveError(f"Load {load.name} has no copper path to the source ({srcs}) "
                                 f"on {problem.net}. If KiCad shows them connected, the link "
                                 "is probably still unrouted (ratsnest) or goes through an "
                                 "unfilled zone; refill zones and reload. Parts that join "
                                 "nets must be added as DC connections.")
        if int((~live).sum()):
            floating_nodes = int((~live[self.merged_of]).sum())
            self.warnings.append(f"{ncomp - len(live_comps)} copper island(s) "
                                 f"({floating_nodes} mesh nodes) are not connected to a "
                                 "source and were left out")
        self.live = live
        self.is_fixed = np.zeros(nm, dtype=bool)
        self.is_fixed[list(owner)] = True
        self.free = np.flatnonzero(live & ~self.is_fixed)
        self.fixed_idx = np.flatnonzero(self.is_fixed)
        A = self.K[self.free][:, self.free].tocsr()
        self.K_fd = self.K[self.free][:, self.fixed_idx].tocsr()
        with progress.timed("Factorizing"):
            self.factor = SpdSolver(A, problem.settings, progress, self.stats, previous)
        self.setup_s = time.perf_counter() - t0
        for w in self.warnings:
            progress.warn(w)

    def solve(self, voltages: Optional[Dict[str, float]] = None,
              currents: Optional[Dict[str, float]] = None,
              progress: ProgressReporter = NULL) -> Solution:
        """Solve with per-source voltages and per-load currents (defaults: the problem's)."""
        t0 = time.perf_counter()
        prob = self.problem
        volts = {s.name: s.voltage for s in prob.sources}
        volts.update(voltages or {})
        amps = {l.name: l.current for l in prob.loads}
        amps.update(currents or {})

        f = np.zeros(self.nm)
        for load in prob.loads:
            dead = self.dead_pads.get(load.name, ())
            pads = [pid for pid in load.pad_ids if pid not in dead]
            if not pads:                       # not connected at all: draws nothing
                amps[load.name] = 0.0
                continue
            share = amps[load.name] / len(pads)
            for pid in pads:
                f[self.pad_unknown[pid]] -= share

        # Solve for u = V - v_ref rather than V itself. Drops are ~1e-4 of the rail
        # voltage, so with absolute volts the source term would dominate the right-hand
        # side and a relative residual tolerance would say little about the drop.
        v_ref = max(volts.values())
        u_fixed = np.zeros(self.nm)
        for name, us in self.source_unknowns.items():
            u_fixed[us] = volts[name] - v_ref
        rhs = f[self.free] - self.K_fd @ u_fixed[self.fixed_idx]
        with progress.timed("Solving"):
            x = self.factor.solve(rhs, progress)

        u_all = np.full(self.nm, np.nan)
        u_all[self.fixed_idx] = u_fixed[self.fixed_idx]
        u_all[self.free] = x
        v = u_all + v_ref

        # Current each source delivers = net current flowing out of its fixed unknowns.
        # K annihilates constants on each connected piece, so using u avoids cancellation.
        flow = self.K @ np.nan_to_num(u_all) - f
        source_current = {name: float(flow[us].sum())
                          for name, us in self.source_unknowns.items()}
        # Per pad: pads that touch share one unknown, and so share its current.
        source_pad_current = {}
        for src in prob.sources:
            us = [self.pad_unknown[pid] for pid in src.pad_ids]
            source_pad_current[src.name] = [float(flow[u]) / us.count(u) for u in us]
        series_current = {}
        for e in self.series:
            if e.g is None:
                series_current[e.name] = float("nan")   # a short: current not resolved
            else:
                series_current[e.name] = float(e.g * (u_all[e.node_a] - u_all[e.node_b]))
        stats = dict(self.stats)
        stats["solve_s"] = self.setup_s + time.perf_counter() - t0
        return Solution(problem=prob, mesh=self.mesh, node_v=v[self.merged_of],
                        merged_of=self.merged_of, v_merged=v, sheet_g=self.sheet_g,
                        barrel_elements=self.elements, source_current=source_current,
                        stats=stats, warnings=list(self.warnings),
                        series_elements=self.series, series_current=series_current,
                        tri_rho_factor=self.rho_factor, layer_rho=self.layer_rho,
                        source_voltage=volts, load_current=amps,
                        source_pad_current=source_pad_current,
                        dead_pads={k: list(v) for k, v in self.dead_pads.items()})


def solve(snap: BoardSnapshot, mesh: NetMesh, problem: NetProblem,
          progress: ProgressReporter = NULL) -> Solution:
    """One-shot solve with the problem's own source voltages and load currents."""
    return LinearSystem(snap, mesh, problem, progress).solve(progress=progress)
