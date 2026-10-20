"""The aggressor's operating point, setup checks and guesses from the board (#69).

The topology follows from the VRM's voltages: a switcher whose input is above its output
is a buck, one whose input is below its output a boost. The output current is the DC
setup's load on the VRM's output rail (its loads plus the input current of every VRM it
feeds), unless the aggressor gives one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.core.model.parts import is_capacitor_ref, is_inductor_ref, parse_inductance
from kipi.dc.pi.model import (
    PinRef, PISetup, Vrm, _supplied_current, net_groups, rail_nominal, vin_range,
)
from kipi.noise.model import Aggressor, AggressorPhase


class NoiseError(ValueError):
    """The aggressor cannot be solved as set up."""


@dataclass
class OperatingPoint:
    topology: str                   # "buck" or "boost"
    vin: float
    vout: float
    iout: float                     # total, all phases
    iin: float                      # average input current, all phases
    duty: float
    efficiency: float
    phases: int
    notes: List[str] = field(default_factory=list)   # where each number came from

    @property
    def inductor_a(self) -> float:
        """Average current of one phase's inductor."""
        avg = self.iout if self.topology == "buck" else self.iin
        return avg / max(self.phases, 1)


def smps_vrms(setup: PISetup) -> List[Vrm]:
    return [v for v in setup.vrms if v.kind == "smps"]


def find_vrm(setup: PISetup, agg: Aggressor) -> Vrm:
    vrm = next((v for v in setup.vrms if v.name == agg.vrm), None)
    if vrm is None:
        raise NoiseError(f"Aggressor {agg.name}: choose its VRM")
    if vrm.kind != "smps":
        raise NoiseError(f"Aggressor {agg.name}: VRM {vrm.name} is not a switcher")
    return vrm


def input_voltage(setup: PISetup, agg: Aggressor, vrm: Vrm) -> Tuple[Optional[float], str]:
    """The aggressor's input voltage and where it came from."""
    if agg.vin_v:
        return float(agg.vin_v), "entered"
    lo, hi = vin_range(setup, vrm)
    if lo and hi:
        return 0.5 * (lo + hi), f"middle of {vrm.name}'s input range"
    nom = rail_nominal(setup, vrm.input.net if vrm.input else None, vrm.name)
    if nom:
        return nom, f"target voltage of the VRM feeding {vrm.input.net}"
    return (lo or hi), (f"{vrm.name}'s input range" if (lo or hi) else "")


def load_current(setup: PISetup, agg: Aggressor, vrm: Vrm) -> Tuple[float, str]:
    """The aggressor's output current and where it came from."""
    if agg.load_a is not None:
        return float(agg.load_a), "entered"
    groups = net_groups(setup, [vrm.output.net])
    amps = _supplied_current(setup, groups, groups[vrm.output.net], frozenset({vrm.name}))
    return amps, f"DC setup's loads on {vrm.output.net}"


def topology(agg: Aggressor, vin: float, vout: float) -> str:
    if agg.topology in ("buck", "boost"):
        return agg.topology
    if math.isclose(vin, vout, rel_tol=1e-6):
        raise NoiseError(f"Aggressor {agg.name}: input and output are both {vout:g} V, so "
                         "it is neither a buck nor a boost; set its topology")
    return "buck" if vin > vout else "boost"


def _mean(xs, default):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else default


