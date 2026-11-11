# Copyright (c) 2026 Tomachie LLC.
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

TWO_PI = 2.0 * np.pi


def _table(points: Sequence[Sequence[float]]):
    pts = sorted((float(p[0]), float(p[1]), float(p[2])) for p in points
                 if isinstance(p, (list, tuple)) and len(p) == 3)
    return (np.array([p[0] for p in pts]), np.array([p[1] for p in pts]),
            np.array([p[2] for p in pts]))


def table_impedance(points: Sequence[Sequence[float]], omega) -> np.ndarray:
    f_t, r_t, x_t = _table(points)
    f = np.atleast_1d(np.asarray(omega, dtype=float)) / TWO_PI
    if len(f_t) == 0:
        return np.zeros_like(f, dtype=complex)
    r0 = r_t[0]
    fl, rl, xl = (f_t[1:], r_t[1:], x_t[1:]) if f_t[0] <= 0.0 else (f_t, r_t, x_t)
    if len(fl) == 0:
        return np.full_like(f, r0, dtype=complex)
    r = np.empty_like(f)
    x = np.empty_like(f)
    low = f < fl[0]
    r[low] = r0 + (rl[0] - r0) * (f[low] / fl[0])
    x[low] = xl[0] * f[low] / fl[0]
    hi = ~low
    lf = np.log(np.clip(f[hi], fl[0], fl[-1]))
    r[hi] = np.interp(lf, np.log(fl), rl)
    x[hi] = np.interp(lf, np.log(fl), xl)
    return r + 1j * x


def by_current_impedance(curves: List[Dict], amps: float, omega) -> np.ndarray:
    cs = sorted((float(c["a"]), c["points"]) for c in curves if c.get("points"))
    if not cs:
        return np.zeros_like(np.atleast_1d(np.asarray(omega, dtype=float)), dtype=complex)
    a = max(float(amps or 0.0), 0.0)
    if a <= cs[0][0]:
        return table_impedance(cs[0][1], omega)
    for (a0, p0), (a1, p1) in zip(cs, cs[1:]):
        if a <= a1:
            t = 0.0 if a1 == a0 else (a - a0) / (a1 - a0)
            return (1 - t) * table_impedance(p0, omega) + t * table_impedance(p1, omega)
    return table_impedance(cs[-1][1], omega)


def circuit_impedance(c: Dict[str, float], omega) -> np.ndarray:
    w = np.atleast_1d(np.asarray(omega, dtype=float))
    y = 1.0 / (c["rs_ohm"] + 1j * w * c["l_h"]) + 1.0 / c["rp_ohm"] + 1j * w * c["cp_f"]
    return 1.0 / y


def circuit_from(ac: dict) -> Optional[Dict[str, float]]:
    ec = ac.get("equivalent_circuit") if isinstance(ac.get("equivalent_circuit"), dict) else {}
    out = {}
    for k in ("rs_ohm", "l_h", "rp_ohm", "cp_f"):
        v = ec.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            return None
        out[k] = float(v)
    return out


def curves_from(ac: dict) -> List[Dict]:
    zb = ac.get("z_dc_bias") if isinstance(ac.get("z_dc_bias"), dict) else {}
    curves = zb.get("curves") if isinstance(zb.get("curves"), dict) else {}
    return [{"a": float(c["dc_current_a"]), "points": [list(p) for p in c.get("points") or []]}
            for c in curves.get("by_current") or []
            if isinstance(c, dict) and isinstance(c.get("dc_current_a"), (int, float))
            and c.get("points")]


def inductance_at(points: Sequence[Sequence[float]], amps: float) -> float:
    pts = sorted((float(p[0]), float(p[1])) for p in points
                 if isinstance(p, (list, tuple)) and len(p) == 2)
    if not pts:
        return 0.0
    a = abs(float(amps or 0.0))
    if a <= pts[0][0]:
        return pts[0][1]
    for (a0, l0), (a1, l1) in zip(pts, pts[1:]):
        if a <= a1:
            t = 0.0 if a1 == a0 else (a - a0) / (a1 - a0)
            return (1 - t) * l0 + t * l1
    return pts[-1][1]


def l_points_from(e: dict) -> List[List[float]]:
    db = e.get("dc_bias") if isinstance(e.get("dc_bias"), dict) else {}
    return [[float(p[0]), float(p[1])] for p in db.get("points") or []
            if isinstance(p, (list, tuple)) and len(p) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)
            and p[1] > 0]


def points_from(ac: dict) -> List[List[float]]:
    zp = ac.get("z_points") if isinstance(ac.get("z_points"), dict) else {}
    return [list(p) for p in zp.get("points") or [] if isinstance(p, (list, tuple)) and len(p) == 3]
