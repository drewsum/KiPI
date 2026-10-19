"""Reading part values from footprint fields."""

from __future__ import annotations

import re
from typing import Optional

from kipi.core.model.board import BoardSnapshot

_PREFIX = {"": 1.0, "R": 1.0, "r": 1.0, "E": 1.0, "m": 1e-3, "u": 1e-6, "µ": 1e-6,
           "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
# "10m", "4.7 kΩ", "0.005", "5mR", "10 mohm", plus RKM codes "4k7", "0R1" and "R005".
# Anything after the value ("1% 0603", "/1W") is ignored.
_RES_RE = re.compile(r"^\s*(\d*\.?\d+)?\s*([RrEmuµkKMG]?)(\d*)\s*(?:Ω|(?i:ohms?)|R)?"
                     r"(?=$|[\s,;/_(])")
_RESISTOR_REF_RE = re.compile(r"^R\d")


def parse_resistance(value: str) -> Optional[float]:
    """Ohms from a resistor's value field, or None if it isn't a resistance."""
    m = _RES_RE.match(value or "")
    if not m:
        return None
    num, prefix, frac = m.groups()
    if frac:                                  # RKM code: the letter is the decimal point
        if num is not None and "." in num:
            return None
        base = float(f"{num or '0'}.{frac}")
    elif num is not None:
        base = float(num)
    else:
        return None
    return base * _PREFIX[prefix]


def resistor_ohms(snap: Optional[BoardSnapshot], ref: str) -> Optional[float]:
    """The value of resistor ``ref`` (reference R<n>) in ohms, if it can be read."""
    if snap is None or not _RESISTOR_REF_RE.match(ref or ""):
        return None
    value = next((f.value for f in snap.footprints if f.reference == ref), "")
    return parse_resistance(value)


# "100n", "4.7uF", "10 µF 6.3V X5R", "1u0", "4n7", "0.1", "22p". A bare number is farads
# only if it has a unit; "0.1" alone is read as microfarads (the usual schematic shorthand).
_CAP_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": None}
_CAP_RE = re.compile(r"^\s*(\d*\.?\d+)?\s*([pnuµμm]?)(\d*)\s*(F|f)?(?=$|[\s,;/_(])")
_CAPACITOR_REF_RE = re.compile(r"^C\d")


def parse_capacitance(value: str) -> Optional[float]:
    """Farads from a capacitor's value field, or None if it isn't a capacitance."""
    m = _CAP_RE.match(value or "")
    if not m:
        return None
    num, prefix, frac, unit = m.groups()
    if frac:                                  # "4n7", "1u0": the letter is the decimal point
        if not prefix or (num is not None and "." in num):
            return None
        base = float(f"{num or '0'}.{frac}")
    elif num is not None:
        base = float(num)
    else:
        return None
    scale = _CAP_PREFIX[prefix]
    if scale is None:
        scale = 1.0 if unit else 1e-6
    return base * scale


def is_capacitor_ref(ref: str) -> bool:
    return bool(_CAPACITOR_REF_RE.match(ref or ""))


# "4.7uH", "10 µH 3A", "1u0", "470n", "2R2" (RKM: µH, the usual power-inductor code),
# "0.47". A bare number is henries only if it has a unit; else microhenries.
_IND_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
               "R": 1e-6, "": None}
_IND_RE = re.compile(r"^\s*(\d*\.?\d+)?\s*([pnuµμmR]?)(\d*)\s*(H|h)?(?=$|[\s,;/_(])")
_INDUCTOR_REF_RE = re.compile(r"^L\d")


def parse_inductance(value: str) -> Optional[float]:
    """Henries from an inductor's value field, or None if it isn't an inductance."""
    m = _IND_RE.match(value or "")
    if not m:
        return None
    num, prefix, frac, unit = m.groups()
    if frac:                                  # "4u7", "2R2": the letter is the decimal point
        if not prefix or (num is not None and "." in num):
            return None
        base = float(f"{num or '0'}.{frac}")
    elif num is not None:
        if prefix == "R":                     # "10R" is a resistor's code, not an inductor's
            return None
        base = float(num)
    else:
        return None
    scale = _IND_PREFIX[prefix]
    if scale is None:
        scale = 1.0 if unit else 1e-6
    return base * scale


def is_inductor_ref(ref: str) -> bool:
    return bool(_INDUCTOR_REF_RE.match(ref or ""))


# "10uF 6.3V X5R", "100n/50V", "4.7u 16 V", "1u 6V3" (RKM). The rating is the first
# voltage in the field.
_VOLT_RE = re.compile(r"(?:^|[\s,;/_(])(\d*\.?\d+)\s*(?:V(\d*)|(?i:volts?))(?=$|[\s,;/_)])")
_CLASS2_RE = re.compile(r"(?i)(?:^|[^A-Z0-9])([XYZ][4-8][PRSTUV])(?=$|[^A-Z0-9])")
_CLASS1_RE = re.compile(r"(?i)(?:^|[^A-Z0-9])(C0G|COG|NP0|NPO)(?=$|[^A-Z0-9])")


def parse_voltage_rating(value: str) -> Optional[float]:
    """A capacitor's voltage rating from its value field ("10uF 6.3V X5R": 6.3), or None."""
    m = _VOLT_RE.search(value or "")
    if not m:
        return None
    num, frac = m.group(1), m.group(2)
    if frac:                                  # "6V3": the V is the decimal point
        if "." in num:
            return None
        return float(f"{num}.{frac}")
    return float(num) or None


def is_class2(value: str) -> bool:
    """Whether a capacitor's value field names a class II ceramic dielectric (X5R, X7R,
    X6S, Y5V...), which loses capacitance with DC bias."""
    return bool(_CLASS2_RE.search(value or ""))


def parse_dielectric(value: str) -> str:
    """The ceramic dielectric a capacitor's value field names, as "X7R", "C0G" (also for
    NP0)..., or "" when it names none."""
    m = _CLASS2_RE.search(value or "")
    if m:
        return m.group(1).upper()
    return "C0G" if _CLASS1_RE.search(value or "") else ""