def operating_point(setup: PISetup, agg: Aggressor) -> OperatingPoint:
    vrm = find_vrm(setup, agg)
    vout = float(vrm.voltage)
    vin, vin_from = input_voltage(setup, agg, vrm)
    if not vin or vin <= 0 or vout <= 0:
        raise NoiseError(f"Aggressor {agg.name}: its input voltage is unknown; enter it, "
                         f"or give {vrm.name} an input rail or input range")
    topo = topology(agg, vin, vout)
    if topo == "buck" and vin <= vout or topo == "boost" and vin >= vout:
        raise NoiseError(f"Aggressor {agg.name}: a {topo} cannot take {vin:g} V to "
                         f"{vout:g} V")
    iout, iout_from = load_current(setup, agg, vrm)
    eta = min(max(vrm.efficiency or 0.9, 0.05), 1.0)
    n = max(len(agg.phases), 1)
    # A buck's duty is raised by its losses; only what was entered counts (the defaults
    # only damp the ringing).
    rhs = _mean([p.rds_on_hs_ohm for p in agg.phases], 0.0)
    rls = _mean([p.rds_on_ls_ohm for p in agg.phases], 0.0)
    dcr = _mean([p.dcr_ohm for p in agg.phases], 0.0)
    if topo == "buck":
        iin = vout * iout / (eta * vin)
        iph = iout / n
        duty = (vout + iph * (dcr + rls)) / (vin - iph * (rhs - rls))
    else:
        iin = vout * iout / (eta * vin)
        duty = 1.0 - vin * eta / vout
    if agg.duty is not None:
        duty = float(agg.duty)
    if not 0.0 < duty < 1.0:
        raise NoiseError(f"Aggressor {agg.name}: duty cycle {duty:.3g} is outside 0-1")
    notes = [f"Topology: {topo}" + (" (from the voltages)" if agg.topology == "auto" else ""),
             f"Vin {vin:g} V ({vin_from}), Vout {vout:g} V ({vrm.name})",
             f"Iout {iout:.4g} A ({iout_from})",
             f"Duty {duty:.4f}" + (" (entered)" if agg.duty is not None else "")]
    return OperatingPoint(topo, vin, vout, iout, iin, duty, eta, n, notes)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def validate_noise(setup: PISetup, snap: Optional[BoardSnapshot], agg: Aggressor) -> List[str]:
    """What stops the aggressor from being solved, one sentence each (empty if nothing)."""
    errors = []
    try:
        operating_point(setup, agg)
    except NoiseError as e:
        errors.append(str(e))
    if agg.fsw_hz <= 0:
        errors.append(f"Aggressor {agg.name}: switching frequency must be above 0")
    if agg.rise_s <= 0 or agg.fall_s <= 0:
        errors.append(f"Aggressor {agg.name}: rise and fall times must be above 0")
    elif agg.fsw_hz > 0 and agg.rise_s + agg.fall_s >= 1.0 / agg.fsw_hz:
        errors.append(f"Aggressor {agg.name}: the edges take longer than a switching "
                      "period")
    if not agg.phases:
        errors.append(f"Aggressor {agg.name}: add its phases (Fill from VRM)")
    refs = {f.reference for f in snap.footprints} if snap is not None else None
    for i, ph in enumerate(agg.phases, 1):
        where = f"Aggressor {agg.name}, phase {i}"
        if ph.inductance_h <= 0:
            errors.append(f"{where}: enter its inductance")
        if refs is not None:
            missing = [r for r in [ph.inductor, *ph.input_caps, *ph.output_caps,
                                   *(p.ref for p in (ph.high_side, ph.low_side) if p)]
                       if r and r not in refs]
            if missing:
                errors.append(f"{where}: not on the board: {', '.join(sorted(set(missing)))}")
    return errors


# ---------------------------------------------------------------------------
# Guesses from the board
# ---------------------------------------------------------------------------

def _pads_by_ref(snap: BoardSnapshot) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for p in snap.pads:
        if p.footprint:
            out.setdefault(p.footprint, []).append(p)
    return out


def _centroid(pads) -> Tuple[float, float]:
    xs = [p.position[0] for p in pads]
    ys = [p.position[1] for p in pads]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _two_net_parts(by_ref, net_a: str, net_b: str, test) -> List[str]:
    """References of two-terminal parts passing ``test`` with one pad on each net."""
    out = []
    for ref, pads in by_ref.items():
        if not test(ref):
            continue
        nets = {p.net for p in pads}
        if nets == {net_a, net_b}:
            out.append(ref)
    return sorted(out)


