"""The AC part of the user's setup: capacitor models, probe points and sweep settings.

These live in the same setup file as the DC power tree (:class:`kipi.dc.pi.model.PISetup`),
which also holds the VRMs and series links that both analyses share. Nothing here
imports the DC model, so the setup can import this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

# DC bias (#74): a class II ceramic (X5R, X7R...) keeps 1 / (1 + k (V / V_rated)^BIAS_EXP)
# of its capacitance at V volts, with k set by what it keeps at its rated voltage. It is
# a typical curve's shape; the datasheet's curve is better where there is one.
BIAS_EXP = 1.5
DEFAULT_KEPT_AT_RATED_PCT = 40.0
REPORT_FORMATS = ("html", "pdf")          # what a report can be written as (#119)

# Aging (#109): a class II ceramic loses a fixed share of its capacitance per decade of
# hours since it was last heated past its Curie point. Its rated capacitance is taken as
# at AGING_REF_HOURS (the usual referee time); parts are not aged before that.
AGING_REF_HOURS = 1000.0
DEFAULT_AGING_HOURS = 87600.0           # ten years
# Typical aging rates, % per decade of hours; the datasheet's figure is better.
AGING_PCT_PER_DECADE = {"C0G": 0.0, "X8R": 2.0, "X7R": 2.0, "X7S": 2.5, "X7T": 2.5,
                        "X6S": 3.0, "X5R": 3.0, "Z5U": 5.0, "Y5V": 7.0}

# Temperature (#77): a typical part's capacitance change (%) against temperature (°C) for
# each dielectric, from its rated value at 25 °C, joined by straight lines and held flat
# past the ends. Parts differ; the datasheet's curve is better.
CAP_REF_TEMP_C = 25.0
TEMP_CURVES = {
    "C0G": [(-55.0, 0.0), (125.0, 0.0)],
    "X5R": [(-55.0, -10.0), (-25.0, -4.0), (25.0, 0.0), (85.0, -8.0)],
    "X6S": [(-55.0, -12.0), (25.0, 0.0), (105.0, -15.0)],
    "X7R": [(-55.0, -10.0), (-25.0, -4.0), (25.0, 0.0), (85.0, -4.0), (125.0, -12.0)],
    "X7S": [(-55.0, -12.0), (25.0, 0.0), (85.0, -6.0), (125.0, -18.0)],
    "X7T": [(-55.0, -15.0), (25.0, 0.0), (85.0, -10.0), (125.0, -28.0)],
    "X8R": [(-55.0, -10.0), (25.0, 0.0), (125.0, -6.0), (150.0, -14.0)],
    "Z5U": [(10.0, -15.0), (25.0, 0.0), (85.0, -50.0)],
    "Y5V": [(-30.0, -70.0), (0.0, -25.0), (15.0, 0.0), (25.0, 0.0), (50.0, -30.0),
            (85.0, -75.0)],
}
TEMPERATURE_SOURCES = ("none", "fixed", "thermal")

if TYPE_CHECKING:                       # PinRef lives in the setup model, which imports us
    from kipi.dc.pi.model import PinRef


@dataclass
class CapModel:
    """An RLC model shared by one or more capacitors (a group of identical parts)."""
    name: str
    capacitance_f: float = 0.0          # all three are required (> 0) before solving
    esl_h: float = 0.0
    esr_ohm: float = 0.0
    refs: List[str] = field(default_factory=list)   # footprint references using this model
    # DC bias (#74): with a rating, the capacitance is derated by the DC voltage across
    # each part (not for a C0G dielectric).
    rated_v: Optional[float] = None     # DC voltage rating
    kept_at_rated_pct: float = DEFAULT_KEPT_AT_RATED_PCT   # capacitance left at rated_v
    # Aging (#109): the dielectric ("" when not set: not aged) and its aging rate.
    dielectric: str = ""
    aging_pct_per_decade: float = 0.0
    # Manufacturing tolerance: the part's capacitance tolerance, % of nominal (None when
    # not set). Worst case for a PDN is the low end, so the sweep settings apply it
    # subtractively: the model keeps 1 - tolerance of its capacitance.
    mfg_tolerance_pct: Optional[float] = None

    @property
    def dc_bias(self) -> bool:
        """Whether the model is derated by DC bias: it has a voltage rating."""
        return bool(self.rated_v and self.rated_v > 0) and self.dielectric != "C0G"

    def capacitance_at(self, volts: float, hours: Optional[float] = None,
                       temp_c: Optional[float] = None,
                       mfg_tolerance: bool = False) -> float:
        """One part's capacitance with ``volts`` DC across it after ``hours`` of operation
        (None: not aged) at ``temp_c`` (None: 25 °C): derated when the model estimates DC
        bias (has a rating), aged and moved with temperature when it has a dielectric, and
        at the low end of its manufacturing tolerance with ``mfg_tolerance``; else its
        capacitance as entered."""
        c = (self.capacitance_f * self.within_tolerance(mfg_tolerance) * self.aged(hours)
             * self.at_temperature(temp_c))
        if not (self.dc_bias and volts):
            return c
        kept = min(max(self.kept_at_rated_pct, 1.0), 100.0) / 100.0
        k = 1.0 / kept - 1.0
        return c / (1.0 + k * (abs(volts) / self.rated_v) ** BIAS_EXP)

    def within_tolerance(self, on: bool = True) -> float:
        """The share of its nominal capacitance a part at the low end of its manufacturing
        tolerance has: 1.0 without a tolerance, or when ``on`` is false."""
        tol = self.mfg_tolerance_pct
        if not (on and tol and tol > 0):
            return 1.0
        return max(1.0 - min(tol, 100.0) / 100.0, 0.0)

    def at_temperature(self, temp_c: Optional[float]) -> float:
        """The share of its 25 °C capacitance the part has at ``temp_c``."""
        curve = TEMP_CURVES.get(self.dielectric)
        if curve is None or temp_c is None:
            return 1.0
        import numpy as np
        ts, pct = zip(*curve)
        return max(1.0 + float(np.interp(temp_c, ts, pct)) / 100.0, 0.0)

    def aged(self, hours: Optional[float]) -> float:
        """The share of its capacitance the part keeps after ``hours`` of aging."""
        if not (self.dielectric and hours and hours > AGING_REF_HOURS):
            return 1.0
        loss = self.aging_pct_per_decade / 100.0 * math.log10(hours / AGING_REF_HOURS)
        return max(1.0 - loss, 0.0)

    def impedance(self, omega):
        """Complex impedance at angular frequency ``omega`` (scalar or array)."""
        return self.esr_ohm + 1j * omega * self.esl_h + 1.0 / (1j * omega * self.capacitance_f)

    def srf_hz(self) -> Optional[float]:
        """Self-resonant frequency of the part alone (no mounting)."""
        import math
        if self.capacitance_f > 0 and self.esl_h > 0:
            return 1.0 / (2 * math.pi * math.sqrt(self.capacitance_f * self.esl_h))
        return None


@dataclass
class Probe:
    """Where the PDN impedance is looked into: a device's supply pins and ground pins."""
    name: str
    rail: "PinRef"
    ground: "PinRef"
    target_ohm: Optional[float] = None    # target impedance, flat up to target_hz
    target_hz: Optional[float] = None
    # A custom target mask in place of the flat one: (f_hz, z_ohm) corner points, joined
    # by straight lines on the log-log plot. The flat values are kept while it is in use.
    target_custom: bool = False
    target_points: List[Tuple[float, float]] = field(default_factory=list)

    def target_mask(self) -> Optional[List[Tuple[float, float]]]:
        """The custom mask's points, sorted by frequency, when it is in use."""
        return sorted(self.target_points) if self.target_custom and self.target_points             else None


