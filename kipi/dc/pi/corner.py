# Copyright (c) 2026 Tomachie LLC.
"""The DC solve at its typical or its worst case.

A setup can hold, besides each value KiPI solves with, a maximum: each load's current
(and each pin group's) and each series link's resistance. ``corner`` picks one:

* ``typ``: the values as entered.
* ``max``: the maximum where one is given, else the value as entered; the items solved
  without a maximum are listed, so the result says which loads were not at their worst.
  A VRM with a DC accuracy is solved at the end of it nearer 0 V (its lowest output),
  the loads' tolerances and the VRMs' input ranges first fixed in volts at the set
  voltages, so they do not move with it. A board input is a source whose accuracy is its
  stated range (USB's 4.75 to 5.5 V): it is solved at its minimum, and through its cable
  allowance (added to its load line), where it has one.

:func:`resolve` turns a setup into a copy holding only the corner's numbers, before
anything else in the solve looks at it.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kipi.dc.pi.model import net_groups, rail_nominal, range_unit, range_volts, vin_range

CORNERS = ("typ", "max")
LABELS = {"typ": "Typical", "max": "Worst case (max)"}


@dataclass
class CornerInfo:
    corner: str = "typ"
    typical_used: List[str] = field(default_factory=list)   # worst case, no maximum given
    basis: Dict[str, str] = field(default_factory=dict)     # load name -> typ / max / ...
    set_points: Dict[str, Tuple[float, float]] = field(default_factory=dict)  # VRM -> (set, solved)
    no_accuracy: List[str] = field(default_factory=list)    # VRMs solved at their set voltage
    cables: Dict[str, float] = field(default_factory=dict)  # source -> cable allowance, mOhm
    # VRM -> its highest output in its DC accuracy: an LDO's dropout is judged there, the
    # part that needs the most input.
    high_points: Dict[str, float] = field(default_factory=dict)

    def warnings(self) -> List[str]:
        if self.corner != "max":
            return []
        out = []
        if self.set_points:
            out.append("Worst case: these VRMs are solved at the low end of their DC "
                       "accuracy: " + ", ".join(f"{n} {lo:.4g} V (set {v:.4g} V)"
                                                for n, (v, lo) in self.set_points.items()))
        if self.cables:
            out.append("Worst case: these inputs are solved through their cable allowance: "
                       + ", ".join(f"{n} {r:g} mΩ" for n, r in self.cables.items()))
        if self.no_accuracy:
            out.append("Worst case: no DC accuracy is given for these VRMs, so they are "
                       "solved at their set voltage: " + ", ".join(self.no_accuracy))
        if self.typical_used:
            out.append("Worst case: no maximum is given for these, so the typical value is "
                       "solved: " + ", ".join(self.typical_used))
        return out


def _below(mx: Optional[float], typ: float) -> bool:
    """Whether a maximum is below its typical value by more than rounding: a file can
    state the same figure twice through different arithmetic (1.4 and
    1.4000000000000001)."""
    return mx is not None and mx < typ and not math.isclose(mx, typ, rel_tol=1e-9)


def corner_errors(setup) -> List[str]:
    """What makes the corners unsolvable: an unknown corner, or a maximum below its
    typical value."""
    errors = []
    if getattr(setup, "corner", "typ") not in CORNERS:
        errors.append(f"Unknown solve corner {setup.corner!r}")
    for l in setup.loads:
        pairs = [(l.current, l.current_max)] + [(g.current, g.current_max) for g in l.groups]
        if any(_below(mx, typ) for typ, mx in pairs):
            errors.append(f"Load {l.name}: a maximum current is below its typical current")
    for k in setup.links:
        if _below(k.resistance_max, k.resistance):
            errors.append(f"DC connection {k.name}: the maximum resistance is below its "
                          "typical resistance")
        if k.diode_vf and _below(k.diode_vf_max, k.diode_vf):
            errors.append(f"DC connection {k.name}: the maximum forward voltage is below "
                          "its typical forward voltage")
    return errors


def _pick(worst: bool, name: str, typ: float, mx: Optional[float],
          info: CornerInfo) -> Tuple[float, str]:
    if not worst:
        return typ, "typ"
    if mx is not None:
        return mx, "max"
    info.typical_used.append(name)
    return typ, "typ (no max)"


def _fix_in_volts(setup):
    """Every load tolerance and VRM input range given in % or volts from a nominal, as
    volts at the set voltages now, so lowering a set voltage does not move them."""
    groups = net_groups(setup)
    ranges = {v.name: vin_range(setup, v, groups) for v in setup.vrms
              if v.vin_pct or v.vin_dv}
    for l in setup.loads:
        if l.has_tolerance() and (l.tol_pct or l.tol_dv):
            nominal = rail_nominal(setup, l.supply.net, groups=groups)
            if nominal:
                l.tol_min, l.tol_max = l.limits(nominal)
                l.tol_pct = l.tol_dv = False
    for v in setup.vrms:
        if v.name in ranges:
            v.vin_min, v.vin_max = ranges[v.name]
            v.vin_pct = v.vin_dv = False


def _ends(v) -> List[float]:
    return [x for x in range_volts(v.voltage, v.acc_min, v.acc_max,
                                   range_unit(v.acc_pct, v.acc_dv)) if x is not None]


def _high_end(v) -> Optional[float]:
    """The VRM's highest output in its DC accuracy (furthest from 0 V), None without one."""
    if not v.has_accuracy() or not v.voltage:
        return None
    highest = max(_ends(v) + [v.voltage], key=abs)
    return highest if highest != v.voltage else None


