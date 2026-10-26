"""The switcher's currents through the board's own copper (#69): the capacitor current
split, the rails' ripple and the ground noise between a victim's driver and receiver.

AC mode's 2.5D field model (:class:`kipi.ac.solver.fem.ACSystem`) is built for the
converter's input and output rails and their ground, with ports:

* **sources**, per phase: the pulsed current i_HS between the switching device's
  high-side and ground pins (buck: drawn from VIN; boost: delivered to VOUT), and the
  inductor current i_L between the inductor's rail pad and the device's ground pins;
* **parts**: every modelled capacitor on either rail (derated at its rail's voltage) and
  any series link inside them;
* **observers**: for each victim, its driver's ground pads against its receiver's.

The board's port impedance matrix is solved at a few frequencies per decade and
interpolated onto the harmonics (as AC mode's sweep does). With the parts on their ports,
the sources' currents divide among the capacitors through the copper's real impedance,
the rails ripple by the transfer impedance at the device's own pins, and the ground under
a victim's driver and receiver moves apart by the transfer impedance to their pads: the
**common-impedance** coupling, the switching current's return sharing copper with the
victim's.

The mesh of the rails and ground is also where the hot loops are traced (#187,
:mod:`.copper`), so it is built once, before the waveforms, with every port's pads as
terminals (:func:`board_ports`), and handed to the field model.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.pi.model import PinRef, PISetup, biased_link, net_groups


@dataclass
class BoardModelResult:
    cap_currents: Dict[str, np.ndarray]          # into each capacitor's + pad, (F,)
    cap_rail: Dict[str, str]                     # "in" or "out"
    input_ripple: Optional[np.ndarray]
    output_ripple: Optional[np.ndarray]
    ground_diff: Dict[str, np.ndarray]           # victim -> V(driver gnd) − V(receiver gnd)
    field_freqs: np.ndarray
    unknowns: int = 0
    plane_capacitance_f: float = 0.0
    missing_caps: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    probe_ripple: Dict[str, np.ndarray] = field(default_factory=dict)   # V(rail) − V(gnd)


@dataclass
class BoardPorts:
    """The board model's nets and ports, before the waveforms exist."""
    gnd: str
    rail: frozenset                              # the rails' nets
    grounds: frozenset                           # the ground's nets (#171)
    in_rail: frozenset
    buck: bool
    ports: list                                  # PortDef
    src: List[int]                               # the source ports, two per phase
    shorts: list
    cap_rail: Dict[str, str]
    missing: List[str]
    notes: List[str]
    obs_probes: List[str]
    obs_victims: List[str]

    def terminal_ids(self) -> List[str]:
        """Every pad the mesh needs as a terminal."""
        return list(dict.fromkeys(
            [pid for p in self.ports for pid in (*p.plus_pads, *p.minus_pads)]
            + [pid for a, b in self.shorts for pid in (*a, *b)]))

    def ground_nets(self) -> List[str]:
        return [self.gnd, *sorted(self.grounds - {self.gnd})]


def ground_pads(snap: BoardSnapshot, ref: str, gnd: str) -> List[str]:
    """Pad ids of footprint ``ref`` on the ground net."""
    _, _, pads, _ = snap.items_on_net(gnd)
    return [p.id for p in pads if p.footprint == ref]


def _pads(snap: BoardSnapshot, pin: Optional[PinRef]) -> List[str]:
    if pin is None:
        return []
    _, _, pads, _ = snap.items_on_net(pin.net)
    return [p.id for p in pads if p.footprint == pin.ref and (not pin.pins
                                                              or p.number in pin.pins)]


def aggressor_ground(setup: PISetup, agg) -> str:
    """The aggressor's ground net: its VRM's, else its first phase's low-side pins'."""
    from kipi.noise.checks import find_vrm
    vrm = find_vrm(setup, agg)
    return vrm.ground.net if vrm.ground else next(
        (ph.low_side.net for ph in agg.phases if ph.low_side), "")


