"""Adding a load's stack-up against its tolerances (#167).

Each side is added apart, as far below nominal as the parts take the load and as far above:

    DC    = the DC drop (or boost) + the VRM's DC accuracy        against the DC tolerance
    AC    = the ripple + the transient                            against the AC tolerance
    total = DC + AC                                               against the tolerance

A check needs every part it adds; until then it can still FAIL (the parts only add), but not
PASS. The load PASSes when every tolerance it has is met with all its parts known.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from kipi.tolerance.model import LoadEntry

EPS_V = 1e-12
Pair = Tuple[float, float]
#: The parts each check adds.
CHECK_PARTS = {"dc": ("dc", "accuracy"), "ac": ("ripple", "transient"),
               "total": ("dc", "accuracy", "ripple", "transient")}
CHECK_NAMES = {"dc": "DC", "ac": "AC", "total": "Total"}


def deviation_limits(nominal: Optional[float], lo, hi, pct: bool, dv: bool = False):
    """A tolerance as (below, above): how far under and over ``nominal`` it lets the load
    go, volts, either None where that end is not set (or cannot be placed)."""
    from kipi.dc.pi.model import range_unit, range_volts
    if nominal is None or (lo is None and hi is None):
        return None, None
    v_lo, v_hi = range_volts(nominal, lo, hi, range_unit(pct, dv))
    return (None if v_lo is None else nominal - v_lo,
            None if v_hi is None else v_hi - nominal)


def load_limits(load, nominal) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """{"dc"|"ac"|"total": (below, above) allowed} for the load's three tolerances."""
    return {
        "total": deviation_limits(nominal, load.tol_min, load.tol_max, load.tol_pct,
                                  load.tol_dv),
        "dc": deviation_limits(nominal, load.dc_tol_min, load.dc_tol_max, load.dc_tol_pct,
                               load.dc_tol_dv),
        "ac": deviation_limits(nominal, load.ac_tol_min, load.ac_tol_max, load.ac_tol_pct,
                               load.ac_tol_dv),
    }


def has_any_tolerance(load) -> bool:
    return any(x is not None for x in (load.tol_min, load.tol_max, load.dc_tol_min,
                                       load.dc_tol_max, load.ac_tol_min, load.ac_tol_max))


@dataclass
class Check:
    kind: str                                   # "dc", "ac", "total"
    below_v: float
    above_v: float
    allowed: Tuple[Optional[float], Optional[float]]
    complete: bool                              # every part known
    passed: Optional[bool]                      # None: no tolerance, or not known yet

    @property
    def has_limit(self) -> bool:
        return self.allowed != (None, None)

    def margins(self) -> Tuple[Optional[float], Optional[float]]:
        """Volts to spare below and above (negative: over), None where not limited."""
        return tuple(None if a is None else a - v
                     for a, v in zip(self.allowed, (self.below_v, self.above_v)))


@dataclass
class Stack:
    load: str
    vrm: str                                    # "" when no VRM drives the rail
    nominal: Optional[float]
    parts: Dict[str, Optional[Pair]] = field(default_factory=dict)   # None: not known
    checks: Dict[str, Check] = field(default_factory=dict)
    accuracy_set: bool = False

    @property
    def verdict(self) -> Optional[bool]:
        """FAIL if any check fails, PASS once every check with a tolerance passes, else
        None (not known yet, or no tolerance)."""
        judged = [c for c in self.checks.values() if c.has_limit]
        if any(c.passed is False for c in judged):
            return False
        if judged and all(c.passed for c in judged):
            return True
        return None


def judge(below: float, above: float, allowed, complete: bool) -> Optional[bool]:
    lo, hi = allowed
    if lo is None and hi is None:
        return None
    if (lo is not None and below > lo + EPS_V) or (hi is not None and above > hi + EPS_V):
        return False
    return True if complete else None


def stack(load, entry: Optional[LoadEntry], vrm, bw: bool = False,
          not_applicable=()) -> Stack:
    """The load's stack-up from its accepted values (``entry``), its VRM's DC accuracy and,
    through the 20 MHz limit if ``bw``, its ripple and transient. Kinds in
    ``not_applicable`` count as nothing."""
    nominal = vrm.voltage if vrm is not None else None
    out = Stack(load.name, vrm.name if vrm is not None else "", nominal)
    entry = entry or LoadEntry()
    for kind in ("dc", "ripple", "transient"):
        c = entry.get(kind)
        out.parts[kind] = (c.sides(bw) if c is not None
                           else (0.0, 0.0) if kind in not_applicable else None)
    if vrm is not None:
        out.parts["accuracy"] = vrm.accuracy_sides()
        out.accuracy_set = vrm.has_accuracy()
    else:
        out.parts["accuracy"] = None
    limits = load_limits(load, nominal)
    for kind, names in CHECK_PARTS.items():
        known = [out.parts[n] for n in names if out.parts[n] is not None]
        below = sum(p[0] for p in known)
        above = sum(p[1] for p in known)
        complete = len(known) == len(names)
        out.checks[kind] = Check(kind, below, above, limits[kind], complete,
                                 judge(below, above, limits[kind], complete))
    return out


# ------------------------------------------------------------------ showing values
def side(v: Optional[float], nominal: Optional[float], unit: str, below: bool) -> str:
    """One side's deviation ``v`` (volts, ≥ 0) in ``unit`` (kipi.dc.pi.vunits): signed mV
    or % from nominal, or the voltage it takes the load to."""
    from kipi.dc.pi import vunits
    if v is None:
        return "–"
    if unit == vunits.VOLTS:
        if nominal is None:
            return "–"
        return f"{nominal - v if below else nominal + v:.4f}"
    x = vunits.difference(-v if below else v, nominal, unit)
    if x is None:
        return "–"
    if abs(x) < 5e-4:
        return "0"
    return f"{x:+.3f}" if unit == vunits.PCT else f"{x:+.2f}"


def pair(p: Optional[Pair], nominal: Optional[float], unit: str) -> str:
    """"−12.30 / +4.00" (mV), "−1.025 / +0.333" (%), or "1.1877 / 1.2040" (V)."""
    if p is None:
        return ""
    return (f"{side(p[0], nominal, unit, True).replace('-', '−')} / "
            f"{side(p[1], nominal, unit, False)}")


def unit_suffix(unit: str) -> str:
    from kipi.dc.pi import vunits
    return {vunits.MV: "mV", vunits.PCT: "%", vunits.VOLTS: "V"}[unit]
