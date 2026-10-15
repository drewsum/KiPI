"""What an AC solve covers, and whether the setup allows it.

An AC solve covers one *rail*: the net group driven by one VRM (its output nets, plus
nets joined to them by series links), against one ground net. VRMs carry no impedance
in the AC model; they say which nets are supplies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.core.model.parts import is_capacitor_ref
from kipi.dc.pi.model import PISetup, Vrm, net_groups


@dataclass
class CapCandidate:
    ref: str
    value: str
    nets: List[str]                      # distinct nets of its pads, sorted
    footprint: str = ""                  # library footprint, "library:name"


def capacitor_candidates(snap: BoardSnapshot, nets) -> List[CapCandidate]:
    """Capacitors (references C<n>) whose pads are all on the given nets, on at least two
    different nets: the parts a capacitor model can be assigned to."""
    nets = set(nets)
    pads_by_ref: Dict[str, List[str]] = {}
    for p in snap.pads:
        if p.footprint and is_capacitor_ref(p.footprint):
            pads_by_ref.setdefault(p.footprint, []).append(p.net)
    values = {f.reference: f.value for f in snap.footprints}
    footprints = {f.reference: f.footprint for f in snap.footprints}
    out = []
    for ref, pad_nets in pads_by_ref.items():
        distinct = sorted(set(pad_nets))
        if len(distinct) >= 2 and all(n and n in nets for n in distinct):
            out.append(CapCandidate(ref, values.get(ref, ""), distinct,
                                    footprints.get(ref, "")))
    return sorted(out, key=lambda c: _ref_key(c.ref))


def _ref_key(ref: str):
    import re
    m = re.match(r"([A-Za-z_]*)(\d*)(.*)", ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else -1, m.group(3))


def rail_nets(setup: PISetup, vrm: Vrm, gnd: str = "") -> frozenset:
    """The nets of the rail driven by ``vrm``: its output net plus the nets series links
    join to it. Links to the ground net ``gnd`` are parts across the rail, not joins."""
    import copy
    joined = copy.copy(setup)
    joined.links = [k for k in setup.links if gnd not in (k.side_a.net, k.side_b.net)]
    return net_groups(joined)[vrm.output.net]


def default_ground(vrm: Optional[Vrm]) -> str:
    return vrm.ground.net if vrm is not None and vrm.ground else ""


def rail_caps(snap: BoardSnapshot, setup: PISetup, rail: frozenset,
              gnd: str) -> Tuple[List[tuple], List[str]]:
    """Capacitors in the solve: [(ref, model, rail net, pads rail side, pads ground side)],
    and the references of capacitors between the rail and ground that have no model."""
    from kipi.ac.model import caps_dict
    models = caps_dict(setup.caps)
    pads_by_ref: Dict[str, list] = {}
    for p in snap.pads:
        if p.footprint and is_capacitor_ref(p.footprint):
            pads_by_ref.setdefault(p.footprint, []).append(p)
    used, missing = [], []
    for ref, pads in sorted(pads_by_ref.items(), key=lambda t: _ref_key(t[0])):
        a = [p for p in pads if p.net in rail]
        b = [p for p in pads if p.net == gnd]
        if not a or not b or len(a) + len(b) != len(pads):
            continue
        if ref in models:
            used.append((ref, models[ref], a[0].net, [p.id for p in a], [p.id for p in b]))
        else:
            missing.append(ref)
    return used, missing


def validate_ac(setup: PISetup, vrm_name: str, gnd: str) -> List[str]:
    """Problems that stop an AC solve of the rail driven by ``vrm_name`` (empty = OK)."""
    errors: List[str] = []
    vrm = next((v for v in setup.vrms if v.name == vrm_name), None)
    if vrm is None:
        return ["Pick the rail to simulate (add a VRM on it first)" if not setup.vrms
                else "Pick the rail to simulate"]
    if not gnd:
        errors.append("Pick the ground net")
    s = setup.ac
    if not (s.f_start_hz > 0 and s.f_stop_hz > s.f_start_hz):
        errors.append("The stop frequency must be above the start frequency")
    if s.points_per_decade < 1:
        errors.append("Use at least one point per decade")

    names = [m.name for m in setup.caps]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"Capacitor model names must be unique: {', '.join(dupes)}")
    seen: Dict[str, str] = {}
    for m in setup.caps:
        missing = [what for what, v in (("capacitance", m.capacitance_f), ("ESL", m.esl_h),
                                        ("ESR", m.esr_ohm)) if not v > 0]
        if missing and m.refs:
            errors.append(f"Capacitor model {m.name}: enter its {', '.join(missing)}")
        for ref in m.refs:
            if ref in seen:
                errors.append(f"{ref} is in two capacitor models ({seen[ref]}, {m.name})")
            seen[ref] = m.name

    names = [p.name for p in setup.probes]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"Probe names must be unique: {', '.join(dupes)}")
    rail = rail_nets(setup, vrm, gnd)
    on_rail = [p for p in setup.probes if p.rail and p.rail.net in rail]
    for p in setup.probes:
        if p.rail is None or p.ground is None:
            errors.append(f"Probe {p.name}: set both its supply pins and its ground pins")
    for p in on_rail:
        if p.ground and gnd and p.ground.net != gnd:
            errors.append(f"Probe {p.name}: its ground pins are on {p.ground.net}, not on "
                          f"the ground being simulated ({gnd})")
        if p.target_custom:
            fs = [f for f, _ in p.target_points]
            if not p.target_points:
                errors.append(f"Probe {p.name}: add at least one point to its custom target")
            elif not all(f > 0 and z > 0 for f, z in p.target_points):
                errors.append(f"Probe {p.name}: every custom target point needs a frequency "
                              "and an impedance above zero")
            elif len(set(fs)) != len(fs):
                errors.append(f"Probe {p.name}: two custom target points are at the same "
                              "frequency")
        elif p.target_ohm is not None and not p.target_ohm > 0:
            errors.append(f"Probe {p.name}: the target impedance must be positive")
    if not on_rail:
        errors.append(f"Add a probe on {' / '.join(sorted(rail))} (the device whose supply "
                      "impedance you want)")
    if gnd and gnd in rail:
        errors.append(f"{gnd} is part of the rail; pick a ground net")
    for k in setup.links:
        if k.esl_h is not None and k.esl_h < 0:
            errors.append(f"Series link {k.name}: ESL cannot be negative")
    return errors
