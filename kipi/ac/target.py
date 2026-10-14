"""Target impedance and frequency from a device's transient spec.

The PDN must hold the rail within its AC tolerance while the load steps by ``i_step``, so

    Z_target = V_ac / I_step

up to the frequency content of the step. A step that rises in ``t_rise = I_step / slew``
has its spectrum's knee at

    f_target = 1 / (pi * t_rise)

A probe can instead have a custom target mask: piecewise linear on the log-log impedance
plot between ``(frequency, impedance)`` corner points.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np


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


def mask_at(points: Sequence[Tuple[float, float]], freqs) -> np.ndarray:
    """A custom target mask at ``freqs``: the ``(f_hz, z_ohm)`` corner points joined by
    straight lines on the log-log plot, held flat below the first point and above the
    last."""
    pts = sorted((float(f), float(z)) for f, z in points)
    f = np.asarray(freqs, dtype=float)
    if not pts:
        return np.full(f.shape, np.nan)
    lf = np.log10([p[0] for p in pts])
    lz = np.log10([p[1] for p in pts])
    return 10.0 ** np.interp(np.log10(f), lf, lz)


def target_at(freqs, target_ohm: Optional[float] = None, target_hz: Optional[float] = None,
              points: Optional[Sequence[Tuple[float, float]]] = None) -> Optional[np.ndarray]:
    """The target impedance at each of ``freqs`` (NaN where none applies), or None without
    a target. A custom mask (``points``) takes the place of the flat target, which holds
    ``target_ohm`` up to ``target_hz`` (the whole sweep if none, or if the sweep starts
    above it)."""
    f = np.asarray(freqs, dtype=float)
    if points:
        return mask_at(points, f)
    if not target_ohm:
        return None
    t = np.full(f.shape, float(target_ohm))
    if target_hz and (f <= target_hz).any():
        t[f > target_hz] = np.nan
    return t
