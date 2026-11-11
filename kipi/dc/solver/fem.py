# Portions Copyright (c) 2026 Tomachie LLC.
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
from kipi.core.temperature import at as temperature_at
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
    voltage_drop: float = 0.0
    # A multi-pin part's piece (#193): the merged unknowns of its side groups and the
    # conductance matrix between them (``g`` is then None and no short is meant).
    nodes: Optional[List[int]] = None
    gmat: Optional[np.ndarray] = None


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
    # The voltage source in series with each part's resistance, side A to B: its fixed
    # drop, or what a diode's operating point needs (#191).
    series_drop: Dict[str, float] = field(default_factory=dict)
    # A tied load's current at each of its pads, in its pad order: what the copper and
    # vias at that pad deliver into it (CurrentLoad.tied).
    load_pad_current: Dict[str, List[float]] = field(default_factory=dict)
    # A packaged load's die voltage (CurrentLoad.pad_ohms): what the part itself sees.
    load_die_v: Dict[str, float] = field(default_factory=dict)


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


def _pads_reaching_a_source(mesh: NetMesh, problem: NetProblem, groups: List[np.ndarray],
                            pad_groups: Dict[str, np.ndarray]) -> Dict[str, bool]:
    """Which pads have a path through copper, vias or series parts to a source's pad."""
    merged, nm = _merge_groups(mesh.n_nodes, groups)
    a = [merged[mesh.tris[:, 0]], merged[mesh.tris[:, 1]], merged[mesh.tris[:, 0]]]
    b = [merged[mesh.tris[:, 1]], merged[mesh.tris[:, 2]], merged[mesh.tris[:, 2]]]
    for bar in mesh.barrels:
        rings = [merged[n[0]] for n in bar.ring_nodes.values()]
        a += [np.asarray(rings[:-1], dtype=np.int64)]
        b += [np.asarray(rings[1:], dtype=np.int64)]
    for link in problem.series:
        sides = link.groups if link.conductance is not None else [link.pads_a, link.pads_b]
        ends = [merged[pad_groups[p[0]][0]] for p in sides if p and p[0] in pad_groups]
        a += [np.asarray(ends[:-1], dtype=np.int64)]
        b += [np.asarray(ends[1:], dtype=np.int64)]
    ra, rb = np.concatenate(a), np.concatenate(b)
    graph = sp.coo_matrix((np.ones(len(ra)), (ra, rb)), shape=(nm, nm))
    _, comp = connected_components(graph, directed=False)
    fed = {comp[merged[pad_groups[p][0]]] for src in problem.sources
           for p in src.pad_ids if p in pad_groups}
    return {pid: bool(comp[merged[nodes[0]]] in fed) for pid, nodes in pad_groups.items()}