def aggressor_nets(setup: PISetup, agg) -> frozenset:
    """The aggressor's ground and its input and output rails' nets: the planes its
    current flows in."""
    from kipi.ac.checks import ground_nets, rail_nets
    from kipi.noise.checks import find_vrm
    vrm = find_vrm(setup, agg)
    gnd = aggressor_ground(setup, agg)
    vin_net = vrm.input.net if vrm.input else ""
    in_rail = (net_groups(setup, [vin_net]).get(vin_net, frozenset([vin_net]))
               if vin_net else frozenset())
    nets = rail_nets(setup, vrm, gnd) | in_rail | ground_nets(setup, vrm, gnd)
    return frozenset(nets) - {""}


def board_ports(snap: BoardSnapshot, setup: PISetup, agg, op,
                victim_grounds: Dict[str, Tuple[List[str], List[str]]], probes=(),
                dc_result=None) -> BoardPorts:
    """The board model's ports: the phases' sources, the rails' capacitors and links, the
    AC probes to give the ripple at, and each victim's ground difference.
    ``victim_grounds`` maps a victim to its driver's and receiver's ground pad ids.
    ``dc_result``, the last DC power-tree solve if any, gives the DC voltage across each
    capacitor (else its rail's nominal) and the current through each diode link (else an
    estimate from the loads beyond it), as AC mode's solve does."""
    from kipi.ac.bias import dc_link_currents, dc_volts
    from kipi.ac.checks import ground_nets, rail_caps, rail_nets
    from kipi.ac.solver.fem import ACError, PortDef
    from kipi.noise.checks import find_vrm
    vrm = find_vrm(setup, agg)
    gnd = aggressor_ground(setup, agg)
    if not gnd:
        raise ACError("the aggressor has no ground net (set its low-side pins)")
    vin_net = vrm.input.net if vrm.input else ""
    groups = net_groups(setup, [vin_net] if vin_net else [])
    out_rail = rail_nets(setup, vrm, gnd)
    in_rail = groups.get(vin_net, frozenset([vin_net])) if vin_net else frozenset()
    grounds = ground_nets(setup, vrm, gnd)        # tied to gnd by series links (#171)
    rail = frozenset(out_rail | in_rail) - grounds
    buck = op.topology == "buck"

    ports: List[PortDef] = []
    src: List[int] = []
    for i, ph in enumerate(agg.phases, 1):
        hs, ls = _pads(snap, ph.high_side), _pads(snap, ph.low_side)
        ind_net = vrm.output.net if buck else vin_net
        ind = [p.id for p in snap.pads if p.footprint == ph.inductor
               and p.net in (out_rail if buck else in_rail)] if ph.inductor else []
        if not hs or not ls or not ind:
            raise ACError(f"phase {i} needs its high-side and low-side pins and an "
                          f"inductor with a pad on {ind_net}")
        if buck:     # i_HS leaves VIN at the device, i_L enters VOUT at the inductor
            ports.append(PortDef(f"I_HS{i}", "probe", ls, hs))
            ports.append(PortDef(f"I_L{i}", "probe", ind, ls))
        else:        # i_HS enters VOUT at the rectifier, i_L leaves VIN at the inductor
            ports.append(PortDef(f"I_HS{i}", "probe", hs, ls))
            ports.append(PortDef(f"I_L{i}", "probe", ls, ind))
        src += [len(ports) - 2, len(ports) - 1]
    caps, missing = rail_caps(snap, setup, rail, grounds)
    # A capacitor without a model gets its value from the board and typical parasitics,
    # as in the waveforms' own split (kipi.noise.parasitics).
    from kipi.ac.model import CapModel
    from kipi.core.model.parts import parse_capacitance
    from kipi.noise.parasitics import FALLBACK_C_F, FALLBACK_ESL_H, FALLBACK_ESR_OHM
    values = {f.reference: f.value for f in snap.footprints}
    for ref in missing:
        pads = [p for p in snap.pads if p.footprint == ref]
        a = [p for p in pads if p.net in rail]
        b = [p for p in pads if p.net in grounds]
        c = parse_capacitance(values.get(ref, "")) or FALLBACK_C_F
        caps.append((ref, CapModel(ref, c, FALLBACK_ESL_H, FALLBACK_ESR_OHM, [ref]),
                     a[0].net, [p.id for p in a], [p.id for p in b]))
    cap_rail: Dict[str, str] = {}
    solved_v = dc_volts(snap, dc_result, caps) if setup.ac.dc_bias else {}
    for ref, model, net, a, b in caps:
        volts = solved_v.get(ref, op.vin if net in in_rail else op.vout)
        if setup.ac.dc_bias:
            model = dataclasses.replace(model, capacitance_f=model.capacitance_at(volts))
        ports.append(PortDef(ref, "cap", a, b, model))
        cap_rail[ref] = "in" if net in in_rail else "out"
    shorts = []
    inside = set(rail) | grounds
    notes: List[str] = []
    link_currents = dc_link_currents(dc_result)
    for k in setup.links:
        if k.side_a.net in inside and k.side_b.net in inside:
            a, b = _pads(snap, k.side_a), _pads(snap, k.side_b)
            if k.is_short():
                shorts.append((a, b))
            else:
                solved = k.name in link_currents
                k = biased_link(setup, k, link_currents)
                if k.is_diode:
                    src = "from the DC solve" if solved else "the DC loads beyond it, estimated"
                    notes.append(f"Series link {k.name}: diode small-signal model at "
                                 f"{k.bias_a:.3g} A, {src}")
                ports.append(PortDef(k.name, "link", a, b, k))
    if solved_v:
        notes.append(f"Capacitors derated at the DC solve's voltage across them: "
                     f"{len(solved_v)} of {len(caps)}")
    # The AC probes the ripple is wanted at: open ports between their rail and ground pins.
    obs_probes: List[str] = []
    for pr in probes:
        plus, minus = _pads(snap, pr.rail), _pads(snap, pr.ground)
        if pr.rail is None or pr.rail.net not in rail:
            where = pr.rail.net if pr.rail else "(none)"
            notes.append(f"Probe {pr.name}: its rail {where} is not one of the aggressor's "
                         "rails: no ripple shown there")
        elif pr.ground is None or pr.ground.net not in grounds:
            notes.append(f"Probe {pr.name}: its ground pins are not on {gnd}: no ripple "
                         "shown there")
        elif not plus or not minus:
            notes.append(f"Probe {pr.name}: its pins have no pads on the board: no ripple "
                         "shown there")
        else:
            ports.append(PortDef(f"probe {pr.name}", "probe", plus, minus))
            obs_probes.append(pr.name)
    obs_victims: List[str] = []
    for name, (drv, rcv) in victim_grounds.items():
        if not drv or not rcv:
            notes.append(f"Victim {name}: its driver or receiver has no pad on {gnd}: no "
                         "shared-ground coupling")
            continue
        if set(drv) == set(rcv):
            continue
        ports.append(PortDef(f"gnd {name}", "probe", drv, rcv))
        obs_victims.append(name)
    return BoardPorts(gnd, rail, grounds, in_rail, buck, ports, src, shorts, cap_rail,
                      missing, notes, obs_probes, obs_victims)


