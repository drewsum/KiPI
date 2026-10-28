"""AC impedance sweep of one rail: field solves, then the parts, at every frequency.

1. At the field-solve frequencies (``field_points_per_decade``), the copper model is
   factorized once and solved for every port: the board's port impedance matrix Z_b.
2. jω·Z_b, smooth across frequency (the plane capacitance's 1/jω is taken out), is
   interpolated onto the full sweep (PCHIP on log f, real and imaginary parts).
3. At each sweep frequency the parts terminate their ports, and the impedance at each
   probe is Z_pp − Z_pE (Z_EE + diag Z_e)⁻¹ Z_Ep (the other probes stay open). A
   multi-pin part (#193) is a port from its side A to each other side, the ports sharing
   its N-port admittance.
4. Package models (#53) join at their balls' ports, and their die probes are solved
   through them (:mod:`kipi.ac.package`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from kipi.ac import package as pkgs
from kipi.ac.bias import (
    apply_derating, dc_link_currents, dc_volts, part_temperatures, temperature_source,
)
from kipi.ac.board_map import ACMap, MapMesh, build_map
from kipi.ac.checks import ground_nets, rail_caps, rail_nets, validate_ac
from kipi.ac.solver.field import field_solves
from kipi.ac.solver.fem import ACError, ACStats, ACSystem, PortDef
from kipi.ac.target import judged_from, target_at
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.pi.model import PinRef, PISetup, biased_link, warmed_link
from kipi.dc.solver import pads_for


@dataclass
class ProbeCurve:
    name: str
    z: np.ndarray                         # complex ohms at ACResult.freqs
    target_ohm: Optional[float] = None
    target_hz: Optional[float] = None
    target_points: Optional[List[Tuple[float, float]]] = None   # custom mask, in place of
                                                                 # the flat target
    # The VRM's control loop bandwidth when the target applies only above it (None: the
    # whole target applies; see kipi.ac.target.judged_from).
    above_hz: Optional[float] = None
    # The probe's load step and its slew rate, for the estimated transient (#166).
    step_a: Optional[float] = None
    slew_a_per_us: Optional[float] = None

    @property
    def mag(self) -> np.ndarray:
        return np.abs(self.z)

    @property
    def has_target(self) -> bool:
        return bool(self.target_points or self.target_ohm)

    def target_at(self, freqs) -> Optional[np.ndarray]:
        """The target at each of ``freqs`` (NaN where none applies); None without one."""
        return target_at(freqs, self.target_ohm, self.target_hz, self.target_points,
                         self.above_hz)

    def worst(self, freqs: np.ndarray):
        """(|Z|, f) where the impedance comes closest to (or furthest over) the target,
        where the target applies; without one, the highest impedance in the sweep (above
        the VRM's bandwidth, where it has one)."""
        t = self.target_at(freqs)
        if t is None:
            sel = np.ones(len(freqs), dtype=bool)
            if self.above_hz and (np.asarray(freqs) >= self.above_hz).any():
                sel = np.asarray(freqs) >= self.above_hz
            i = np.flatnonzero(sel)[int(np.argmax(self.mag[sel]))]
        else:
            sel = np.isfinite(t)
            if not sel.any():
                i = int(np.argmax(self.mag))
            else:
                i = np.flatnonzero(sel)[int(np.argmax(self.mag[sel] / t[sel]))]
        return float(self.mag[i]), float(freqs[i])

    def transient(self, freqs: np.ndarray, bandwidth_hz: Optional[float]):
        """The estimated response to the probe's load step
        (:func:`kipi.ac.transient.transient`); None without a step, slew rate or VRM
        bandwidth."""
        from kipi.ac.transient import transient
        slew = self.slew_a_per_us * 1e6 if self.slew_a_per_us else None
        return transient(freqs, self.z, bandwidth_hz, self.step_a, slew)

    def allowed_v(self) -> Optional[float]:
        """The AC tolerance the flat target implies, target |Z| times the load step
        (Z_target = V_AC / I_step); None without both."""
        if self.target_points or not (self.target_ohm and self.step_a):
            return None
        return self.target_ohm * self.step_a

    def passed(self, freqs: np.ndarray) -> Optional[bool]:
        t = self.target_at(freqs)
        if t is None:
            return None
        sel = np.isfinite(t)
        return bool(np.all(self.mag[sel] <= t[sel])) if sel.any() else None


@dataclass
class ACResult:
    rail: str                             # VRM name
    rail_nets: List[str]
    ground: str                           # its nets, " + " between them (#171)
    freqs: np.ndarray
    probes: List[ProbeCurve]
    field_freqs: np.ndarray
    caps: List[str] = field(default_factory=list)          # references in the model
    missing_caps: List[str] = field(default_factory=list)  # on the rail, no model
    links: List[str] = field(default_factory=list)
    plane_capacitance_f: float = 0.0
    stats: ACStats = field(default_factory=ACStats)
    warnings: List[str] = field(default_factory=list)
    solve_s: float = 0.0
    bandwidth_hz: Optional[float] = None  # the VRM's control loop bandwidth, if given
    report_path: Optional[Path] = None    # the HTML report, when one was written
    board_map: Optional[ACMap] = None     # what the Board map tab shows (#67)
    # DC bias (#74) and aging (#109): {reference: (DC volts or None, derated farads)} for
    # the parts derated, and what was done.
    cap_bias: Dict[str, Tuple[Optional[float], float]] = field(default_factory=dict)
    bias_note: str = ""
    # Each capacitor derating (#180): (what, whether any part got it, how or why not).
    deratings: List[Tuple[str, bool, str]] = field(default_factory=list)


class _Pads:
    def __init__(self, snap):
        self.snap = snap

    def __call__(self, ref) -> List[str]:
        return [p.id for p in pads_for(self.snap, ref.net, ref.ref, ref.pins or None)]


def fmt_hz(f: float) -> str:
    """A frequency for the log: 4 significant digits in Hz, kHz, MHz or GHz."""
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


OPEN_KINDS = ("probe", "pkg")             # ports left open by the parts: probes, package balls


def element_impedance(port: PortDef, omega: np.ndarray) -> np.ndarray:
    if port.kind == "cap":
        return port.element.impedance(omega)
    return port.element.impedance(omega)   # a series link


@dataclass
class LinkPin:
    """The element of one port of a multi-pin series link (#193), from its side A to side
    ``index`` + 1 (B, then the others). Its ports share the part's admittance."""
    link: object
    index: int


def link_ports(k, pads) -> List[PortDef]:
    """A series link's ports (``pads`` maps a PinRef to pad ids): one from side A to side
    B, or a multi-pin part's one from side A to each of its other sides."""
    if not k.is_multi:
        return [PortDef(k.name, "link", pads(k.side_a), pads(k.side_b), k)]
    return [PortDef(f"{k.name} {s.label()}", "link", pads(s), pads(k.side_a), LinkPin(k, i))
            for i, s in enumerate(k.sides[1:])]


def warm_parts(snap: BoardSnapshot, ports: List[PortDef], temperature,
               temps: Dict[str, float]) -> int:
    """Put the SPICE models of the parts on ``ports`` at the copper's temperature under
    them (#196), in place: series links at the mean over their pads of ``temperature``,
    capacitors at ``temps`` (°C by reference, :func:`kipi.ac.bias.part_temperatures`).
    Returns how many parts were warmed."""
    if temperature is None:
        return 0
    pads = _Pads(snap)
    done: Dict[int, object] = {}
    for p in ports:
        if p.kind == "cap" and p.element.uses_spice and p.name in temps:
            p.element = replace(p.element, temp_c=temps[p.name])
            done[id(p)] = p.element
        elif p.kind == "link":
            k = p.element.link if isinstance(p.element, LinkPin) else p.element
            if id(k) not in done:
                done[id(k)] = warmed_link(snap, k, pads, temperature)
            w = done[id(k)]
            p.element = LinkPin(w, p.element.index) if isinstance(p.element, LinkPin) else w
    return sum(getattr(e, "temp_c", None) is not None for e in done.values())


def build_ports(snap: BoardSnapshot, setup: PISetup, vrm_name: str, gnd: str,
                link_currents: Optional[Dict[str, float]] = None):
    """Ports, merged short links, notes and the package models joining the solve
    (:class:`kipi.ac.package.PackageUse`) for the rail driven by ``vrm_name``. Die probes
    have no port: they are solved through their packages' balls, ports of kind "pkg".
    ``link_currents``, the last DC solve's current through each series link by name,
    biases the diodes among them; without it, an estimate from the loads beyond each."""
    vrm = next(v for v in setup.vrms if v.name == vrm_name)
    rail = rail_nets(setup, vrm, gnd)
    grounds = ground_nets(setup, vrm, gnd)
    pads = _Pads(snap)
    ports: List[PortDef] = []
    probes = [p for p in setup.probes if p.rail.net in rail]
    for p in probes:
        if not p.on_die:
            ports.append(PortDef(p.name, "probe", pads(p.rail), pads(p.ground), p))
    uses, notes = pkgs.uses(setup, snap, set(rail), grounds, probes)
    for use in uses:
        k = use.model
        for b in use.balls:
            plus = pads(PinRef(k.ref, b.net, b.pins))
            minus = pads(PinRef(k.ref, b.ground_net, b.ground_pins))
            if not (plus and minus):
                raise ACError(f"Package {k.name}: {b.label} has no pads of {k.ref} on "
                              f"{b.net if not plus else b.ground_net}")
            ports.append(PortDef(f"{k.name} {b.label}", "pkg", plus, minus, (k, b)))
    caps, missing = rail_caps(snap, setup, rail, grounds)
    for ref, model, _net, a, b in caps:
        ports.append(PortDef(ref, "cap", a, b, model))
    shorts, links = [], []
    inside = set(rail) | grounds
    for k in setup.links:
        on = [p.net in inside for p in k.sides]
        if not any(on):
            continue
        if not all(on):
            off = ", ".join(dict.fromkeys(p.net for p, x in zip(k.sides, on) if not x))
            notes.append(f"Series link {k.name} leads off the rail to {off}; left out")
            continue
        if k.is_short():
            shorts.append((pads(k.side_a), pads(k.side_b)))
        else:
            solved = bool(link_currents) and k.name in link_currents
            k = biased_link(setup, k, link_currents)
            if k.is_diode:
                src = "from the DC solve" if solved else "the DC loads beyond it, estimated"
                notes.append(f"Series link {k.name}: diode small-signal model at "
                             f"{k.bias_a:.3g} A, {src}")
            ports += link_ports(k, pads)
            links.append(k.name)
    return rail, ports, shorts, [c[0] for c in caps], missing, links, notes, uses


def _interpolate(f_field: np.ndarray, zb: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """Z_b at ``freqs`` from samples at ``f_field``: PCHIP of jω·Z_b on log f."""
    if len(f_field) == len(freqs) and np.allclose(f_field, freqs):
        return zb
    from scipy.interpolate import PchipInterpolator
    w_field = 2 * np.pi * f_field
    y = zb * (1j * w_field)[:, None, None]
    x = np.log10(f_field)
    xi = np.log10(freqs)
    re = PchipInterpolator(x, y.real, axis=0)(xi)
    im = PchipInterpolator(x, y.imag, axis=0)(xi)
    return (re + 1j * im) / (1j * 2 * np.pi * freqs)[:, None, None]


def terminate(zb: np.ndarray, freqs: np.ndarray, ports: List[PortDef]) -> np.ndarray:
    """Impedance at each probe port (columns), with every part on its port."""
    probe_idx = [i for i, p in enumerate(ports) if p.kind == "probe"]
    out = np.zeros((len(freqs), len(probe_idx)), complex)
    E, ze, ye = _elements(freqs, ports)
    P = np.array(probe_idx)
    for k in range(len(freqs)):
        z = zb[k]
        zpp = z[P, P]
        if len(E):
            zep = z[np.ix_(E, P)]
            out[k] = zpp - np.einsum("ij,ij->j", zep, _parts_solve(z, E, ze, ye, k, zep))
        else:
            out[k] = zpp
    return out


def _elements(freqs: np.ndarray, ports: List[PortDef]):
    """The indices of the ports that hold parts, their impedances (F, E), and, with a
    multi-pin part among them (#193), their admittances (F, E, E), else None: a
    multi-pin part's ports share a block of it, which may be singular (a common-mode
    choke's windings float apart at DC), so it has no impedance matrix."""
    idx = [i for i, p in enumerate(ports) if p.kind not in OPEN_KINDS]
    omega = 2 * np.pi * freqs
    multi = [j for j, i in enumerate(idx) if isinstance(ports[i].element, LinkPin)]
    ze = np.ones((len(freqs), len(idx)), complex)
    for j, i in enumerate(idx):
        if j not in multi:
            ze[:, j] = element_impedance(ports[i], omega)
    if not multi:
        return np.array(idx, dtype=int), ze, None
    ye = np.zeros((len(freqs), len(idx), len(idx)), complex)
    plain = [j for j in range(len(idx)) if j not in multi]
    ye[:, plain, plain] = 1.0 / ze[:, plain]
    parts: Dict[int, List[Tuple[int, int]]] = {}
    for j in multi:
        el = ports[idx[j]].element
        parts.setdefault(id(el.link), []).append((j, el.index))
    for group in parts.values():
        link = ports[idx[group[0][0]]].element.link
        y = link.admittance(omega)[:, 1:, 1:]           # side A is the ports' minus side
        at = np.array([j for j, _i in group])
        sel = np.array([i for _j, i in group])
        ye[:, at[:, None], at[None, :]] = y[:, sel[:, None], sel[None, :]]
    return np.array(idx, dtype=int), ze, ye


def _parts_solve(z: np.ndarray, E: np.ndarray, ze: np.ndarray, ye: Optional[np.ndarray],
                 k: int, rhs: np.ndarray) -> np.ndarray:
    """(Z_EE + Z_e)⁻¹ rhs at sweep frequency ``k``; with admittances,
    (1 + Y_e Z_EE)⁻¹ Y_e rhs, the same where Z_e = Y_e⁻¹ exists."""
    zee = z[np.ix_(E, E)]
    if ye is None:
        return np.linalg.solve(zee + np.diag(ze[k]), rhs)
    y = ye[k]
    return np.linalg.solve(np.eye(len(E)) + y @ zee, y @ rhs)


def terminate_transfer(zb: np.ndarray, freqs: np.ndarray, ports: List[PortDef],
                       src: List[int], obs: List[int]) -> np.ndarray:
    """Transfer impedances (F, len(obs), len(src)) between probe ports with every part on
    its port: the voltage at each ``obs`` port per ampere into each ``src`` port, the other
    probes open. Z_os − Z_oE (Z_EE + Z_e)⁻¹ Z_Es; its diagonal for ``obs = src`` is
    :func:`terminate`."""
    E, ze, ye = _elements(freqs, ports)
    S, O = np.array(src, dtype=int), np.array(obs, dtype=int)
    out = np.zeros((len(freqs), len(O), len(S)), complex)
    for k in range(len(freqs)):
        z = zb[k]
        out[k] = z[np.ix_(O, S)]
        if len(E):
            out[k] -= z[np.ix_(O, E)] @ _parts_solve(z, E, ze, ye, k, z[np.ix_(E, S)])
    return out


def element_currents(zb: np.ndarray, freqs: np.ndarray, ports: List[PortDef],
                     src: List[int]) -> Tuple[np.ndarray, np.ndarray]:
    """How currents into the ``src`` probe ports divide among the parts: (part port
    indices, (F, parts, len(src))), each part's port current per ampere into each source,
    entering the board at its plus side: −(Z_EE + Z_e)⁻¹ Z_Es."""
    E, ze, ye = _elements(freqs, ports)
    S = np.array(src, dtype=int)
    out = np.zeros((len(freqs), len(E), len(S)), complex)
    for k in range(len(freqs)):
        if len(E):
            z = zb[k]
            out[k] = -_parts_solve(z, E, ze, ye, k, z[np.ix_(E, S)])
    return E, out


def _with_packages(zi, freqs, ports, uses, setup, notes, progress):
    """The impedance at every board probe, then every die probe, with the package models
    joined at their balls (:func:`kipi.ac.package.connect`); and the die probes, as
    :class:`PortDef` with no pads."""
    probe_idx = [i for i, p in enumerate(ports) if p.kind == "probe"]
    ball_idx = [i for i, p in enumerate(ports) if p.kind == "pkg"]
    idx = probe_idx + ball_idx
    zr = terminate_transfer(zi, freqs, ports, idx, idx)
    by_port = {(p.package, p.die_port): p for p in setup.probes if p.on_die}
    ys, n_balls, dies = [], [], []
    for use in uses:
        why = pkgs.check_coverage(use, float(freqs[0]), float(freqs[-1]))
        if why:
            notes.append(why)
            progress.warn(why)
        try:
            ys.append(use.y_at(freqs))
        except ValueError as e:
            raise ACError(f"Package {use.model.name}: {e}") from None
        n_balls.append(len(use.balls))
        dies += [PortDef(by_port[(use.model.name, d.number)].name, "probe", [], [],
                         by_port[(use.model.name, d.number)]) for d in use.dies]
    progress.info(f"Package models: {', '.join(u.model.name for u in uses)}; "
                  f"{len(ball_idx)} PCB port(s), {len(dies)} die probe(s)")
    try:
        z = pkgs.connect(zr, len(probe_idx), pkgs.block_diag(ys, n_balls), len(ball_idx))
    except np.linalg.LinAlgError:
        raise ACError("The package models cannot be joined to the board (a singular "
                      "network): check their port connections") from None
    return z, dies


def solve_ac(snap: BoardSnapshot, setup: PISetup, vrm_name: str, gnd: str,
             progress: ProgressReporter = NULL, dc_result=None) -> ACResult:
    """Sweep the rail driven by ``vrm_name`` against ``gnd``. ``dc_result``, the last DC
    power-tree solve if any, gives the DC bias across the capacitors (#74) and, with a
    thermal co-simulation, the board's temperatures (#77)."""
    t0 = time.perf_counter()
    errors = validate_ac(setup, vrm_name, gnd)
    if errors:
        raise ACError("Setup problems:\n- " + "\n- ".join(errors))
    s = setup.ac
    rail, ports, shorts, caps, missing, links, notes, uses = build_ports(
        snap, setup, vrm_name, gnd, dc_link_currents(dc_result))
    for n in notes:
        progress.warn(n)
    vrm = next(v for v in setup.vrms if v.name == vrm_name)
    # The ground picked first, then the ground nets series links tie to it (#171).
    grounds = [gnd, *sorted(ground_nets(setup, vrm, gnd) - {gnd})]
    ground = " + ".join(grounds)
    volts = {}
    if s.dc_bias and dc_result is not None:
        volts = dc_volts(snap, dc_result, [(p.name, p.element, None, p.plus_pads, p.minus_pads)
                                           for p in ports if p.kind == "cap"
                                           and p.element.dc_bias])
    temperature, temp_text, temp_warning = temperature_source(setup, dc_result)
    if temp_warning:
        notes.append(temp_warning)
        progress.warn(temp_warning)
    temps = part_temperatures(snap, ports, temperature)
    cap_bias, bias_notes, deratings = apply_derating(ports, setup, vrm, volts, temps,
                                                     temp_text)
    for n in bias_notes:
        progress.info(n)
    warmed = warm_parts(snap, ports, temperature, temps)
    if warmed:
        progress.info(f"SPICE models at the copper's temperature under them ({temp_text}): "
                      f"{warmed} part(s)")
    # Die probes (#53) are marked on their package's part.
    die_names = [p.name for p in setup.probes if p.on_die and any(
        u.model.name == p.package and any(d.number == p.die_port for d in u.dies)
        for u in uses)]
    n_die = len(die_names)
    progress.info(f"Rail {vrm_name}: {', '.join(sorted(rail))} against {ground}; "
                  f"{len(caps)} capacitor(s) with models, {len(missing)} without, "
                  f"{len(links)} series link(s), "
                  f"{sum(p.kind == 'probe' for p in ports) + n_die} probe(s)")
    if missing:
        progress.warn(f"{len(missing)} capacitor(s) between the rail and {ground} have no model "
                      f"and are left out: {', '.join(missing[:20])}"
                      f"{'…' if len(missing) > 20 else ''}")
    # The board map, handed to the GUI as it fills: the parts now, the mesh once meshed.
    board_map = build_map(snap, setup, vrm_name, rail, ground, caps, missing,
                          [p.name for p in ports if p.kind == "probe"] + die_names)
    progress.partial(board_map)
    system = ACSystem(snap, sorted(rail), grounds, ports, shorts, setup.settings, s, progress,
                      temperature=temperature)
    if system.stats.t_copper is not None:
        lo, hi = system.stats.t_copper
        progress.info(f"Copper at {temp_text}: {lo:.3g} °C" if hi - lo < 0.05 else
                      f"Copper at {temp_text}: {lo:.3g} to {hi:.3g} °C")
    board_map.mesh = MapMesh.from_mesh(system.mesh, system.node_ground)
    progress.partial(board_map)

    freqs = s.frequencies()
    f_field = s.field_frequencies()
    progress.stage(f"Field solves: {len(f_field)} frequencies, {len(ports)} ports, "
                   f"{system.n} unknowns")
    t1 = time.perf_counter()
    zb = field_solves(system, f_field, progress)
    system.stats.times["field"] = time.perf_counter() - t1
    progress.info(f"Field solves done in {system.stats.times['field']:.2f} s")

    with progress.timed(f"Sweep: {len(freqs)} frequencies"):
        zi = _interpolate(f_field, zb, freqs)
        if uses:
            z, die_probes = _with_packages(zi, freqs, ports, uses, setup, notes, progress)
        else:
            z, die_probes = terminate(zi, freqs, ports), []
    probe_ports = [p for p in ports if p.kind == "probe"] + die_probes
    bandwidth = vrm.bandwidth_khz * 1e3 if vrm.bandwidth_khz else None
    curves = []
    for i, p in enumerate(probe_ports):
        pr = p.element
        above, why = judged_from(bandwidth, freqs, pr.target_ohm, pr.target_hz,
                                 pr.target_mask())
        if why:
            notes.append(f"Probe {p.name}: {why}")
            progress.warn(notes[-1])
        curves.append(ProbeCurve(p.name, z[:, i], pr.target_ohm, pr.target_hz,
                                 pr.target_mask(), above, pr.step_a, pr.slew_a_per_us))
    if bandwidth:
        progress.info(f"Targets apply above the {vrm_name} control loop bandwidth, "
                      f"{fmt_hz(bandwidth)} (open-loop PDN impedance)")
    for c in curves:
        zmax, fmax = c.worst(freqs)
        verdict = {None: "", True: " PASS", False: " FAIL"}[c.passed(freqs)]
        progress.info(f"  {c.name}: |Z| {np.min(c.mag) * 1e3:.3g} to {np.max(c.mag) * 1e3:.4g} mΩ; "
                      f"worst {zmax * 1e3:.4g} mΩ at {fmax / 1e6:.4g} MHz{verdict}")
        if not (c.step_a and c.slew_a_per_us):
            continue
        if not bandwidth:
            notes.append(f"Probe {c.name}: no transient estimate, since {vrm_name} has no "
                         "control loop bandwidth to close its loop with")
            progress.warn(notes[-1])
            continue
        tr = c.transient(freqs, bandwidth)
        progress.info(f"  {c.name}: a {c.step_a:g} A load step dips about "
                      f"{tr.undershoot_v * 1e3:.4g} mV; its release rises about "
                      f"{tr.overshoot_v * 1e3:.4g} mV (estimate)")
        for note in tr.notes:
            notes.append(f"Probe {c.name}: {note}")
            progress.warn(notes[-1])
    return ACResult(rail=vrm_name, rail_nets=sorted(rail), ground=ground, freqs=freqs,
                    probes=curves, field_freqs=f_field, caps=caps, missing_caps=missing,
                    links=links, plane_capacitance_f=system.total_plane_capacitance,
                    stats=system.stats, warnings=notes + system.warnings,
                    solve_s=time.perf_counter() - t0, bandwidth_hz=bandwidth,
                    board_map=board_map, cap_bias=cap_bias,
                    bias_note=bias_notes[0] if bias_notes else "", deratings=deratings)
