"""Solve a whole power tree defined in a :class:`~kipi.dc.pi.model.PISetup`.

Each net group (nets joined by DC connections) is one FEM problem driven by its single
VRM. Groups are solved downstream first: a VRM's input current depends on what its
output delivers, and its input pins are a load on the group feeding it.

* LDO:       I_in = I_out + I_q
* Switcher:  I_in = V_out * I_out / (efficiency * V_in) + I_q, where V_in is the voltage
             at the VRM's input pins, which depends on I_in, so the feeding group is
             iterated to a fixed point (the factorized system is reused).
* Remote sense: the VRM's output is corrected until the sense pins sit at the set point.
  With ground sense, the set point is held above the ground sense pins instead of 0 V;
  their voltage comes from the ground solve, so rails and grounds are iterated together.
* Ground: groups used as ground are referenced to 0 V at the source VRM's ground pins.
  Loads return their current at their ground pins; every other VRM returns
  I_in - I_out at its ground pins.
* Multi-phase VRMs: every phase carries I_out / N. Phase 1's output pins hold the VRM's
  output voltage; phases 2..N inject their share, so each phase settles at whatever
  voltage its copper gives it. The VRM regulates its sense pins or, without them, the
  mean of its phase outputs. Each phase draws its own input current (from its own
  output and input voltages) and returns I_out / N - I_in,phase at its ground pins; a
  source VRM's reference is phase 1's ground.

After the solve each VRM's input is checked against its dropout and input range. A VRM
that cannot hold its set point fails every load it feeds, directly or downstream; a
group whose iteration did not converge leaves its loads unjudged.
"""

from __future__ import annotations

import time
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from kipi.core.mesh.mesher import mesh_nets
from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.model import Load, PISetup, PinRef, Vrm, net_groups, validate
from kipi.dc.pi.results import (  # also importable from here, where they were defined
    NOT_CONVERGED,
    OUT_OF_REGULATION,
    GroupResult,
    LoadReport,
    PhaseReport,
    TreeResult,
    VrmReport,
    drop_or_boost,  # noqa: F401 - re-exported for kipi.dc.pi.report and the GUI
    fmt_drop,  # noqa: F401
)
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.solver import NetResult, pads_for
from kipi.dc.solver.fem import LinearSystem, SolveError
from kipi.dc.solver.post import compute_fields
from kipi.dc.solver.setup import CurrentLoad, NetProblem, SeriesLink, VoltageSource

MAX_TREE_ITERATIONS = 50
V_TOL = 1e-9

Group = frozenset


class _Pads:
    def __init__(self, snap: BoardSnapshot):
        self.snap = snap

    def __call__(self, ref: PinRef) -> List[str]:
        return [p.id for p in pads_for(self.snap, ref.net, ref.ref, ref.pins or None)]


def _pad_voltages(sol, pad_ids: List[str]) -> np.ndarray:
    return np.array([sol.node_v[next(iter(sol.mesh.pad_nodes[p].values()))[0]]
                     for p in pad_ids])


def _mean_v(sol, pad_ids: List[str]) -> float:
    return float(np.mean(_pad_voltages(sol, pad_ids)))


def _group_load_name(load: Load, index: int) -> str:
    g = load.groups[index]
    return f"{load.name} [{g.name or ','.join(g.pins)}]"


def _input_current(v: Vrm, v_out: float, i_out: float, v_in: float) -> float:
    """Input current of one phase delivering ``i_out`` at ``v_out`` from ``v_in``."""
    iq = v.quiescent_a / (len(v.phases) + 1)
    if v.kind == "ldo":
        return i_out + iq
    if v.kind == "smps":
        if v_in <= 0:
            raise SolveError(f"VRM {v.name}: input voltage collapsed to {v_in:.3f} V")
        return v_out * i_out / (v.efficiency * v_in) + iq
    return 0.0


def _term(v: Vrm, what: str, k: int) -> str:
    """Name of a VRM terminal in a net problem; ``k`` is the 1-based phase."""
    return f"{v.name} ({what})" if not v.phases else f"{v.name} ({what}, phase {k})"


