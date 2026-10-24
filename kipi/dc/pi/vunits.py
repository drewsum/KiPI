"""How a DC result's voltages are shown (#177): in volts, in mV from the target voltage
(the VRM's, for a rail), or in % of it from the target. The results tables and the web
report switch between them; a PDF report shows all three.

A voltage from the target is signed: negative is a drop below it, positive a boost
above it (near a remote-sensed VRM's output pins, say).
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

MV, PCT, VOLTS = "mV", "%", "V"
#: (unit, label), the default first.
UNITS: List[Tuple[str, str]] = [(MV, "mV from target"), (PCT, "% from target"),
                                (VOLTS, "Volts")]
DEFAULT = MV


def _bad(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def voltage(v: Optional[float], target: Optional[float], unit: str) -> Optional[float]:
    """The voltage ``v`` in ``unit``: itself, or its signed distance from ``target`` in mV
    or % of it. None where it has none (no voltage, or % of a 0 V target)."""
    if _bad(v):
        return None
    if unit == VOLTS:
        return v
    return difference(v - (target or 0.0), target, unit)


def difference(dv: Optional[float], target: Optional[float], unit: str) -> Optional[float]:
    """A voltage difference ``dv`` (volts) in ``unit``: volts, mV or % of ``target``."""
    if _bad(dv):
        return None
    if unit == VOLTS:
        return dv
    if unit == MV:
        return dv * 1e3
    return 100.0 * dv / abs(target) if target else None


def fmt(x: Optional[float], unit: str, signed: bool = True) -> str:
    """``x`` (already in ``unit``) for a table: 5 decimals in volts, 3 in mV and %, with
    its sign from the target."""
    if _bad(x):
        return ""
    if unit == VOLTS and not signed:
        return f"{x:.5f}"
    return f"{x:+.5f}" if unit == VOLTS else f"{x:+.3f}"


def show(v: Optional[float], target: Optional[float], unit: str) -> str:
    """The voltage ``v`` in ``unit``, for a table."""
    return fmt(voltage(v, target, unit), unit, signed=unit != VOLTS)


def header(name: str, unit: str) -> str:
    """A column header: "Lowest pin (mV)"."""
    return f"{name} ({unit})"


def margin(load) -> Optional[float]:
    """How far a load's pins are inside its tolerance, volts: the closer of its lowest pin
    to the min and its highest pin to the max, negative when outside. None without a
    tolerance or a solved pin."""
    out = []
    lo, hi = getattr(load, "v_low", None), getattr(load, "v_high", None)
    if _bad(hi):
        hi = lo                               # results saved before boosts: lowest pin only
    if load.limit_low is not None and not _bad(lo):
        out.append(lo - load.limit_low)
    if load.limit_high is not None and not _bad(hi):
        out.append(load.limit_high - hi)
    return min(out) if out else None


def tolerance(load, unit: str) -> str:
    """A load's tolerance in ``unit``: "-30.000 / +50.000" from the target in mV or %,
    "3.13500 to 3.46500" in volts, with "—" for an end not set."""
    if load.limit_low is None and load.limit_high is None:
        return ""
    lo, hi = (show(x, load.nominal, unit) if x is not None else "—"
              for x in (load.limit_low, load.limit_high))
    return f"{lo} to {hi}" if unit == VOLTS else f"{lo} / {hi}"


def load_cells(load, unit: str) -> List[str]:
    """A load's mean, lowest pin, highest pin, tolerance and margin in ``unit``."""
    hi = load.v_high if not _bad(load.v_high) else None
    return [show(load.v_effective, load.nominal, unit), show(load.v_low, load.nominal, unit),
            show(hi, load.nominal, unit), tolerance(load, unit),
            fmt(difference(margin(load), load.nominal, unit), unit)]


LOAD_COLUMNS = ["Mean", "Lowest pin", "Highest pin", "Tolerance", "Margin"]
