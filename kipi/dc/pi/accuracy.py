"""Adjustable regulator output accuracy from its feedback divider and reference limits."""

from dataclasses import dataclass
from itertools import product
from typing import Optional


@dataclass(frozen=True)
class DcAccuracy:
    minimum_v: float
    maximum_v: float


def feedback_divider_accuracy(vfb_min: float, vfb_max: float,
                              rtop_ohm: float, rtop_tolerance_pct: float,
                              rbottom_ohm: float, rbottom_tolerance_pct: float,
                              leakage_max_a: float) -> Optional[DcAccuracy]:
    """The lowest and highest output voltage the divider can produce, or None for invalid
    inputs.

    Positive FB leakage is current flowing out of the divider into the FB pin, which
    raises the regulator output by ``I_leak * Rtop``; negative leakage lowers it. The
    leakage may be anywhere within +/- ``leakage_max_a``. The minimum and maximum are the
    extremes over every corner of the reference, both resistor tolerances and that leakage.
    """
    values = (vfb_min, vfb_max, rtop_ohm, rtop_tolerance_pct,
              rbottom_ohm, rbottom_tolerance_pct, leakage_max_a)
    if any(x < 0 for x in values):
        return None
    if (not 0 < vfb_min <= vfb_max or rtop_ohm <= 0 or rbottom_ohm <= 0
            or rtop_tolerance_pct >= 100 or rbottom_tolerance_pct >= 100):
        return None

    rt_min = rtop_ohm * (1 - rtop_tolerance_pct / 100)
    rt_max = rtop_ohm * (1 + rtop_tolerance_pct / 100)
    rb_min = rbottom_ohm * (1 - rbottom_tolerance_pct / 100)
    rb_max = rbottom_ohm * (1 + rbottom_tolerance_pct / 100)
    # Rtop scales both the divider gain and the leakage term, so which end of it is worst
    # depends on the leakage: try every corner.
    corners = [vfb * (1 + rt / rb) + leak * rt
               for vfb, rt, rb, leak in product((vfb_min, vfb_max), (rt_min, rt_max),
                                                (rb_min, rb_max),
                                                (-leakage_max_a, leakage_max_a))]
    return DcAccuracy(min(corners), max(corners))