def seed_phases(snap: BoardSnapshot, setup: PISetup, vrm: Vrm, topo: str
                ) -> List[AggressorPhase]:
    """One phase per VRM phase, filled from the board where it can be read:

    * the high-side pins: the VRM's input pins (buck) or output pins (boost);
    * the low-side pins: its ground pins;
    * the inductor: an inductor between a net of the device and the output (buck) or
      input (boost) rail, and the switch node is that device net;
    * the capacitors on the input and output rails to ground, each given to the phase
      whose device is nearest.
    """
    by_ref = _pads_by_ref(snap)
    values = {f.reference: f.value for f in snap.footprints}
    phases = []
    vin_net = vrm.input.net if vrm.input else ""
    vout_net = vrm.output.net
    gnd = vrm.ground.net if vrm.ground else ""
    for vp in vrm.all_phases():
        hs = vp.input if topo == "buck" else vp.output
        hs = PinRef(hs.ref, hs.net, list(hs.pins)) if hs else None
        ls = PinRef(vp.ground.ref, vp.ground.net, list(vp.ground.pins)) if vp.ground else None
        ph = AggressorPhase(high_side=hs, low_side=ls)
        dev_refs = {r.ref for r in (vp.input, vp.output, vp.ground) if r}
        dev_nets = {p.net for r in dev_refs for p in by_ref.get(r, [])}
        rail = vout_net if topo == "buck" else vin_net
        for ref, pads in sorted(by_ref.items()):
            if not is_inductor_ref(ref) or len({p.net for p in pads}) != 2:
                continue
            nets = {p.net for p in pads}
            if rail in nets:
                sw = (nets - {rail}).pop()
                if sw in dev_nets and sw not in (vin_net, vout_net, gnd):
                    ph.inductor, ph.switch_net = ref, sw
                    break
        if not ph.inductor:          # the VRM's own pins may be on its inductor
            ph.inductor = next((r for r in sorted(dev_refs) if is_inductor_ref(r)), "")
        if ph.inductor:
            ph.inductance_h = parse_inductance(values.get(ph.inductor, "")) or 0.0
        phases.append(ph)
    if gnd and phases:
        centers = [_centroid(by_ref.get(vp.ground.ref, []) or by_ref.get(vp.output.ref, []))
                   if (vp.ground and by_ref.get(vp.ground.ref)) or by_ref.get(vp.output.ref)
                   else (0.0, 0.0) for vp in vrm.all_phases()]

        def nearest(ref: str) -> AggressorPhase:
            cx, cy = _centroid(by_ref[ref])
            i = min(range(len(centers)),
                    key=lambda k: (centers[k][0] - cx) ** 2 + (centers[k][1] - cy) ** 2)
            return phases[i]

        for net, attr in ((vin_net, "input_caps"), (vout_net, "output_caps")):
            if not net:
                continue
            for ref in _two_net_parts(by_ref, net, gnd, is_capacitor_ref):
                getattr(nearest(ref), attr).append(ref)
    return phases


def guess_ends(snap: BoardSnapshot, net: str) -> Tuple[Optional[PinRef], Optional[PinRef]]:
    """A guess at a victim's driver and receiver pins: the net's two pads farthest apart,
    the driver the one on an IC (U…) where only one of them is."""
    _, _, pads, _ = snap.items_on_net(net)
    pads = [p for p in pads if p.footprint]
    if not pads:
        return None, None
    if len(pads) == 1:
        p = pads[0]
        return PinRef(p.footprint, net, [p.number]), None
    best, pair = -1.0, (pads[0], pads[1])
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            d = (a.position[0] - b.position[0]) ** 2 + (a.position[1] - b.position[1]) ** 2
            if d > best:
                best, pair = d, (a, b)
    a, b = pair
    if b.footprint.startswith("U") and not a.footprint.startswith("U"):
        a, b = b, a
    return PinRef(a.footprint, net, [a.number]), PinRef(b.footprint, net, [b.number])


def pin_position(snap: BoardSnapshot, pin: Optional[PinRef]) -> Optional[Tuple[float, float]]:
    """The centre of a pin reference's pads."""
    if pin is None:
        return None
    pads = [p for p in snap.pads if p.footprint == pin.ref and p.net == pin.net
            and (not pin.pins or p.number in pin.pins)]
    return _centroid(pads) if pads else None
