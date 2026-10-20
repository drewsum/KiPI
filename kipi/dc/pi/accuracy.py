"""Adjustable regulator output accuracy from its feedback divider and reference limits."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DcAccuracy:
    nominal_v: float
    minimum_v: float
    maximum_v: float


def feedback_divider_accuracy(vfb_min: float, vfb_nom: float, vfb_max: float,
                              rtop_ohm: float, rtop_tolerance_pct: float,
                              rbottom_ohm: float, rbottom_tolerance_pct: float,
                              leakage_nom_a: float, leakage_max_a: float) -> Optional[DcAccuracy]:
    """Return nominal and tolerance-corner output voltages, or None for invalid inputs.

    Positive FB leakage is current flowing into the feedback node, which raises the
    regulator output by ``I_leak * Rtop``. The low corner combines the low reference,
    low top resistor, high bottom resistor and nominal leakage. The high corner combines
    the high reference, high top resistor, low bottom resistor and maximum leakage.
    """
    values = (vfb_min, vfb_nom, vfb_max, rtop_ohm, rtop_tolerance_pct,
              rbottom_ohm, rbottom_tolerance_pct, leakage_nom_a, leakage_max_a)
    if any(x < 0 for x in values):
        return None
    if (not 0 < vfb_min <= vfb_nom <= vfb_max or rtop_ohm <= 0 or rbottom_ohm <= 0
            or rtop_tolerance_pct >= 100 or rbottom_tolerance_pct >= 100
            or leakage_max_a < leakage_nom_a):
        return None

    rt_min = rtop_ohm * (1 - rtop_tolerance_pct / 100)
    rt_max = rtop_ohm * (1 + rtop_tolerance_pct / 100)
    rb_min = rbottom_ohm * (1 - rbottom_tolerance_pct / 100)
    rb_max = rbottom_ohm * (1 + rbottom_tolerance_pct / 100)
    nominal = vfb_nom * (1 + rtop_ohm / rbottom_ohm) + leakage_nom_a * rtop_ohm
    minimum = vfb_min * (1 + rt_min / rb_max) + leakage_nom_a * rt_min
    maximum = vfb_max * (1 + rt_max / rb_min) + leakage_max_a * rt_max
    return DcAccuracy(nominal, minimum, maximum)