def solve_board(snap: BoardSnapshot, setup: PISetup, agg, op, wf,
                victim_grounds: Dict[str, Tuple[List[str], List[str]]],
                progress: ProgressReporter = NULL, probes=(),
                mesh=None, dc_result=None) -> BoardModelResult:
    """The board model of the aggressor's rails at the waveforms' harmonics.
    ``victim_grounds`` maps a victim to its driver's and receiver's ground pad ids;
    ``probes``, AC probes to give the rails' ripple at (#157); ``mesh``, the rails' and
    ground's mesh with its node kinds, already built (else it is built here);
    ``dc_result``, the last DC solve, for the parts' bias (:func:`board_ports`)."""
    from kipi.ac.solver.fem import ACSystem
    from kipi.ac.solver.sweep import _interpolate, element_currents, terminate_transfer
    bp = board_ports(snap, setup, agg, op, victim_grounds, probes, dc_result)
    ports, src, buck = bp.ports, bp.src, bp.buck
    currents = [c for pw in wf.phases for c in (pw.pulsed.c, pw.inductor.c)]
    notes = list(bp.notes)
    first_probe = len(ports) - len(bp.obs_probes) - len(bp.obs_victims)
    obs_probes, obs_victims = bp.obs_probes, bp.obs_victims

    # 0 V is the ground: the minus side of the first port that ends on it.
    pad_net = {p.id: p.net for p in snap.pads}
    ref_port = next((i for i, p in enumerate(ports)
                     if p.minus_pads and pad_net.get(p.minus_pads[0]) == bp.gnd), 0)
    system = ACSystem(snap, sorted(bp.rail), bp.ground_nets(), ports, bp.shorts,
                      setup.settings, setup.ac, progress, reference_port=ref_port,
                      prebuilt=mesh)
    freqs = wf.freqs()
    f0, f1 = float(freqs[0]), float(freqs[-1])
    n = max(4, int(math.ceil(math.log10(f1 / f0) * setup.noise.field_per_decade)) + 1)
    f_field = np.logspace(math.log10(f0), math.log10(f1), n)
    zb = np.zeros((n, len(ports), len(ports)), complex)
    progress.stage(f"Board model: {n} field solves, {len(ports)} ports, {system.n} unknowns")
    try:
        for k, f in enumerate(f_field):
            progress.check_cancel()
            progress.debug(f"Board model field solve {k + 1}/{n} at {f / 1e6:.4g} MHz")
            zb[k] = system.port_impedance(2 * np.pi * f, progress)
            progress.fraction((k + 1) / n)
    finally:
        system.close()
    if f1 > 1e9:
        notes.append(f"The board model is quasi-static: above about 1 GHz (here to "
                     f"{f1 / 1e9:.3g} GHz) it is approximate")
    z = _interpolate(f_field, zb, freqs)
    i_src = np.stack(currents, axis=1)                                  # (F, S)
    E, per_amp = element_currents(z, freqs, ports, src)
    flows = np.einsum("fes,fs->fe", per_amp, i_src)
    cap_currents = {ports[e].name: -flows[:, j] for j, e in enumerate(E)
                    if ports[e].kind == "cap"}
    obs = (src + [first_probe + j for j in range(len(obs_probes))]
           + [len(ports) - len(obs_victims) + j for j in range(len(obs_victims))])
    zt = terminate_transfer(z, freqs, ports, src, obs)
    # Between a victim's grounds only the copper's resistance (skin effect included) is
    # shared impedance. The inductive part of a ground's potential depends on the path
    # it is measured along: the victim's own loop (its trace over the plane) links that
    # flux, which the inductive coupling counts. (The 2.5D model would also overstate it
    # where a plane faces no rail copper: its current spreads wider than a real return.)
    first_victim = len(src) + len(obs_probes)
    if obs_victims:
        zt[:, first_victim:, :] = zt[:, first_victim:, :].real
    v = np.einsum("fos,fs->fo", zt, i_src)
    # The rails at phase 1's device: buck VIN = −V(I_HS1), VOUT = V(I_L1); boost VOUT =
    # V(I_HS1), VIN = −V(I_L1).
    input_ripple = -v[:, 0] if buck else -v[:, 1]
    output_ripple = v[:, 1] if buck else v[:, 0]
    ground = {name: v[:, first_victim + j] for j, name in enumerate(obs_victims)}
    at_probes = {name: v[:, len(src) + j] for j, name in enumerate(obs_probes)}
    return BoardModelResult(cap_currents, bp.cap_rail, input_ripple, output_ripple, ground,
                            f_field, system.n, system.total_plane_capacitance, bp.missing,
                            notes + system.warnings, at_probes)