@dataclass
class ACSettings:
    # Sweep
    f_start_hz: float = 1e3
    f_stop_hz: float = 300e6
    points_per_decade: int = 250
    # Field (copper) solves per decade; the sweep in between is interpolated. 0 = solve the
    # copper at every sweep point (exact, slow).
    field_points_per_decade: int = 20
    # Mesh: coarser than DC by default; refined toward plated holes, where via inductance
    # is decided.
    max_edge_mm: float = 1.0
    grading: float = 0.3
    min_edge_mm: float = 0.03
    # Grade the mesh only at plated holes near the rail (#123, kipi.ac.solver.near_rail);
    # off: at every hole of the rail and its ground.
    grade_near_rail_only: bool = True
    # Dielectric
    loss_tangent: float = 0.02
    default_er: float = 4.3               # where the stackup has no dielectric constant
    # Last rail (VRM name) and ground net chosen for a solve.
    rail: str = ""
    ground: str = ""
    html_report: bool = False             # unused: reports are written on demand (#185)
    report_format: str = "html"           # "html" or "pdf" (#119)
    # Derate the capacitor models that estimate DC bias (#74); off: every model as entered.
    dc_bias: bool = True
    # Take the capacitor models that have a manufacturing tolerance to the low end of it.
    mfg_tolerance: bool = False
    # Age the capacitor models that have a dielectric (#109) by this many hours.
    aging: bool = False
    aging_hours: float = DEFAULT_AGING_HOURS
    # Temperature (#77): "none" (copper at the DC settings' reference temperature and the
    # capacitors at 25 °C), "fixed" (everything at temperature_c) or "thermal" (the last
    # DC thermal co-simulation's copper temperatures, temperature_c where it has none).
    temperature: str = "none"
    temperature_c: float = 25.0

    def __post_init__(self):
        self.report_format = report_format(self.report_format)

    def frequencies(self):
        import numpy as np
        decades = np.log10(self.f_stop_hz / self.f_start_hz)
        n = max(2, int(round(decades * self.points_per_decade)) + 1)
        return np.logspace(np.log10(self.f_start_hz), np.log10(self.f_stop_hz), n)

    def field_frequencies(self):
        import numpy as np
        if self.field_points_per_decade <= 0 or \
                self.field_points_per_decade >= self.points_per_decade:
            return self.frequencies()
        decades = np.log10(self.f_stop_hz / self.f_start_hz)
        n = max(4, int(np.ceil(decades * self.field_points_per_decade)) + 1)
        return np.logspace(np.log10(self.f_start_hz), np.log10(self.f_stop_hz), n)


def report_format(value) -> str:
    """A saved report format, or HTML where it is missing or unknown."""
    value = str(value or "").lower()
    return value if value in REPORT_FORMATS else "html"


def caps_dict(caps: List[CapModel]) -> Dict[str, CapModel]:
    """Footprint reference -> its model."""
    return {ref: m for m in caps for ref in m.refs}