def _low_end(v) -> Optional[float]:
    """The VRM's lowest output in its DC accuracy (nearest 0 V on a negative rail), None
    without an accuracy."""
    if not v.has_accuracy() or not v.voltage:
        return None
    lowest = min(_ends(v) + [v.voltage], key=abs)
    return lowest if lowest != v.voltage else None


def _set_points(out, info: CornerInfo):
    lows = {v.name: _low_end(v) for v in out.vrms}
    info.no_accuracy = [v.name for v in out.vrms if not v.has_accuracy()]
    for v in out.vrms:
        high = _high_end(v)
        if high is not None:
            info.high_points[v.name] = high
    if not any(x is not None for x in lows.values()):
        return
    _fix_in_volts(out)
    for v in out.vrms:
        if lows[v.name] is not None:
            info.set_points[v.name] = (v.voltage, lows[v.name])
            v.voltage = lows[v.name]


def resolve(setup):
    """A copy of ``setup`` with every current, resistance and VRM set voltage the
    corner's, and what was picked (:class:`CornerInfo`)."""
    out = copy.deepcopy(setup)
    info = CornerInfo(getattr(setup, "corner", "typ"))
    worst = info.corner == "max"
    if worst:
        _set_points(out, info)
        for v in out.vrms:
            if v.cable_mohm:
                info.cables[v.name] = v.cable_mohm
                v.load_line_mohm += v.cable_mohm
    for l in out.loads:
        if l.groups:
            bases = set()
            for g in l.groups:
                g.current, b = _pick(worst, f"{l.name} [{g.name or ','.join(g.pins)}]",
                                     g.current, g.current_max, info)
                g.current_max = None
                bases.add(b)
            info.basis[l.name] = bases.pop() if len(bases) == 1 else "mixed"
        else:
            l.current, info.basis[l.name] = _pick(worst, l.name, l.current, l.current_max,
                                                  info)
        l.current_max = None
    from kipi.dc.pi.diode_curve import diode_ref, stated_diodes
    for k in stated_diodes(out.links):
        # A diode stated by its datasheet: its model at the corner's forward drop. The
        # curve is typical: the worst case lifts it as much as the drop.
        if k.diode_vf:
            vf, _ = _pick(worst, f"DC connection {k.name} forward voltage", k.diode_vf,
                          k.diode_vf_max, info)
            if k.vf_curve and vf != k.diode_vf:
                k.vf_curve = [[i, v + vf - k.diode_vf] for i, v in k.vf_curve]
            k.diode_vf = vf
        diode_ref(k)
        k.diode_vf_max = k.resistance_max = None
    diodes = {id(k) for k in out.links if k.is_diode}
    for k in out.links:
        if id(k) in diodes:
            continue                    # its model holds its resistance
        if worst and k.resistance_max is None and k.resistance == 0:
            continue                    # a short stays a short: nothing to warn about
        k.resistance, _ = _pick(worst, f"DC connection {k.name}", k.resistance,
                                k.resistance_max, info)
        k.resistance_max = None
    return out, info