class _Rail:
    """The terminals of one rail group: its VRM's phases, sense pins and the inputs of the
    VRMs it feeds."""

    def __init__(self, vrm: Vrm, children: List[Vrm], pads: _Pads):
        self.vrm, self.children = vrm, children
        self.sense_pads = pads(vrm.sense) if vrm.sense else []
        # Phase 1 is the voltage source; phases 2..N inject their share of the demand.
        self.phase_pads = [pads(ph.output) for ph in vrm.all_phases()]
        self.phase_load = [_term(vrm, "output", k) for k in range(2, len(self.phase_pads) + 1)]
        self.child_load = {c.name: [_term(c, "input", k) for k in range(1, len(c.phases) + 2)]
                           for c in children}
        self.child_pads = {c.name: [pads(ph.input) for ph in c.all_phases()]
                           for c in children}

    @property
    def n_phases(self) -> int:
        return len(self.phase_pads)

    def terminal_loads(self) -> List[CurrentLoad]:
        loads = [CurrentLoad(name, pp, 0.0) for c in self.children
                 for name, pp in zip(self.child_load[c.name], self.child_pads[c.name])]
        if self.sense_pads:
            loads.append(CurrentLoad(f"{self.vrm.name} (sense)", self.sense_pads, 0.0))
        loads += [CurrentLoad(name, pp, 0.0)
                  for name, pp in zip(self.phase_load, self.phase_pads[1:])]
        return loads

    def regulated(self, sol) -> Optional[float]:
        """The voltage the VRM regulates, or None when it holds its output pins directly."""
        if self.sense_pads:
            return _mean_v(sol, self.sense_pads)
        if self.n_phases > 1:
            return float(np.mean([_mean_v(sol, pp) for pp in self.phase_pads]))
        return None


