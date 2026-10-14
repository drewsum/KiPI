"""Target impedance and frequency from a device's transient spec.

The PDN must hold the rail within its AC tolerance while the load steps by ``i_step``, so

    Z_target = V_ac / I_step

up to the frequency content of the step. A step that rises in ``t_rise = I_step / slew``
has its spectrum's knee at

    f_target = 1 / (pi * t_rise)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class Target:
    z_ohm: float          # target impedance
    t_rise_s: float       # rise time of the current step
    f_hz: float           # frequency up to which the target applies


def target_from_transient(v_ac_v: float, i_step_a: float,
                          slew_a_per_s: float) -> Optional[Target]:
    """The target for an AC tolerance ``v_ac_v``, a load step ``i_step_a`` and its slew
    rate ``slew_a_per_s``; None unless all three are positive."""
    if not (v_ac_v > 0 and i_step_a > 0 and slew_a_per_s > 0):
        return None
    t_rise = i_step_a / slew_a_per_s
    return Target(v_ac_v / i_step_a, t_rise, 1.0 / (math.pi * t_rise))
