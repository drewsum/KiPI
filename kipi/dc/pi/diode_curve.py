# Copyright (c) 2026 Tomachie LLC.
"""A series diode's model from its datasheet: its forward-voltage curve, or its forward
drop.

KiPI solves a diode from its SPICE model (#191). A power-tree file states a diode the way
its datasheet does: a forward-voltage curve, [[amps, volts], ...], and a forward drop
(typical and maximum). :func:`diode_ref` gives the link a ``.model D`` for them: fitted to
the curve, V = N x VT x ln(I / IS) + I x RS by least squares in volts, or, with only a
drop, KiPI's own constant-drop model. A Schottky's curve is far from a straight line: a
line fitted from 2 A to 20 A reads about 0.16 V high at 0.5 A; the junction's log term
follows it.

The DC solve takes the curve itself (:func:`curve_current`), exactly as printed; the
fitted model gives AC and Coupled Noise a diode's small-signal values at its current.
The worst case (:mod:`kipi.dc.pi.corner`) lifts the curve by the maximum drop less the
typical, as much at every current.
"""

from __future__ import annotations

import math
import re
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

from kipi.core.model.spice import VT, Library, constant_drop_ref, link_ref

# The series resistance a diode with no stated slope resistance gets: small against any
# rail's copper, enough to keep the solve well conditioned.
DIODE_MIN_OHMS = 1e-3
# What a model built here says it was made from, so a solve knows to rebuild it at the
# corner's numbers and never touches a model the user loaded from a file.
CURVE_SOURCE = "the datasheet's forward-voltage curve"
DROP_SOURCE = "the datasheet's forward drop"


def curve_volts(points: Sequence[Sequence[float]], amps: float) -> float:
    """The curve's forward voltage at ``amps``: straight lines in log current between
    its points (a diode's voltage grows with the log of its current); beyond its ends,
    the line through its last two points the same way."""
    pts = sorted((float(i), float(v)) for i, v in points if float(i) > 0)
    if len(pts) == 1:
        return pts[0][1]
    x = math.log(max(amps, 1e-12))
    k = next((n for n in range(1, len(pts)) if pts[n][0] >= amps), len(pts) - 1)
    (i1, v1), (i2, v2) = pts[k - 1], pts[k]
    x1, x2 = math.log(i1), math.log(i2)
    return max(v1 + (v2 - v1) * (x - x1) / (x2 - x1), 0.0)


def fit_curve(points: Sequence[Sequence[float]]) -> Optional[tuple]:
    """(IS, N, RS) of the junction and series resistance through the curve's points, or
    None when they do not make a forward curve (fewer than two currents, or a voltage
    that does not rise with the current)."""
    pts = sorted((float(i), float(v)) for i, v in points if float(i) > 0)
    if len({i for i, _ in pts}) < 2:
        return None
    i = np.array([p[0] for p in pts])
    v = np.array([p[1] for p in pts])
    cols = [np.log(i), np.ones_like(i)] + ([i] if len(pts) >= 3 else [])
    a, b, *r = np.linalg.lstsq(np.column_stack(cols), v, rcond=None)[0]
    rs = float(r[0]) if r else 0.0
    if rs < 0:                          # a curve bending the other way: no series term
        a, b = np.linalg.lstsq(np.column_stack(cols[:2]), v, rcond=None)[0]
        rs = 0.0
    if not a > 0:
        return None
    return float(math.exp(-b / a)), float(a / VT), max(rs, DIODE_MIN_OHMS)


def curve_ref(points: Sequence[Sequence[float]]):
    """A diode model fitted to the forward curve ``points``; None when they make none."""
    fit = fit_curve(points)
    if fit is None:
        return None
    i_s, n, rs = fit
    lib = Library(f".model VFCURVE D(IS={i_s:.6g} N={n:.6g} RS={rs:.6g})\n")
    ref, _v = link_ref(lib, "VFCURVE")
    ref.file = CURVE_SOURCE
    return ref


def built_here(link) -> bool:
    """Whether ``link``'s model was made from its datasheet values (here, or by an older
    setup's constant drop), not loaded from a SPICE file."""
    src = getattr(link.spice, "file", "") or ""
    return link.spice is None or src in (CURVE_SOURCE, DROP_SOURCE) or src.startswith(
        "the older setup's constant")


def diode_ref(link) -> None:
    """Give ``link`` the diode model of its datasheet values: fitted to its curve, else
    its constant forward drop. A link with neither is left as it is."""
    ref = None
    if link.vf_curve:
        ref = curve_ref(link.vf_curve)
    if ref is None and link.diode_vf > 0:
        ref = constant_drop_ref(link.diode_vf, _stated_rs(link))
        ref.file = DROP_SOURCE
    if ref is not None:
        link.spice = ref
        link.resistance = ref.values["resistance"]


def _stated_rs(link) -> float:
    """The series resistance a drop model is built with: the one a model built here
    already has (so building it again changes nothing), else the link's own."""
    if link.spice is not None and built_here(link):
        m = re.search(r"rs=([-+0-9.eE]+)", link.spice.netlist or "", re.I)
        if m:
            return float(m.group(1))
    return max(link.resistance, DIODE_MIN_OHMS)


def curve_current(points: Sequence[Sequence[float]]) -> Callable[[float], Tuple[float, float]]:
    """The DC solve's diode from its datasheet curve itself, not a model fitted to it:
    the current at ``v`` volts from anode to cathode and its slope dI/dV, straight lines
    in log current between the points and beyond its ends along the end segments."""
    pts = sorted((float(v), math.log(float(i))) for i, v in points if float(i) > 0)
    vs = np.array([p[0] for p in pts])
    ln_i = np.array([p[1] for p in pts])
    slopes = np.diff(ln_i) / np.maximum(np.diff(vs), 1e-9)

    def iv(v: float) -> Tuple[float, float]:
        k = int(np.clip(np.searchsorted(vs, v) - 1, 0, len(slopes) - 1))
        s = max(float(slopes[k]), 1e-3)
        amps = math.exp(min(ln_i[k] + s * (v - vs[k]), 50.0))
        return amps, amps * s

    return iv


def stated_diodes(links: List) -> List:
    """The links whose diode model comes from their datasheet values."""
    return [k for k in links if (k.diode_vf > 0 or k.vf_curve) and built_here(k)]
