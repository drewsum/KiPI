"""Where a victim's noise couples in, for the 3D view (#152).

The solve gives each segment of the victim's traces its part of the receiver's peak noise,
by mechanism (:attr:`kipi.noise.result.VictimResult.segment_v`). Here those parts are
colored along the traces, the common-ground part at the driver, and the capacitor loops
that induce the inductive part are drawn beside them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

# The 3D view's choices: key, text.
VIEWS = (("off", "Off"), ("total", "Superposition"), ("capacitive", "Capacitive"),
         ("inductive", "Inductive"), ("common", "Common ground"))
_MECH = {"capacitive": ("capacitive",), "inductive": ("inductive",),
         "total": ("capacitive", "inductive"), "common": ()}


@dataclass
class CouplingOverlay:
    """What the 3D view draws: segments and points colored by their part of the peak (mV,
    on a range of ±limit_mv), and the capacitor loops plain. Coordinates are (x, y, z) mm,
    KiCad's."""
    title: str
    segments: np.ndarray                    # (S, 2, 3)
    segment_mv: np.ndarray                  # (S,)
    points: np.ndarray                      # (P, 3)
    point_mv: np.ndarray                    # (P,)
    loops: List[np.ndarray] = field(default_factory=list)     # each (N, 2, 3)
    limit_mv: float = 1.0
    note: str = ""


def coupling_overlay(result, victim: str, view: str) -> Optional[CouplingOverlay]:
    """The overlay of ``victim``'s coupling in ``view`` (a :data:`VIEWS` key), or None
    (view off, no such victim, or a result saved before #152)."""
    if result is None or view not in _MECH:
        return None
    v = next((x for x in getattr(result, "victims", []) if x.name == victim), None)
    if v is None or v.segments_xyz is None or not len(v.segments_xyz):
        return None
    mechs = [m for m in _MECH[view] if m in v.segment_v]
    if mechs:
        seg_mv = 1e3 * sum(v.segment_v[m] for m in mechs)
        segments = v.segments_xyz
    else:
        seg_mv, segments = np.zeros(0), np.zeros((0, 2, 3))
    common = view in ("total", "common") and v.driver_xyz is not None and v.common_v
    points = np.array([v.driver_xyz]) if common else np.zeros((0, 3))
    point_mv = np.array([1e3 * v.common_v]) if common else np.zeros(0)
    loops = (list(result.loop_paths.values())
             if view in ("total", "inductive") else [])
    shown = np.concatenate([np.abs(seg_mv), np.abs(point_mv)])
    limit = float(shown.max()) if len(shown) and shown.max() > 0 else 1.0
    name = dict(VIEWS)[view].lower()
    part = sum(seg_mv) + sum(point_mv)
    note = (f"{v.name}: {part:.3g} mV of its {v.peak_v * 1e3:.4g} mV peak is {name} "
            f"coupling, at {v.peak_t_s * 1e9:.4g} ns into the period")
    if view == "total":
        note = (f"{v.name}: {v.peak_v * 1e3:.4g} mV peak at {v.peak_t_s * 1e9:.4g} ns into "
                f"the period; red adds to it, blue takes from it")
    if view == "common" and not common:
        note = f"{v.name}: no common-ground coupling (it needs the board model)"
    return CouplingOverlay(f"{dict(VIEWS)[view]} share of the peak (mV)", segments, seg_mv,
                           points, point_mv, loops, limit, note)
