"""AC impedance sweep of one rail: field solves, then the parts, at every frequency.

1. At the field-solve frequencies (``field_points_per_decade``), the copper model is
   factorized once and solved for every port: the board's port impedance matrix Z_b.
2. jω·Z_b, smooth across frequency (the plane capacitance's 1/jω is taken out), is
   interpolated onto the full sweep (PCHIP on log f, real and imaginary parts).
3. At each sweep frequency the parts terminate their ports, and the impedance at each
   probe is Z_pp − Z_pE (Z_EE + diag Z_e)⁻¹ Z_Ep (the other probes stay open).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from kipi.ac.checks import rail_caps, rail_nets, validate_ac
from kipi.ac.solver.fem import ACError, ACStats, ACSystem, PortDef
from kipi.ac.target import target_at
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.pi.model import PISetup
from kipi.dc.solver import pads_for
from kipi.dc.solver.setup import SHORT_OHMS


@dataclass
class ProbeCurve:
    name: str
    z: np.ndarray                         # complex ohms at ACResult.freqs
    target_ohm: Optional[float] = None
    target_hz: Optional[float] = None
    target_points: Optional[List[Tuple[float, float]]] = None   # custom mask, in place of
                                                                 # the flat target

    @property
    def mag(self) -> np.ndarray:
        return np.abs(self.z)

    @property
    def has_target(self) -> bool:
        return bool(self.target_points or self.target_ohm)

    def target_at(self, freqs) -> Optional[np.ndarray]:
        """The target at each of ``freqs`` (NaN where none applies); None without one."""
        return target_at(freqs, self.target_ohm, self.target_hz, self.target_points)

    def worst(self, freqs: np.ndarray):
        """(|Z|, f) where the impedance comes closest to (or furthest over) the target, up
        to where the target applies; the highest impedance in the sweep without one."""
        t = self.target_at(freqs)
        if t is None:
            i = int(np.argmax(self.mag))
        else:
            sel = np.isfinite(t)
            i = np.flatnonzero(sel)[int(np.argmax(self.mag[sel] / t[sel]))]
        return float(self.mag[i]), float(freqs[i])

    def passed(self, freqs: np.ndarray) -> Optional[bool]:
        t = self.target_at(freqs)
        if t is None:
            return None
        sel = np.isfinite(t)
        return bool(np.all(self.mag[sel] <= t[sel]))


@dataclass
class ACResult:
    rail: str                             # VRM name
    rail_nets: List[str]
    ground: str
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
    report_path: Optional[Path] = None    # the HTML report, when one was written


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


def element_impedance(port: PortDef, omega: np.ndarray) -> np.ndarray:
    if port.kind == "cap":
        return port.element.impedance(omega)
    k = port.element                       # a series link
    return k.resistance + 1j * omega * (k.esl_h or 0.0) + 0 * omega


def build_ports(snap: BoardSnapshot, setup: PISetup, vrm_name: str, gnd: str):
    """Ports, merged short links, and notes for the rail driven by ``vrm_name``."""
    vrm = next(v for v in setup.vrms if v.name == vrm_name)
    rail = rail_nets(setup, vrm, gnd)
    pads = _Pads(snap)
    ports: List[PortDef] = []
    probes = [p for p in setup.probes if p.rail.net in rail]
    for p in probes:
        ports.append(PortDef(p.name, "probe", pads(p.rail), pads(p.ground), p))
    caps, missing = rail_caps(snap, setup, rail, gnd)
    for ref, model, _net, a, b in caps:
        ports.append(PortDef(ref, "cap", a, b, model))
    shorts, links, notes = [], [], []
    inside = set(rail) | {gnd}
    for k in setup.links:
        on = (k.side_a.net in inside, k.side_b.net in inside)
        if not any(on):
            continue
        if not all(on):
            notes.append(f"Series link {k.name} leads off the rail to "
                         f"{k.side_b.net if on[0] else k.side_a.net}; left out")
            continue
        if k.resistance <= SHORT_OHMS and not k.esl_h:
            shorts.append((pads(k.side_a), pads(k.side_b)))
        else:
            ports.append(PortDef(k.name, "link", pads(k.side_a), pads(k.side_b), k))
            links.append(k.name)
    return rail, ports, shorts, [c[0] for c in caps], missing, links, notes


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
    elem_idx = [i for i, p in enumerate(ports) if p.kind != "probe"]
    out = np.zeros((len(freqs), len(probe_idx)), complex)
    omega = 2 * np.pi * freqs
    ze = (np.stack([element_impedance(ports[i], omega) for i in elem_idx], axis=1)
          if elem_idx else np.zeros((len(freqs), 0)))
    P, E = np.array(probe_idx), np.array(elem_idx, dtype=int)
    for k in range(len(freqs)):
        z = zb[k]
        zpp = z[P, P]
        if len(E):
            m = z[np.ix_(E, E)] + np.diag(ze[k])
            zep = z[np.ix_(E, P)]
            out[k] = zpp - np.einsum("ij,ij->j", z[np.ix_(E, P)], np.linalg.solve(m, zep))
        else:
            out[k] = zpp
    return out


def solve_ac(snap: BoardSnapshot, setup: PISetup, vrm_name: str, gnd: str,
             progress: ProgressReporter = NULL) -> ACResult:
    t0 = time.perf_counter()
    errors = validate_ac(setup, vrm_name, gnd)
    if errors:
        raise ACError("Setup problems:\n- " + "\n- ".join(errors))
    s = setup.ac
    rail, ports, shorts, caps, missing, links, notes = build_ports(snap, setup, vrm_name, gnd)
    for n in notes:
        progress.warn(n)
    progress.info(f"Rail {vrm_name}: {', '.join(sorted(rail))} against {gnd}; "
                  f"{len(caps)} capacitor(s) with models, {len(missing)} without, "
                  f"{len(links)} series link(s), "
                  f"{sum(p.kind == 'probe' for p in ports)} probe(s)")
    if missing:
        progress.warn(f"{len(missing)} capacitor(s) between the rail and {gnd} have no model "
                      f"and are left out: {', '.join(missing[:20])}"
                      f"{'…' if len(missing) > 20 else ''}")
    system = ACSystem(snap, sorted(rail), gnd, ports, shorts, setup.settings, s, progress)

    freqs = s.frequencies()
    f_field = s.field_frequencies()
    zb = np.zeros((len(f_field), len(ports), len(ports)), complex)
    progress.stage(f"Field solves: {len(f_field)} frequencies, {len(ports)} ports, "
                   f"{system.n} unknowns")
    t1 = time.perf_counter()
    try:
        for k, f in enumerate(f_field):
            progress.check_cancel()
            progress.info(f"  [{k + 1}/{len(f_field)}] solving {fmt_hz(f)}")
            zb[k] = system.port_impedance(2 * np.pi * f, progress)
            progress.fraction((k + 1) / len(f_field))
            if k == 0:
                progress.info(f"  first frequency took {time.perf_counter() - t1:.2f} s "
                              f"({system.stats.method})")
    finally:
        system.close()
    system.stats.times["field"] = time.perf_counter() - t1
    progress.info(f"Field solves done in {system.stats.times['field']:.2f} s")

    with progress.timed(f"Sweep: {len(freqs)} frequencies"):
        z = terminate(_interpolate(f_field, zb, freqs), freqs, ports)
    probe_ports = [p for p in ports if p.kind == "probe"]
    curves = [ProbeCurve(p.name, z[:, i], p.element.target_ohm, p.element.target_hz,
                         p.element.target_mask()) for i, p in enumerate(probe_ports)]
    for c in curves:
        zmax, fmax = c.worst(freqs)
        verdict = {None: "", True: " PASS", False: " FAIL"}[c.passed(freqs)]
        progress.info(f"  {c.name}: |Z| {np.min(c.mag) * 1e3:.3g} to {np.max(c.mag) * 1e3:.4g} mΩ; "
                      f"worst {zmax * 1e3:.4g} mΩ at {fmax / 1e6:.4g} MHz{verdict}")
    return ACResult(rail=vrm_name, rail_nets=sorted(rail), ground=gnd, freqs=freqs,
                    probes=curves, field_freqs=f_field, caps=caps, missing_caps=missing,
                    links=links, plane_capacitance_f=system.total_plane_capacitance,
                    stats=system.stats, warnings=notes + system.warnings,
                    solve_s=time.perf_counter() - t0)
