"""Adjustable regulator output accuracy from its feedback divider and reference limits."""

from dataclasses import dataclass
from itertools import product
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

    Positive FB leakage is current flowing out of the divider into the FB pin, which
    raises the regulator output by ``I_leak * Rtop``; negative leakage lowers it. The
    nominal leakage is signed, and the leakage may be anywhere within +/- ``leakage_max_a``.
    The minimum and maximum are the extremes over every corner of the reference, both
    resistor tolerances and that leakage range.
    """
    values = (vfb_min, vfb_nom, vfb_max, rtop_ohm, rtop_tolerance_pct,
              rbottom_ohm, rbottom_tolerance_pct, leakage_max_a)
    if any(x < 0 for x in values):
        return None
    if (not 0 < vfb_min <= vfb_nom <= vfb_max or rtop_ohm <= 0 or rbottom_ohm <= 0
            or rtop_tolerance_pct >= 100 or rbottom_tolerance_pct >= 100
            or leakage_max_a < abs(leakage_nom_a)):
        return None

    rt_min = rtop_ohm * (1 - rtop_tolerance_pct / 100)
    rt_max = rtop_ohm * (1 + rtop_tolerance_pct / 100)
    rb_min = rbottom_ohm * (1 - rbottom_tolerance_pct / 100)
    rb_max = rbottom_ohm * (1 + rbottom_tolerance_pct / 100)
    nominal = vfb_nom * (1 + rtop_ohm / rbottom_ohm) + leakage_nom_a * rtop_ohm
    # Rtop scales both the divider gain and the leakage term, so which end of it is worst
    # depends on the leakage: try every corner.
    corners = [vfb * (1 + rt / rb) + leak * rt
               for vfb, rt, rb, leak in product((vfb_min, vfb_max), (rt_min, rt_max),
                                                (rb_min, rb_max),
                                                (-leakage_max_a, leakage_max_a))]
    return DcAccuracy(nominal, min(corners), max(corners))
