"""Reading part values from footprint fields."""

from __future__ import annotations

import math
import re
from typing import Optional, Tuple

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


# A chip part's own series inductance (#159), from the length between its terminals.
# Vishay's measurements (technical note 60107, "Frequency Response of Thin Film Chip
# Resistors") put an 0402's at about 0.4 nH and an 0603's at about 0.6 nH with wrap-around
# terminations, end connections included; a line through them, 0.1 nH + 0.3 nH per mm,
# gives the other sizes. A wide-terminal part (0612, 1020) has its terminals on its long
# sides, so the length between them is its short one: KiCad's names put that first.
CHIP_ESL_BASE_H = 0.1e-9
CHIP_ESL_PER_MM_H = 0.3e-9
# Imperial size codes: the length between the terminals, mm.
_CHIP_LENGTH_MM = {"0201": 0.6, "0402": 1.0, "0603": 1.6, "0805": 2.0, "1206": 3.2,
                   "1210": 3.2, "1218": 3.2, "1812": 4.5, "2010": 5.0, "2512": 6.3,
                   "0306": 0.8, "0508": 1.25, "0612": 1.6, "1020": 2.5, "1225": 3.2}
# "R_0603_1608Metric": the metric code (mm x 10); "RESC1608X55N" (IPC-7351): the same;
# "R0603", "0805": the imperial code.
_METRIC_RE = re.compile(r"(\d\d)(\d\d)Metric", re.IGNORECASE)
_IPC_RE = re.compile(r"(?:RES|CAP|IND)C(\d\d)(\d\d)X", re.IGNORECASE)
_IMPERIAL_RE = re.compile(r"(?<![\d.])(\d{4})(?![\d.])")


def chip_length_mm(footprint: str) -> Optional[float]:
    """The length between a chip part's terminals in mm, from its footprint's name
    ("Resistor_SMD:R_0603_1608Metric": 1.6), or None."""
    name = (footprint or "").split(":")[-1]
    m = _METRIC_RE.search(name) or _IPC_RE.search(name)
    if m:
        return int(m.group(1)) / 10 or None
    for code in _IMPERIAL_RE.findall(name):
        if code in _CHIP_LENGTH_MM:
            return _CHIP_LENGTH_MM[code]
    return None


def chip_esl_h(length_mm: float) -> float:
    """The series inductance of a chip part ``length_mm`` long between its terminals."""
    return CHIP_ESL_BASE_H + CHIP_ESL_PER_MM_H * length_mm


def part_esl(snap: Optional[BoardSnapshot], ref: str) -> Optional[Tuple[float, str]]:
    """Part ``ref``'s own series inductance from its package: henries, and what it was
    read from ("0603 footprint", "pads 1.6 mm apart"). None when it cannot be told."""
    if snap is None:
        return None
    fp = next((f for f in snap.footprints if f.reference == ref), None)
    if fp is None:
        return None
    length = chip_length_mm(fp.footprint)
    if length is not None:
        return chip_esl_h(length), f"{fp.footprint.split(':')[-1]} footprint"
    # No size in the name: a two-pad part's pad centres are about as far apart as its
    # terminals.
    pads = [p for p in snap.pads if p.footprint == ref]
    if len(pads) == 2:
        (x0, y0), (x1, y1) = pads[0].position, pads[1].position
        length = math.hypot(x1 - x0, y1 - y0)
        if 0 < length <= 15:
            return chip_esl_h(length), f"pads {length:.2g} mm apart"
    return None


def bead_esl_h(z_ohm: float, f_hz: float, dcr_ohm: float = 0.0) -> Optional[float]:
    """A ferrite bead's inductance from its impedance |Z| at ``f_hz``, a point on its
    inductive slope below its peak: |Z|² = DCR² + (2πf·L)². None if |Z| ≤ DCR."""
    if f_hz <= 0 or z_ohm <= dcr_ohm:
        return None
    return math.sqrt(z_ohm ** 2 - dcr_ohm ** 2) / (2 * math.pi * f_hz)