def _rho_factor(problem: NetProblem, xy: np.ndarray, tcr=None,
                layers=None) -> Optional[np.ndarray]:
    """rho(T) / rho(REF_TEMP_C) at the given points, on copper layer ids ``layers`` (per
    point, or one for all; None: the hottest layer), or None when no temperature. ``tcr``
    is the temperature coefficient at each point (default: the setup's)."""
    if problem.temperature is None:
        return None
    t = temperature_at(problem.temperature, xy, layers)
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
        if link.conductance is not None:            # each group of sides is one node
            groups.extend(side_nodes(pads) for pads in link.groups)
            continue
        a, b = side_nodes(link.pads_a), side_nodes(link.pads_b)
        if link.resistance <= SHORT_OHMS:
            groups.append(np.concatenate([a, b]))
        else:
            groups.extend([a, b])
    # A load's pads are one node (its package planes join them, CurrentLoad.tied) - only
    # the pads with a copper path to a source: a pad on an island stays unconnected (#88).
    # A load with package resistances (CurrentLoad.pad_ohms) is not merged: its pads join
    # a die node of their own through those resistances (see _die_links).
    packaged: Dict[str, List[Tuple[str, float]]] = {}
    if s.tie_load_pads and any(l.tied and len(l.pad_ids) > 1 for l in problem.loads):
        live_pad = _pads_reaching_a_source(mesh, problem, groups, pad_groups)
        for load in problem.loads:
            pads = [p for p in load.pad_ids if p in pad_groups and live_pad.get(p)]
            if not (load.tied and len(pads) > 1):
                continue
            if load.pad_ohms is not None:
                packaged[load.name] = _package_paths(load, pads)
            else:
                groups.append(np.concatenate([pad_groups[p] for p in pads]))
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
    rho_factor = _rho_factor(problem, mesh.points[mesh.tris].mean(axis=1), tri_tcr,
                             mesh.tri_layer)
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
            if problem.temperature is not None:     # the mean of the two layers' copper
                g /= float(np.mean(_rho_factor(problem, np.asarray([bar.position] * 2),
                                               layers=np.array([la, lb]))))
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
        if link.conductance is not None:
            nodes = [int(merged_of[pad_groups[pads[0]][0]]) for pads in link.groups]
            gm = np.asarray(link.conductance, dtype=float)
            series.append(SeriesElement(i, link.name, nodes[0], nodes[min(1, len(nodes) - 1)],
                                        None, 0.0, nodes, gm))
            rows = np.concatenate([rows, np.repeat(nodes, len(nodes))])
            cols = np.concatenate([cols, np.tile(nodes, len(nodes))])
            data = np.concatenate([data, gm.ravel()])
            continue
        na = int(merged_of[pad_groups[link.pads_a[0]][0]])
        nb = int(merged_of[pad_groups[link.pads_b[0]][0]])
        if link.resistance <= SHORT_OHMS or na == nb:
            series.append(SeriesElement(i, link.name, na, nb, None, link.voltage_drop))
            continue
        g = 1.0 / link.resistance
        series.append(SeriesElement(i, link.name, na, nb, g, link.voltage_drop))
        rows = np.concatenate([rows, [na, nb, na, nb]])
        cols = np.concatenate([cols, [na, nb, nb, na]])
        data = np.concatenate([data, [g, g, -g, -g]])

    pad_unknown = {pid: int(merged_of[nodes[0]]) for pid, nodes in pad_groups.items()}
    dies, nm, (dr, dc, dd) = _die_links(packaged, pad_unknown, nm)
    rows, cols, data = (np.concatenate([rows, dr]), np.concatenate([cols, dc]),
                        np.concatenate([data, dd]))
    K = sp.coo_matrix((data, (rows, cols)), shape=(nm, nm)).tocsr()
    K.sum_duplicates()
    return K, merged_of, nm, sheet_g, elements, pad_unknown, series, rho_factor, dies


@dataclass
class DieNode:
    """A packaged load's die (CurrentLoad.pad_ohms): its unknown, and each live pad's
    unknown and conductance to it."""
    unknown: int
    pads: List[Tuple[str, int, float]]       # (pad id, pad unknown, siemens)


def _package_paths(load, live_pads: List[str]) -> List[Tuple[str, float]]:
    """(pad id, ohms to the die) for the load's live pads."""
    if len(load.pad_ohms) != len(load.pad_ids):
        raise SolveError(f"Load {load.name}: {len(load.pad_ohms)} package resistances for "
                         f"{len(load.pad_ids)} pads")
    ohms = dict(zip(load.pad_ids, load.pad_ohms))
    for pid in live_pads:
        if not ohms[pid] > 0:
            raise SolveError(f"Load {load.name}: pad {pid} has a package resistance of "
                             f"{ohms[pid]} ohm; a package path must be above 0 ohm")
    return [(pid, float(ohms[pid])) for pid in live_pads]


