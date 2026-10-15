"""Heuristics for spotting power and ground nets."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from kipi.core.model.board import BoardSnapshot

_POWER_RE = re.compile(
    r"""(^|[/_\-.+])(
        v(cc|dd|ee|ss|in|out|bus|bat|sys|core|io|ref|aux|pp|nn|mot|drv)\w* |
        p?gnd\w* | agnd | dgnd | gnd\w* | earth | chassis |
        pwr\w* | power\w* | vbatt? | supply\w* |
        \+?\d+v\d* | \d+v\d+ | -\d+v\d* | v\d+(p\d+)?
    )($|[/_\-.])""",
    re.IGNORECASE | re.VERBOSE,
)
_GROUND_RE = re.compile(r"(^|[/_\-.])(p?gnd\w*|agnd|dgnd|vss\w*|earth|chassis)($|[/_\-.])",
                        re.IGNORECASE)
_POWER_CLASS_RE = re.compile(r"pwr|power|supply|rail", re.IGNORECASE)


def is_power_name(name: str) -> bool:
    return bool(name) and bool(_POWER_RE.search(name))


def is_ground_name(name: str) -> bool:
    return bool(name) and bool(_GROUND_RE.search(name))


def classify_nets(snap: BoardSnapshot) -> Dict[str, str]:
    """Map net name -> 'ground' | 'power' | 'signal'.

    A net counts as power if its name or net class looks like a rail, or if it
    owns a copper zone and also connects to at least four pads.
    """
    zone_nets = {z.net for z in snap.zones if z.net}
    pad_count: Dict[str, int] = {}
    for p in snap.pads:
        if p.net:
            pad_count[p.net] = pad_count.get(p.net, 0) + 1

    out: Dict[str, str] = {}
    for n in snap.nets:
        if not n.name:
            continue
        if is_ground_name(n.name):
            out[n.name] = "ground"
        elif (is_power_name(n.name) or _POWER_CLASS_RE.search(n.netclass or "")
              or (n.name in zone_nets and pad_count.get(n.name, 0) >= 4)):
            out[n.name] = "power"
        else:
            out[n.name] = "signal"
    return out


def sorted_nets(snap: BoardSnapshot) -> List[str]:
    """Nets with power first, then ground, then signals, each group alphabetical."""
    kinds = classify_nets(snap)
    order = {"power": 0, "ground": 1, "signal": 2}
    return sorted(kinds, key=lambda n: (order[kinds[n]], n.lower()))


_RAIL_RE = re.compile(r"(\d+)([vV.,pP])(\d*)")
# A minus sign that starts the name or a hierarchical sheet's part of it: "-5V", "/AFE/-12V".
_NEGATIVE_RE = re.compile("(^|/)[-\u2212]$")


def voltage_from_name(net: str) -> Optional[float]:
    """A rail's voltage read from its name, or None if the name doesn't give one.

    '+3.3V' -> 3.3, '3V3' -> 3.3, 'V3P3' -> 3.3, '1V8' -> 1.8, '+12V' -> 12, '-5V' -> -5,
    'VDD_0V75' -> 0.75, 'VBUS' -> 5, ground nets -> 0.
    """
    if not net:
        return None
    if is_ground_name(net):
        return 0.0
    m = _RAIL_RE.search(net)
    if not m:
        return 5.0 if "vbus" in net.lower() else None
    whole, frac = m.group(1), m.group(3)
    volts = float(f"{whole}.{frac}") if frac else float(whole)
    return -volts if _NEGATIVE_RE.search(net[:m.start()]) else volts


def guess_voltage(net: str) -> float:
    """Best guess at a rail's voltage from its name, for pre-filling a new VRM: the
    :func:`voltage_from_name`, or 1 V when the name doesn't give one."""
    v = voltage_from_name(net)
    return 1.0 if v is None else v