class _TreeSolver:
    """One solve of a validated setup. Each method is one step of :func:`solve_tree`."""

    def __init__(self, snap: BoardSnapshot, setup: PISetup, progress: ProgressReporter,
                 temperature, mesh_cache: Optional[dict],
                 factor_cache: Optional[dict] = None):
        errors = validate(setup)
        if errors:
            raise SolveError("Setup problems:\n- " + "\n- ".join(errors))
        self.snap, self.setup, self.progress = snap, setup, progress
        self.temperature, self.mesh_cache = temperature, mesh_cache
        self.factor_cache = factor_cache
        self.pads = _Pads(snap)
        self.groups = net_groups(setup)
        self.driver: Dict[Group, Vrm] = {self.groups[v.output.net]: v for v in setup.vrms}
        self.fed_by: Dict[Group, List[Vrm]] = {}     # rail group -> VRMs whose input is in it
        for v in setup.vrms:
            if v.input:
                self.fed_by.setdefault(self.groups[v.input.net], []).append(v)
        self.pad_count: Dict[str, int] = {}
        for p in snap.pads:
            self.pad_count[p.net] = self.pad_count.get(p.net, 0) + 1
        self.order = self._downstream_first()
        self.ground_groups = self._ground_groups()
        self.built: Dict[Group, Tuple[object, LinearSystem]] = {}

        # Per VRM, one entry per phase.
        self.v_out: Dict[str, List[float]] = {}
        self.i_out: Dict[str, List[float]] = {}
        self.v_in: Dict[str, List[float]] = {}
        self.i_in: Dict[str, List[float]] = {}
        self.v_sense: Dict[str, float] = {}
        # Ground-sensing VRMs regulate against their ground sense pins, whose voltage comes
        # from the ground solve, which depends on the rails' currents: iterate both to a
        # fixed point (passes after the first only re-solve the factorized systems).
        self.v_gsense: Dict[str, float] = {v.name: 0.0 for v in setup.vrms if v.ground_sense}
        self.rail_solutions: Dict[Group, object] = {}
        self.ground_solutions: Dict[Group, object] = {}
        self.not_converged: set = set()              # groups whose own iteration failed
        self.tree_converged = True                   # the rails/grounds outer iteration

    # ------------------------------------------------------------ structure
    def label_of(self, g: Group) -> str:
        """Short display name: the group's busiest net, plus how many nets it joins."""
        main = max(sorted(g), key=lambda n: self.pad_count.get(n, 0))
        return main if len(g) == 1 else f"{main} (+{len(g) - 1} net{'s' if len(g) > 2 else ''})"

    def _downstream_first(self) -> List[Group]:
        """A group comes after every group its VRM inputs feed."""
        order: List[Group] = []
        seen = set()

        def visit(g):
            if g in seen:
                return
            seen.add(g)
            for child in self.fed_by.get(g, []):
                visit(self.groups[child.output.net])
            order.append(g)
        for g in self.driver:
            visit(g)
        return order

    def _ground_groups(self) -> List[Group]:
        out: List[Group] = []
        refs = [v.ground for v in self.setup.vrms] + [l.ground for l in self.setup.loads]
        for ref in refs:
            if ref and self.groups[ref.net] not in out:
                out.append(self.groups[ref.net])
        return out

    def _fed_from(self, g: Group) -> Iterator[Group]:
        """``g`` and every group powered through it."""
        yield g
        for child in self.fed_by.get(g, []):
            yield from self._fed_from(self.groups[child.output.net])

    # ------------------------------------------------------------ systems
    def _links_in(self, g: Group) -> List[SeriesLink]:
        return [SeriesLink(k.name, self.pads(k.side_a), self.pads(k.side_b), k.resistance)
                for k in self.setup.links if k.side_a.net in g]

    def _system_for(self, g: Group, sources, loads, label):
        """Mesh and factorize a group once; later passes only re-solve."""
        if g in self.built:
            return self.built[g]
        s = self.setup.settings
        nets = sorted(g)
        prob = NetProblem(label, sources, loads, s, series=self._links_in(g), nets=nets,
                          temperature=self.temperature)
        key = (tuple(nets), tuple(sorted(prob.terminal_pad_ids())), s.max_edge_mm, s.grading,
               s.min_edge_mm, s.circle_quad_segs, s.simplify_mm, s.min_angle_deg)
        mesh = self.mesh_cache.get(key) if self.mesh_cache is not None else None
        if mesh is None:
            with self.progress.timed(f"Meshing {label}"):
                mesh = mesh_nets(self.snap, nets, prob.terminal_pad_ids(), name=label,
                                 max_edge_mm=s.max_edge_mm, min_angle_deg=s.min_angle_deg,
                                 simplify_mm=s.simplify_mm, circle_quad_segs=s.circle_quad_segs,
                                 min_edge_mm=s.min_edge_mm, grading=s.grading,
                                 progress=self.progress)
            if self.mesh_cache is not None:
                self.mesh_cache[key] = mesh
        previous = self.factor_cache.get(key) if self.factor_cache is not None else None
        system = LinearSystem(self.snap, mesh, prob, self.progress, previous=previous)
        if self.factor_cache is not None:
            self.factor_cache[key] = system.factor
        self.built[g] = (mesh, system)
        return self.built[g]

    # ------------------------------------------------------------ rails
    def _setup_loads_in(self, g: Group) -> List[CurrentLoad]:
        loads = []
        for l in self.setup.loads:
            if l.supply.net not in g:
                continue
            if not l.groups:
                loads.append(CurrentLoad(l.name, self.pads(l.supply), l.current))
                continue
            for i, grp in enumerate(l.groups):
                ref = PinRef(l.supply.ref, l.supply.net, list(grp.pins))
                loads.append(CurrentLoad(_group_load_name(l, i), self.pads(ref), grp.current))
        return loads

    def _child_currents(self, rail: _Rail, vin_est: Dict[str, List[float]]) -> Dict[str, float]:
        """Input current of every phase of every VRM the rail feeds."""
        currents = {}
        for c in rail.children:
            share = sum(self.i_out[c.name]) / (len(c.phases) + 1)
            for k, name in enumerate(rail.child_load[c.name]):
                currents[name] = _input_current(c, self.v_out[c.name][k], share,
                                                vin_est[c.name][k])
        return currents

    def _iterate_rail(self, rail: _Rail, system, demand: float, label: str,
                      warnings: List[str]):
        """Fixed point of the rail: VRM input currents downstream depend on the voltage at
        their input pins, and remote sense (or a multi-phase VRM's mean output) moves the
        output until the regulated pins sit at the set point. Returns (solution, applied
        voltage, input voltages, currents, iterations)."""
        vrm = rail.vrm
        target = vrm.voltage + self.v_gsense.get(vrm.name, 0.0)
        v_src = target
        vin_est = {c.name: list(self.v_in.get(c.name, [vrm.voltage] * (len(c.phases) + 1)))
                   for c in rail.children}
        change = 0.0
        for it in range(1, MAX_TREE_ITERATIONS + 1):  # noqa: B007 - reported after
            currents = self._child_currents(rail, vin_est)
            total = demand + sum(currents.values())
            for name in rail.phase_load:
                currents[name] = -total / rail.n_phases
            sol = system.solve({vrm.name: v_src}, currents, progress=NULL)
            change = 0.0
            for c in rail.children:
                for k, pp in enumerate(rail.child_pads[c.name]):
                    new = _mean_v(sol, pp)
                    change = max(change, abs(new - vin_est[c.name][k]))
                    vin_est[c.name][k] = new
            vr = rail.regulated(sol)
            if vr is not None:
                step = target - vr
                v_src += step
                change = max(change, abs(step))
            if change < V_TOL:
                break
        else:
            warnings.append(f"{label}: power-tree iteration did not converge "
                            f"(last change {change:.2e} V)")
            self.not_converged.add(self.groups[vrm.output.net])
        if rail.regulated(sol) is not None:
            sol = system.solve({vrm.name: v_src}, currents, progress=NULL)
        return sol, v_src, vin_est, currents, it

    def _solve_rail(self, index: int, g: Group, suffix: str, warnings: List[str]):
        self.progress.check_cancel()
        vrm = self.driver[g]
        label = self.label_of(g)
        self.progress.stage(f"Rail {index + 1}/{len(self.order)}: {label} "
                            f"(from {vrm.name}){suffix}")
        rail = _Rail(vrm, self.fed_by.get(g, []), self.pads)
        loads = self._setup_loads_in(g)
        demand = sum(ld.current for ld in loads)     # fixed loads; the rest vary
        loads += rail.terminal_loads()
        mesh, system = self._system_for(
            g, [VoltageSource(vrm.name, rail.phase_pads[0], vrm.voltage)], loads, label)

        sol, v_src, vin, currents, it = self._iterate_rail(rail, system, demand, label,
                                                           warnings)
        if rail.sense_pads:
            self.v_sense[vrm.name] = _mean_v(sol, rail.sense_pads)
        self.v_out[vrm.name] = [_mean_v(sol, pp) for pp in rail.phase_pads]
        self.i_out[vrm.name] = [sol.source_current[vrm.name],
                                *(-currents[name] for name in rail.phase_load)]
        self.progress.info(f"{label}: {it} iteration(s), {vrm.name} supplies "
                           f"{sum(self.i_out[vrm.name]) * 1e3:.2f} mA at {v_src:.5f} V"
                           + (f" ({rail.n_phases} phases)" if rail.n_phases > 1 else ""))
        for c in rail.children:
            self.v_in[c.name] = vin[c.name]
            self.i_in[c.name] = [currents[name] for name in rail.child_load[c.name]]
        warnings.extend(f"{label}: {w}" for w in sol.warnings)
        self.rail_solutions[g] = sol
        return (label, sorted(g), "rail", vrm.name, mesh, sol, it)

    # ------------------------------------------------------------ grounds
    def _vrm_ground_returns(self, g: Group, loads: List[CurrentLoad]) -> Dict[str, float]:
        """Every non-reference VRM phase returns its output current and sends its input
        current back to its own source; ground sense pins are probed, drawing nothing."""
        currents = {}
        for v in self.setup.vrms:
            for k, ph in enumerate(v.all_phases() if v.ground and v.ground.net in g else []):
                if v.kind == "source" and k == 0:
                    continue                  # the 0 V reference
                name = _term(v, "gnd", k + 1)
                i_back = self.i_in[v.name][k] if v.name in self.i_in else 0.0
                currents[name] = self.i_out[v.name][k] - i_back
                loads.append(CurrentLoad(name, self.pads(ph.ground), 0.0))
            if v.ground_sense and v.ground_sense.net in g:
                loads.append(CurrentLoad(f"{v.name} (gnd sense)", self.pads(v.ground_sense),
                                         0.0))
        return currents

    def _solve_ground(self, g: Group, suffix: str, warnings: List[str]):
        self.progress.check_cancel()
        label = self.label_of(g)
        self.progress.stage(f"Ground: {label}{suffix}")
        refs = [v for v in self.setup.vrms
                if v.kind == "source" and v.ground and v.ground.net in g]
        sources = [VoltageSource(f"{v.name} (gnd)", self.pads(v.ground), 0.0) for v in refs]
        loads = [CurrentLoad(f"{l.name} (return)", self.pads(l.ground), -l.total_current())
                 for l in self.setup.loads if l.ground and l.ground.net in g]
        currents = self._vrm_ground_returns(g, loads)
        mesh, system = self._system_for(g, sources, loads, label)
        sol = system.solve(currents=currents, progress=NULL)
        warnings.extend(f"{label}: {w}" for w in sol.warnings)
        self.ground_solutions[g] = sol
        return (label, sorted(g), "ground", ", ".join(v.name for v in refs), mesh, sol, 1)

    def _ground_sense_change(self) -> float:
        """Move each ground-sensing VRM's reference to its ground sense pins' new voltage."""
        change = 0.0
        for v in self.setup.vrms:
            if v.ground_sense:
                gsol = self.ground_solutions[self.groups[v.ground.net]]
                new = _mean_v(gsol, self.pads(v.ground_sense))
                change = max(change, abs(new - self.v_gsense[v.name]))
                self.v_gsense[v.name] = new
        return change

    # ------------------------------------------------------------ run
    def run(self) -> TreeResult:
        t0 = time.perf_counter()
        out = TreeResult()
        for tree_pass in range(1, MAX_TREE_ITERATIONS + 1):
            warnings: List[str] = []
            self.not_converged = set()
            suffix = f" (pass {tree_pass})" if tree_pass > 1 else ""
            solved = [self._solve_rail(i, g, suffix, warnings) for i, g in enumerate(self.order)]
            solved += [self._solve_ground(g, suffix, warnings) for g in self.ground_groups]
            change = self._ground_sense_change()
            if change < V_TOL:
                break
        else:
            self.tree_converged = False
            warnings.append(f"Ground-sense iteration did not converge "
                            f"(last change {change:.2e} V)")
        if tree_pass > 1 and self.tree_converged:
            self.progress.info(f"Ground sense: converged in {tree_pass} power-tree passes")

        not_converged_labels = {self.label_of(g) for g in self.not_converged}
        set_point = {v.name: v.voltage for v in self.setup.vrms}
        for label, nets, kind, drv, mesh, sol, it in solved:
            # Drop and boost on a rail are measured from its VRM's set point, not from the
            # output pins (which remote sense moves); grounds from their 0 V reference.
            ref = set_point[drv] if kind == "rail" else 0.0
            out.groups[label] = GroupResult(label, nets, kind, drv,
                                            NetResult(mesh, sol, compute_fields(sol, ref)), it,
                                            label not in not_converged_labels)
        out.warnings.extend(warnings)
        regulation = {v.name: self._regulation(v) for v in self.setup.vrms}
        out_of_reg = self._groups_out_of_regulation(regulation, out.warnings)
        out.loads = [self._load_report(l, out_of_reg) for l in self.setup.loads]
        out.vrms = [self._vrm_report(v, regulation[v.name]) for v in self.setup.vrms]
        out.solve_s = time.perf_counter() - t0
        self.progress.info(f"Power tree solved: {len(self.order)} rail(s), "
                           f"{len(self.ground_groups)} ground(s) in {out.solve_s:.2f} s")
        return out

    # ------------------------------------------------------------ checks and reports
    def _regulation(self, v: Vrm) -> str:
        """Why the VRM cannot hold its set point at the solved input voltage ("" if it can).
        A multi-phase VRM is judged on its worst phase."""
        if not v.input:
            return ""
        vis = self.v_in[v.name]
        lo, hi = min(vis), max(vis)
        if v.vin_min is not None and lo < v.vin_min:
            return f"input {lo:.4f} V is below its {v.vin_min:g} V minimum"
        if v.vin_max is not None and hi > v.vin_max:
            return f"input {hi:.4f} V is above its {v.vin_max:g} V maximum"
        if v.kind == "ldo":
            sign = 1.0 if v.voltage >= 0 else -1.0
            headroom = min(sign * (vi - vo) for vi, vo in zip(vis, self.v_out[v.name]))
            if headroom < v.dropout_v:
                return (f"input leaves {headroom:.4f} V across it, less than its "
                        f"{v.dropout_v:g} V dropout")
        return ""

    def _groups_out_of_regulation(self, regulation: Dict[str, str],
                                  warnings: List[str]) -> set:
        out = set()
        for v in self.setup.vrms:
            if regulation[v.name]:
                warnings.append(f"VRM {v.name}: {regulation[v.name]}; the loads it feeds are "
                                f"marked {OUT_OF_REGULATION}")
                out.update(self._fed_from(self.groups[v.output.net]))
        return out

    def _supply_pads(self, l: Load) -> List[str]:
        if not l.groups:
            return self.pads(l.supply)
        return [pid for grp in l.groups
                for pid in self.pads(PinRef(l.supply.ref, l.supply.net, list(grp.pins)))]

    def _load_report(self, l: Load, out_of_reg: set) -> LoadReport:
        g = self.groups[l.supply.net]
        sol = self.rail_solutions[g]
        sv = _pad_voltages(sol, self._supply_pads(l))
        gv = None
        if l.ground:
            gv = _pad_voltages(self.ground_solutions[self.groups[l.ground.net]],
                               self.pads(l.ground))
        g_mean = 0.0 if gv is None else float(gv.mean())
        g_lo = 0.0 if gv is None else float(gv.min())
        g_hi = 0.0 if gv is None else float(gv.max())
        nominal = self.driver[g].voltage
        low, high = float(sv.min()) - g_hi, float(sv.max()) - g_lo
        # The worst pin is the one furthest from nominal, below (drop) or above (boost).
        worst = low if nominal - low >= high - nominal else high
        eff = float(sv.mean()) - g_mean
        rep = LoadReport(l.name, self.label_of(g), l.total_current(), nominal,
                         float(sv.mean()), float(sv.min()),
                         None if gv is None else g_mean, eff, nominal - eff,
                         l.tolerance_pct, None, worst, self._pin_group_rows(l, sol), low, high)
        if not self.tree_converged or g in self.not_converged:
            rep.status = NOT_CONVERGED
        elif g in out_of_reg:
            rep.status, rep.passed = OUT_OF_REGULATION, False
        elif l.tolerance_pct is not None and nominal:
            rep.passed = abs(rep.drop_worst_pct) <= l.tolerance_pct
        return rep

    def _pin_group_rows(self, l: Load, sol) -> List[tuple]:
        rows = []
        for grp in l.groups:
            v = _pad_voltages(sol, self.pads(PinRef(l.supply.ref, l.supply.net, list(grp.pins))))
            rows.append((grp.name or ",".join(grp.pins), grp.current, float(v.mean()),
                         float(v.min()), float(v.max())))
        return rows

    def _vrm_report(self, v: Vrm, regulation: str) -> VrmReport:
        n = len(v.phases) + 1
        has_in = v.name in self.v_in
        vis, iis = ((self.v_in[v.name], self.i_in[v.name]) if has_in
                    else ([None] * n, [None] * n))
        phases = [PhaseReport(k + 1, self.v_out[v.name][k], self.i_out[v.name][k], vis[k],
                              iis[k]) for k in range(n)]
        i_total = sum(self.i_out[v.name])
        limit = v.current_limit_a
        return VrmReport(
            v.name, v.kind, self.label_of(self.groups[v.output.net]), v.voltage,
            float(np.mean(self.v_out[v.name])), self.v_sense.get(v.name), i_total,
            float(np.mean(vis)) if has_in else None, sum(iis) if has_in else None,
            sum(ph.p_out for ph in phases),
            sum(ph.p_in for ph in phases) if has_in else None,
            self.v_gsense.get(v.name), phases if n > 1 else [], limit,
            None if limit is None else abs(i_total) <= limit, regulation)


def solve_tree(snap: BoardSnapshot, setup: PISetup,
               progress: ProgressReporter = NULL, temperature=None,
               mesh_cache: Optional[dict] = None,
               factor_cache: Optional[dict] = None) -> TreeResult:
    """Solve every rail and ground group. ``temperature`` (optional) maps KiCad xy (mm) to
    copper temperature in degC for temperature-dependent resistivity. ``mesh_cache`` (a
    dict) lets repeated solves of the same setup reuse their meshes; ``factor_cache`` (a
    dict) keeps each group's solver so the next solve can reuse its AMG hierarchy as the
    preconditioner, which pays off when only the temperature has changed."""
    return _TreeSolver(snap, setup, progress, temperature, mesh_cache, factor_cache).run()