def _die_links(packaged: Dict[str, List[Tuple[str, float]]], pad_unknown: Dict[str, int],
               nm: int):
    """One die unknown per packaged load, after the copper's, and the stiffness entries of
    its pads' package resistances. Pads already one unknown (one pin's several pads, a
    pad sharing a drill ring) are one path, their conductances in parallel."""
    dies: Dict[str, DieNode] = {}
    rows, cols, data = [], [], []
    for name, paths in packaged.items():
        die = nm
        nm += 1
        by_unknown: Dict[int, float] = {}
        for pid, ohm in paths:
            u = pad_unknown[pid]
            by_unknown[u] = by_unknown.get(u, 0.0) + 1.0 / ohm
        pads = [(pid, pad_unknown[pid], 1.0 / ohm) for pid, ohm in paths]
        for u, g in by_unknown.items():
            rows += [u, die, u, die]
            cols += [u, die, die, u]
            data += [g, g, -g, -g]
        dies[name] = DieNode(die, pads)
    return dies, nm, (np.asarray(rows, dtype=np.int64), np.asarray(cols, dtype=np.int64),
                      np.asarray(data, dtype=float))


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
             self.series, self.rho_factor, self.dies) = assemble(snap, mesh, problem)
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
        # Sources may share copper, as offboard supplies on one connector share its ground
        # pins (#219): they must then hold it at the same voltage (checked per solve), and
        # the current through it is split between them (see solve).
        self.source_unknowns: Dict[str, List[int]] = {}
        owner: Dict[int, List[str]] = {}
        for src in problem.sources:
            us = sorted({self.pad_unknown[pid] for pid in src.pad_ids})
            for u in us:
                owner.setdefault(u, []).append(src.name)
            self.source_unknowns[src.name] = us
        self.shared = {u: names for u, names in owner.items() if len(names) > 1}
        for names in {tuple(n) for n in self.shared.values()}:
            progress.info(f"Sources {', '.join(names)} share copper; the current through it "
                          "is split between them")

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
        # Diodes (#191): the free voltages each one's series source moves, per volt. A
        # diode's drop is then found on its own I-V curve (see _diode_drops) without
        # factorizing again.
        self.free_index = {int(node): i for i, node in enumerate(self.free)}
        self.diodes = [e for e in self.series
                       if e.g is not None and problem.series[e.link].curve is not None]
        self.diode_unit = [self.factor.solve(self._drop_rhs({e.link: 1.0}), progress)
                           for e in self.diodes]
        self.diode_v = np.zeros(len(self.diodes))       # the last solve's, to start from
        self.diode_started = False
        self.setup_s = time.perf_counter() - t0
        for w in self.warnings:
            progress.warn(w)

    def solve(self, voltages: Optional[Dict[str, float]] = None,
              currents: Optional[Dict[str, float]] = None,
              progress: ProgressReporter = NULL,
              source_weights: Optional[Dict[str, float]] = None) -> Solution:
        """Solve with per-source voltages and per-load currents (defaults: the problem's).

        Copper that several sources hold carries a current that is theirs together; each
        pad of theirs on it takes a share in proportion to its source's weight in
        ``source_weights`` (default 1: an even share per pad)."""
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
            die = self.dies.get(load.name)
            if die is not None:                # drawn at its die (CurrentLoad.pad_ohms)
                f[die.unknown] -= amps[load.name]
                continue
            share = amps[load.name] / len(pads)
            for pid in pads:
                f[self.pad_unknown[pid]] -= share

        for names in self.shared.values():
            vs = [volts[n] for n in names]
            if max(vs) - min(vs) > 1e-9 * max(1.0, max(abs(v) for v in vs)):
                raise SolveError(f"Sources {' and '.join(names)} share copper but are set to "
                                 f"different voltages ({', '.join(f'{v:g} V' for v in vs)})")

        # Solve for u = V - v_ref rather than V itself. Drops are ~1e-4 of the rail
        # voltage, so with absolute volts the source term would dominate the right-hand
        # side and a relative residual tolerance would say little about the drop.
        v_ref = max(volts.values())
        u_fixed = np.zeros(self.nm)
        for name, us in self.source_unknowns.items():
            u_fixed[us] = volts[name] - v_ref
        rhs = f[self.free] - self.K_fd @ u_fixed[self.fixed_idx]
        # A series part's voltage source is in series with its resistance, oriented from
        # side A to side B; it is added with opposite signs at its terminals.
        drops = {e.link: e.voltage_drop for e in self.series if e.g is not None}
        for e in self.diodes:
            drops[e.link] = 0.0
        rhs += self._drop_rhs(drops)
        with progress.timed("Solving"):
            x = self.factor.solve(rhs, progress)

        u_all = np.full(self.nm, np.nan)
        u_all[self.fixed_idx] = u_fixed[self.fixed_idx]
        u_all[self.free] = x
        if self.diodes:
            for e, vd, unit in zip(self.diodes, self._diode_drops(u_all), self.diode_unit):
                drops[e.link] = vd
                x = x + vd * unit
            u_all[self.free] = x
        v = u_all + v_ref

        # Current each source delivers = net current flowing out of its fixed unknowns.
        # K annihilates constants on each connected piece, so using u avoids cancellation.
        flow = self.K @ np.nan_to_num(u_all) - f
        for e in self.series:
            if e.g is not None and drops[e.link]:
                flow[e.node_a] -= e.g * drops[e.link]
                flow[e.node_b] += e.g * drops[e.link]
        # Per pad: source pads that touch, or that sources share, are one unknown, and so
        # share its current, by weight (evenly where the weights on it are all zero).
        weight = {src.name: 1.0 for src in prob.sources}
        weight.update(source_weights or {})
        held, count = np.zeros(self.nm), np.zeros(self.nm)
        for src in prob.sources:
            for pid in src.pad_ids:
                held[self.pad_unknown[pid]] += weight[src.name]
                count[self.pad_unknown[pid]] += 1
        source_pad_current = {}
        for src in prob.sources:
            us = [self.pad_unknown[pid] for pid in src.pad_ids]
            source_pad_current[src.name] = [
                float(flow[u] * (weight[src.name] / held[u] if held[u] > 0 else 1 / count[u]))
                for u in us]
        source_current = {name: sum(a) for name, a in source_pad_current.items()}
        series_current = {}
        for e in self.series:
            if e.gmat is not None:                      # into side A's group
                series_current[e.name] = float(e.gmat[0] @ np.nan_to_num(u_all[e.nodes]))
            elif e.g is None:
                series_current[e.name] = float("nan")   # a short: current not resolved
            else:
                series_current[e.name] = float(e.g * (u_all[e.node_a] - u_all[e.node_b]
                                                      - drops[e.link]))
        load_pad_current = self._tied_pad_currents(v, amps)
        load_pad_current.update(self._packaged_pad_currents(v))
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
                        load_pad_current=load_pad_current,
                        load_die_v={n: float(v[d.unknown]) for n, d in self.dies.items()},
                        dead_pads={k: list(v) for k, v in self.dead_pads.items()},
                        series_drop={e.name: drops[e.link] for e in self.series
                                     if e.g is not None})

    def _tied_pad_currents(self, v_merged: np.ndarray, amps: Dict[str, float]) -> Dict[str, List[float]]:
        """Each tied load pad's current: the current its copper triangles and the via
        barrels on its rings carry into it. A pad on poor copper, or with no via to the
        plane, carries little; nothing is shared out by count."""
        if not self.problem.settings.tie_load_pads:
            return {}
        tied = [l for l in self.problem.loads if l.tied and len(l.pad_ids) > 1
                and l.name not in self.dies]
        if not tied:
            return {}
        mesh = self.mesh
        node_v = np.nan_to_num(v_merged[self.merged_of])
        # Current from each mesh node into the copper, element by element.
        b, c, two_a = triangle_geometry(mesh.points, mesh.tris)
        area = np.abs(two_a) / 2
        gs = np.array([self.sheet_g[l] for l in mesh.tri_layer])
        if self.rho_factor is not None:
            gs = gs / self.rho_factor
        coef = gs / (4 * area)
        vt = node_v[mesh.tris]
        q = coef[:, None] * (b * (b * vt).sum(axis=1)[:, None] + c * (c * vt).sum(axis=1)[:, None])
        node_flow = np.bincount(mesh.tris.ravel(), weights=q.ravel(), minlength=mesh.n_nodes)
        out: Dict[str, List[float]] = {}
        for load in tied:
            per_pad = []
            for pid in load.pad_ids:
                per_layer = mesh.pad_nodes.get(pid)
                if not per_layer:
                    per_pad.append(0.0)
                    continue
                nodes = np.concatenate(list(per_layer.values()))
                flow = float(node_flow[nodes].sum())
                # Via barrels whose ring on a layer is part of this pad.
                for e in self.elements:
                    bar = mesh.barrels[e.barrel]
                    ra, rb = bar.ring_nodes[e.layer_a], bar.ring_nodes[e.layer_b]
                    i = e.g * (v_merged[e.node_a] - v_merged[e.node_b])
                    if np.isin(ra, nodes).any() and not np.isin(rb, nodes).any():
                        flow += float(i)
                    elif np.isin(rb, nodes).any() and not np.isin(ra, nodes).any():
                        flow -= float(i)
                per_pad.append(-flow)
            total = sum(per_pad)
            want = amps.get(load.name, load.current)
            if want and abs(total - want) > 0.01 * abs(want):
                self.warnings.append(f"{load.name}: its pads' currents sum to {total:.4g} A, "
                                     f"not its {want:.4g} A (a series part on one of its pads?)")
            out[load.name] = per_pad
        return out

    def _packaged_pad_currents(self, v_merged: np.ndarray) -> Dict[str, List[float]]:
        """A packaged load's current at each of its pads, in its pad order: what its own
        package path carries to the die, g * (V_pad - V_die). A pad with no copper path to
        a source carries none."""
        out: Dict[str, List[float]] = {}
        for load in self.problem.loads:
            die = self.dies.get(load.name)
            if die is None:
                continue
            amps = {pid: g * float(v_merged[u] - v_merged[die.unknown]) for pid, u, g in die.pads}
            out[load.name] = [amps.get(pid, 0.0) for pid in load.pad_ids]
        return out

    def pad_resistance(self, pad_ids: List[str],
                       progress: ProgressReporter = NULL) -> Dict[str, float]:
        """Each pad's driving-point resistance to the sources, ohms: the volts it falls
        when one ampere leaves the copper there and nowhere else, the sources held and
        every other terminal open. One back-substitution per pad on the factorization.

        Solved with ``tie_load_pads`` off, it is each ball's own path through the board -
        its pad, its via, the copper to the plane and the plane to the regulator - which
        judges a ball's connection whatever the package does; with the pads tied it is
        their common node's. A pad with no copper path to a source: infinity; a source
        pad: 0."""
        out: Dict[str, float] = {}
        cache: Dict[int, float] = {}
        for pid in pad_ids:
            u = self.pad_unknown[pid]
            if u not in cache:
                if not self.live[u]:
                    cache[u] = float("inf")
                elif self.is_fixed[u]:
                    cache[u] = 0.0
                else:
                    rhs = np.zeros(len(self.free))
                    rhs[self.free_index[u]] = 1.0
                    cache[u] = float(self.factor.solve(rhs, progress)[self.free_index[u]])
            out[pid] = cache[u]
        return out

    def _drop_rhs(self, drops: Dict[int, float]) -> np.ndarray:
        """The right-hand side of series parts' voltage sources, ``drops`` volts by link."""
        rhs = np.zeros(len(self.free))
        for e in self.series:
            vd = drops.get(e.link, 0.0)
            if e.g is None or not vd:
                continue
            if e.node_a in self.free_index:
                rhs[self.free_index[e.node_a]] += e.g * vd
            if e.node_b in self.free_index:
                rhs[self.free_index[e.node_b]] -= e.g * vd
        return rhs

    def _diode_drops(self, u0: np.ndarray) -> List[float]:
        """Each diode's series source for the copper solved with them at 0 V (``u0``).

        The copper is linear, so the voltages across the diodes are U = U0 + S·Vs, with S
        from the unit solves and Vs the sources. Each diode's resistance R carries
        (U - Vs)/R, which must be its current I(U): Newton's method on U, a small dense
        system, then Vs = U - R·I(U)."""
        n = len(self.diodes)

        def across(u_free_or_all, full: bool):
            out = np.empty(n)
            for k, e in enumerate(self.diodes):
                if full:
                    out[k] = u_free_or_all[e.node_a] - u_free_or_all[e.node_b]
                else:
                    a = self.free_index.get(e.node_a)
                    b = self.free_index.get(e.node_b)
                    out[k] = ((u_free_or_all[a] if a is not None else 0.0)
                              - (u_free_or_all[b] if b is not None else 0.0))
            return out

        u_base = across(u0, True)
        s = np.column_stack([across(unit, False) for unit in self.diode_unit])
        g = np.array([e.g for e in self.diodes])
        curves = [self.problem.series[e.link].curve for e in self.diodes]

        def residual(v):
            iv = [c(x) for c, x in zip(curves, v)]
            i = np.array([a for a, _ in iv])
            gd = np.array([b for _, b in iv])
            f = v - u_base - s @ (v - i / g)
            jac = np.eye(n) - s @ (np.eye(n) - np.diag(gd / g))
            return f, jac, i

        v = self.diode_v.copy()
        if not self.diode_started:
            # The first solve starts each diode where it carries what the copper sends
            # through it as a plain resistor, not at 0 V: a steep junction has no slope
            # there, and with fixed-current loads Newton's step then has no direction.
            v = np.array([_volts_at(c, max(ub * gk, 0.0)) for c, ub, gk in
                          zip(curves, u_base, g)])
            self.diode_started = True
        f, jac, i = residual(v)
        for _ in range(300):
            if np.max(np.abs(f)) < 1e-10:
                break
            try:
                step = -np.linalg.solve(jac, f)
            except np.linalg.LinAlgError:
                step = -f
            big = np.max(np.abs(step))
            if big > 0.1:                       # junctions are exponential: small steps
                step *= 0.1 / big
            norm = np.max(np.abs(f))
            for _half in range(30):
                f2, jac2, i2 = residual(v + step)
                if np.max(np.abs(f2)) < norm or big <= 1e-9:
                    break
                step *= 0.5
            v = v + step
            f, jac, i = f2, jac2, i2
        else:
            names = ", ".join(e.name for e in self.diodes)
            raise SolveError(f"No DC operating point for the diode(s) {names} on "
                             f"{self.problem.net}: is one reverse biased, with a load beyond "
                             "it and no other supply?")
        self.diode_v = v
        return list(v - i / g)


def solve(snap: BoardSnapshot, mesh: NetMesh, problem: NetProblem,
          progress: ProgressReporter = NULL) -> Solution:
    """One-shot solve with the problem's own source voltages and load currents."""
    return LinearSystem(snap, mesh, problem, progress).solve(progress=progress)


def _volts_at(curve, amps: float) -> float:
    """The voltage at which the diode ``curve`` (volts -> (amps, slope)) carries ``amps``
    from anode to cathode, by bisection between 0 V and 10 V; 0 V for no current."""
    if amps <= 0:
        return 0.0
    lo, hi = 0.0, 10.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if curve(mid)[0] < amps:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
