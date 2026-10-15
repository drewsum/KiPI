"""Numbers out of a power-tree file's value fields and datasheet figures.

A value field holds a number or a word ("unknown", "not_specified"); a current is an
object with ``amps`` or one of those words. Datasheet figures are printed strings.

A series part's DC resistance comes only from figures that ARE a DC resistance. A
ferrite's datasheet also prints its impedance in ohms, at 100 MHz; taking that for its DC
resistance would be wrong by orders of magnitude, so figures are chosen by what they
are, never by their unit alone.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

DC_RESISTANCE_SPECS = ("dcr", "dc_resistance", "resistance", "rds_on", "jumper_resistance",
                       "cold_resistance", "r_dc", "rdc", "on_resistance")
OHMS_PER_UNIT = {"ohm": 1.0, "ohms": 1.0, "Ω": 1.0, "mohm": 1e-3, "mΩ": 1e-3,
                 "kohm": 1e3, "kΩ": 1e3, "uohm": 1e-6, "µΩ": 1e-6}
_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def number(value) -> Optional[float]:
    """A value field as a float; None for a word such as "unknown"."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def amps(current) -> Optional[float]:
    """A current object's amps; None for "unknown" or a missing current."""
    return number(current.get("amps")) if isinstance(current, dict) else None


def _printed(text) -> Optional[float]:
    m = _NUMBER.search(str(text or ""))
    return float(m.group()) if m else None


def _ohms(fig: dict, column: str) -> Optional[float]:
    unit = str(fig.get("unit", "")).strip()
    # Exact first: lower-casing turns the ohm sign into a Greek small omega.
    scale = OHMS_PER_UNIT.get(unit, OHMS_PER_UNIT.get(unit.lower()))
    value = _printed(fig.get(column))
    return None if value is None or scale is None else abs(value) * scale


def dc_resistance(element: dict) -> Tuple[Optional[float], Optional[float], List[str]]:
    """(typical, maximum, notes) of a series part's DC resistance in ohms.

    A resistor's own value is its resistance. Otherwise the DC-resistance figures of its
    datasheet: where several are printed (different conditions) the highest is used and
    said so."""
    own = number(element.get("resistance_ohm"))
    if own is not None:
        return own, None, []
    figs = [f for f in element.get("datasheet_figures", [])
            if str(f.get("spec", "")).lower() in DC_RESISTANCE_SPECS]
    typs = [v for v in (_ohms(f, "typ") for f in figs) if v is not None]
    maxs = [v for v in (_ohms(f, "max") for f in figs) if v is not None]
    notes = []
    if len(typs) > 1 or len(maxs) > 1:
        notes.append("several DC-resistance figures printed; the highest of each is used")
    return (max(typs) if typs else None), (max(maxs) if maxs else None), notes
