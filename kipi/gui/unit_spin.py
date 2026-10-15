"""Value fields that take SI prefixes and units: "4.7n" or "4.7 nF" in a µF field.

A field's own unit is its suffix (" µF", " mΩ", " kHz"). When that is an SI unit, a typed
value may carry any prefix and the unit, in any common spelling (``10n``, ``10 nF``,
``2 mOhm``, ``4k7``, ``3.3 V`` in a mV field), and is converted to the field's unit. Fields
in other units (%, °C, A/µs, µΩ·cm, ...) and unitless ones take plain numbers, as before.

The field still shows its usual number of decimals, but keeps more, and shows them when a
typed value needs them (10 pF in a µF field reads 0.00001 µF, not 0.0000).
"""

from __future__ import annotations

import math
import re
from typing import Optional

from qtpy.QtGui import QValidator
from qtpy.QtWidgets import QDoubleSpinBox

PREFIXES = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}
# Each SI unit a field can be in, with the spellings accepted for it (compared ignoring case).
UNITS = {
    "Ω": ("Ω", "Ω", "ohm", "ohms"),
    "F": ("f",),
    "H": ("h",),
    "Hz": ("hz",),
    "A": ("a", "amp", "amps"),
    "V": ("v", "volt", "volts"),
    "W": ("w",),
    "m": ("m",),
}
EXTRA_DECIMALS = 6
_NUMBER = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$")
# Resistor-code style, with the prefix as the decimal point: 4k7, 2M2, 4R7 (R in Ω only).
_CODE = re.compile(r"\s*(\d+)([pnuµμmkKMGR])(\d+)\s*(.*?)\s*$")
# What a value can look like while it is still being typed.
_PARTIAL = re.compile(r"[\s\d.,eE+\-a-zA-ZµμΩΩ]*")


def split_unit(suffix: str):
    """``(base unit, scale)`` of a field suffix such as " µF" -> ("F", 1e-6).

    The base unit is None when the suffix is not a plain SI unit (%, °C, A/µs, "").
    """
    s = suffix.strip()
    if s in UNITS:
        return s, 1.0
    if len(s) > 1 and s[0] in PREFIXES and s[1:] in UNITS:
        return s[1:], PREFIXES[s[0]]
    return None, 1.0


def _scale(rest: str, base: str) -> Optional[float]:
    """The multiplier for what follows the number, in base units, or None if not valid."""
    folded = rest.casefold()
    for alias in sorted(UNITS[base], key=len, reverse=True):
        if folded.endswith(alias.casefold()):
            rest = rest[:-len(alias)].strip()
            break
    if rest.casefold() == "meg":
        return 1e6
    return PREFIXES.get(rest)


def parse_value(text: str, suffix: str) -> Optional[float]:
    """``text`` as a number in the unit of ``suffix``, or None if it cannot be read."""
    text = text.strip()
    unit = suffix.strip()
    if unit and text.endswith(unit):
        # The field's own unit is still there: clicking into a field selects only the
        # number, so "10n" typed over "1.0000 µF" arrives as "10n µF", meaning 10 nF.
        return parse_value(text[:-len(unit)], suffix)
    base, field_scale = split_unit(suffix)
    m = _NUMBER.fullmatch(text)
    if m and not m.group(2):
        return float(m.group(1))
    if base is None:
        return None
    if m:
        scale = _scale(m.group(2), base)
        if scale is not None:
            return float(m.group(1)) * scale / field_scale
    c = _CODE.fullmatch(text)
    if c:
        whole, mark, frac, rest = c.groups()
        if mark == "R":
            if base != "Ω":
                return None
            mark = ""
        if rest and _scale(rest, base) != 1.0:
            return None
        return float(f"{whole}.{frac}") * PREFIXES[mark] / field_scale
    return None


class UnitSpinBox(QDoubleSpinBox):
    """A :class:`QDoubleSpinBox` that reads SI prefixes and units typed into it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shown_decimals = 2

    def setShownDecimals(self, decimals: int):
        """Show ``decimals`` places normally, and keep a few more for typed values."""
        self._shown_decimals = decimals
        self.setDecimals(decimals + EXTRA_DECIMALS)

    def textFromValue(self, value: float) -> str:
        text = super().textFromValue(value)
        point = str(self.locale().decimalPoint())
        if point not in text:
            return text
        whole, frac = text.split(point, 1)
        frac = frac.rstrip("0").ljust(self._shown_decimals, "0")
        return f"{whole}{point}{frac}" if frac else whole

    def _parse(self, text: str) -> Optional[float]:
        point = str(self.locale().decimalPoint())
        if point != ".":
            text = text.replace(point, ".")
        value = parse_value(text, self.suffix())
        return value if value is not None and math.isfinite(value) else None

    def valueFromText(self, text: str) -> float:
        value = self._parse(text)
        return self.value() if value is None else value

    def validate(self, text: str, pos: int):
        special = self.specialValueText()
        if special and text == special:
            return QValidator.Acceptable, text, pos
        value = self._parse(text)
        if value is not None:
            lo, hi = self.minimum(), self.maximum()
            ok = lo <= round(value, self.decimals()) <= hi
            return (QValidator.Acceptable if ok else QValidator.Intermediate), text, pos
        unit = self.suffix().strip()
        typed = text[:-len(unit)] if unit and text.endswith(unit) else text
        if _PARTIAL.fullmatch(typed):
            return QValidator.Intermediate, text, pos
        return QValidator.Invalid, text, pos
