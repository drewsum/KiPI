# Copyright (c) 2026 Tomachie LLC.
"""Probes on the input pins of each switching converter of a Tomachie ``.pdn.json``."""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from kipi.ac.model import Probe
from kipi.ac.target import (
    STABILITY_MARGIN, _hz, buck_input_current, input_stability_ohm, ripple_mask, vrm_inputs,
)
from kipi.dc.pi.importers.pdn_figures import amps, number
from kipi.dc.pi.importers.tomachie_ac import ac_value
from kipi.dc.pi.model import SWITCHING_KINDS, PISetup, Vrm, net_groups, vin_range


def _input_volts(rail: dict) -> tuple:
    v = rail.get("voltage") or {}
    nom = number(v.get("nominal"))
    lo, hi = number(v.get("min")), number(v.get("max"))
    return (lo if lo is not None else nom), (hi if hi is not None else nom)


def _feeding_vrm(setup: PISetup, v: Vrm, net: str) -> Optional[Vrm]:
    group = net_groups(setup, [net]).get(net, frozenset([net]))
    return next((u for u in setup.vrms if u.name != v.name
                 and any(ph.output.net in group for ph in u.all_phases())), None)


def _add(setup: PISetup, name: str, supply, ground) -> Optional[Probe]:
    if any(p.name == name for p in setup.probes):
        return None
    probe = Probe(name, supply, ground)
    setup.probes.append(probe)
    return probe


def converter_input_probes(setup: PISetup, regs: Dict[str, dict], rails: Dict[str, dict],
                           targets_set: List[str], targets_missing: Dict[str, List[str]],
                           without_ground: List[str]):
    for name, v, k in vrm_inputs(setup):
        reg = regs.get(v.name)
        if reg is None or v.kind not in SWITCHING_KINDS or not isinstance(reg.get("ac"), dict):
            continue
        phases = v.all_phases()
        ph = phases[k]
        ground = v.input_return(ph)
        if ground is None:
            without_ground.append(name)
            continue
        in_rail = rails.get(next(iter(reg.get("input_rails") or []), ""), {})
        v_lo, v_hi = _input_volts(in_rail)
        i_out = amps(reg.get("output_current_max")) or amps(reg.get("output_current"))
        i_out = i_out / len(phases) if i_out else None
        eff = v.efficiency or 1.0
        _stability(setup, v, name, ph.input, ground, v_lo, i_out, eff,
                   targets_set, targets_missing)
        if v.kind == "smps" and v_lo and v.voltage < v_lo:
            _ripple(setup, v, reg, name, ph.input, ground, v_lo, v_hi, i_out, eff,
                    targets_set, targets_missing)


def _stability(setup, v, name, supply, ground, v_lo, i_out, eff, targets_set,
               targets_missing):
    probe = _add(setup, f"{name} (stability)", supply, ground)
    if probe is None:
        return
    p_in = v.voltage * i_out / eff if i_out else None
    ohm = input_stability_ohm(v_lo or 0.0, p_in or 0.0)
    bw = v.bandwidth_khz * 1e3 if v.bandwidth_khz else None
    if ohm is None:
        targets_missing.setdefault("no output current or input voltage for the converter",
                                   []).append(probe.name)
        return
    if bw is None:
        targets_missing.setdefault("no control loop bandwidth for the converter",
                                   []).append(probe.name)
        return
    u = _feeding_vrm(setup, v, supply.net)
    feed = u.bandwidth_khz * 1e3 if u is not None and u.bandwidth_khz else None
    if feed and feed >= bw:
        why = (f"its input rail comes from a board input ({u.name}), taken to hold the rail "
               f"up to {_hz(feed)}, beyond the converter's control loop bandwidth; what "
               "feeds that input is not on the board"
               if u.kind == "source" else
               f"the supply of its input rail ({u.name}) regulates up to {_hz(feed)}, "
               "beyond the converter's control loop bandwidth")
        targets_missing.setdefault(why, []).append(probe.name)
        return
    probe.target_ohm, probe.target_hz = ohm, bw
    probe.target_from = (f"input stability: V_in^2 / P_in = {v_lo:g} V^2 / {p_in:.4g} W, "
                         f"{20 * math.log10(STABILITY_MARGIN):.0f} dB below, up to the "
                         f"converter's control loop bandwidth")
    targets_set.append(f"{probe.name} {ohm * 1e3:.4g} mOhm to {_hz(bw)} "
                       f"({probe.target_from})")


def _ripple(setup, v, reg, name, supply, ground, v_lo, v_hi, i_out, eff, targets_set,
            targets_missing):
    probe = _add(setup, f"{name} (ripple)", supply, ground)
    if probe is None:
        return
    f_sw, _ = ac_value(reg["ac"].get("f_sw"), "hz")
    duties = [v.voltage / (eff * x) for x in (v_lo, v_hi) if x]
    i1 = buck_input_current(i_out or 0.0, duties)
    if not f_sw or i1 is None:
        targets_missing.setdefault("no switching frequency or output current for the "
                                   "converter", []).append(probe.name)
        return
    lo, hi = vin_range(setup, v)
    rooms = [r for r in (None if lo is None else v_lo - lo,
                         None if hi is None or v_hi is None else hi - v_hi) if r is not None]
    if not rooms:
        targets_missing.setdefault("no input range for the converter", []).append(probe.name)
        return
    v_ac = min(rooms)
    if not v_ac > 0:
        targets_missing.setdefault("the input rail's DC range takes the converter's whole "
                                   "input range", []).append(probe.name)
        return
    probe.target_custom, probe.target_points = True, ripple_mask(v_ac, i1, f_sw)
    probe.target_from_hz = f_sw
    probe.target_from = (f"input ripple: {i1 * 1e3:.4g} mA at {_hz(f_sw)} "
                         f"({i_out:.4g} A pulsed), {v_ac * 1e3:.4g} mV from the rail's "
                         "range to the converter's input range")
    targets_set.append(f"{probe.name} {v_ac / i1 * 1e3:.4g} mOhm from {_hz(f_sw)} "
                       f"({probe.target_from})")
