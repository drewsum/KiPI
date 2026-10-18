"""Target impedance and frequency from a device's transient spec.

The PDN must hold the rail within its AC tolerance while the load steps by ``i_step``, so

    Z_target = V_ac / I_step

up to the frequency content of the step. A step that rises in ``t_rise = I_step / slew``
has its spectrum's knee at

    f_target = 1 / (pi * t_rise)

A probe can instead have a custom target mask: piecewise linear on the log-log impedance
plot between ``(frequency, impedance)`` corner points.

The AC solve leaves the VRM out of circuit: it computes the *open-loop* PDN impedance.
Below the converter's control loop bandwidth the loop holds the rail, so a target only
applies above the VRM's bandwidth (:func:`judged_from`).

The calculator's AC tolerance and load step can be copied from the DC load on the probe's
pins (:func:`probe_loads`): +/- the tighter side of its tolerance, from the voltage of the
VRM feeding it, and its current. The DC accuracy of the VRM driving the rail can be taken
off the tolerance (:func:`vrm_accuracies`): the larger side of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

import numpy as np

if TYPE_CHECKING:
    from kipi.dc.pi.model import Load, PinRef, PISetup


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
              points: Optional[Sequence[Tuple[float, float]]] = None,
              above_hz: Optional[float] = None) -> Optional[np.ndarray]:
    """The target impedance at each of ``freqs`` (NaN where none applies), or None without
    a target. A custom mask (``points``) takes the place of the flat target, which holds
    ``target_ohm`` up to ``target_hz`` (the whole sweep if none, or if the sweep starts
    above it). With ``above_hz`` (the VRM's control loop bandwidth), either applies only
    at and above that frequency."""
    f = np.asarray(freqs, dtype=float)
    if points:
        t = mask_at(points, f)
    elif target_ohm:
        t = np.full(f.shape, float(target_ohm))
        if target_hz and (f <= target_hz).any():
            t[f > target_hz] = np.nan
    else:
        return None
    if above_hz:
        t[f < above_hz] = np.nan
    return t


def judged_from(bandwidth_hz: Optional[float], freqs, target_ohm: Optional[float] = None,
                target_hz: Optional[float] = None,
                points: Optional[Sequence[Tuple[float, float]]] = None
                ) -> Tuple[Optional[float], Optional[str]]:
    """Where a probe's target starts to apply, given the VRM's control loop bandwidth:
    ``(above_hz, note)`` for :func:`target_at`.

    Normally that is the bandwidth itself. When no frequency of the sweep is left between
    the bandwidth and the end of the target (the bandwidth is at or above the target
    frequency, or above the sweep), the bandwidth is set aside, the target is judged over
    its whole range as before, and ``note`` says why.
    """
    if not bandwidth_hz or bandwidth_hz <= 0:
        return None, None
    t = target_at(freqs, target_ohm, target_hz, points)
    if t is None:
        return bandwidth_hz, None
    f = np.asarray(freqs, dtype=float)
    flat_upper = not points and target_hz and (f <= target_hz).any()
    if flat_upper and bandwidth_hz >= target_hz:
        why = (f"the converter bandwidth ({_hz(bandwidth_hz)}) is at or above the target "
               f"frequency ({_hz(target_hz)})")
    elif (np.isfinite(t) & (f >= bandwidth_hz)).any():
        return bandwidth_hz, None
    else:
        why = f"the converter bandwidth ({_hz(bandwidth_hz)}) is above the sweep"
    return None, (f"{why}, which leaves no frequencies to judge, so the target is judged "
                  "over its whole range instead")


def _hz(f: float) -> str:
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


@dataclass
class LoadTolerance:
    name: str             # the DC load's name
    tolerance: str        # as set: "±5 %", "-3 / +5 %", "3.1 to 3.5 V"
    nominal_v: float      # the set point of the VRM feeding it
    v_ac_v: float         # the tighter side of the tolerance, volts from nominal

    @classmethod
    def of(cls, load: "Load", nominal_v: float) -> Optional["LoadTolerance"]:
        """The AC tolerance a DC load's tolerance allows: +/- its tighter side. None if
        it has none, or nominal lies outside it."""
        lo, hi = load.limits(nominal_v)
        sides = [d for d in (None if lo is None else nominal_v - lo,
                             None if hi is None else hi - nominal_v) if d is not None]
        if not sides or min(sides) <= 0:
            return None
        return cls(load.name, load.tolerance_label(), nominal_v, min(sides))

    def label(self) -> str:
        if self.tolerance.endswith("%"):
            return f"{self.name}: {self.tolerance} of {self.nominal_v:g} V"
        return f"{self.name}: {self.tolerance} (±{self.v_ac_v * 1e3:.4g} mV)"


@dataclass
class VrmAccuracy:
    name: str             # the VRM's name
    accuracy: str         # as set: "±1 %", "1.19 to 1.21 V"
    v: float              # its larger side, volts from the set voltage

    def label(self) -> str:
        return f"{self.name}: {self.accuracy} (±{self.v * 1e3:.4g} mV)"


@dataclass
class ProbeLoad:
    name: str                           # the DC load's name
    tolerance: Optional[LoadTolerance]  # the AC tolerance it allows, if it has one
    current_a: float                    # its total current, 0 for none
    exact: bool = False                 # its pins are the probe's pins

    def label(self) -> str:
        return self.name


def _same_pins(a: Optional["PinRef"], b: Optional["PinRef"]) -> bool:
    return bool(a and b) and (a.ref, a.net, sorted(a.pins)) == (b.ref, b.net, sorted(b.pins))


def probe_loads(setup: "PISetup", rail: Optional["PinRef"],
                ground: Optional["PinRef"] = None) -> List[ProbeLoad]:
    """The DC loads on the probe's part: their supply pins on the footprint of the probe's
    supply pins ``rail``, and their ground pins, where both have them, on the footprint of
    its ``ground``. A load whose supply and ground pins are the probe's own comes first,
    ``exact``; a load without ground pins matches on its supply pins alone."""
    from kipi.dc.pi.model import net_groups
    if rail is None:
        return []
    out = []
    for load in setup.loads:
        if load.supply.ref != rail.ref or (ground and load.ground
                                           and load.ground.ref != ground.ref):
            continue
        tol = None
        if load.has_tolerance():
            group = net_groups(setup, [load.supply.net])[load.supply.net]
            vrm = next((v for v in setup.vrms if v.voltage
                        and any(ph.output.net in group for ph in v.all_phases())), None)
            tol = vrm and LoadTolerance.of(load, vrm.voltage)
        exact = _same_pins(load.supply, rail) and (load.ground is None
                                                   or _same_pins(load.ground, ground))
        out.append(ProbeLoad(load.name, tol or None, abs(load.total_current()), exact))
    out.sort(key=lambda p: not p.exact)
    return out


def vrm_accuracies(setup: "PISetup", rail: Optional["PinRef"] = None) -> List[VrmAccuracy]:
    """LDOs and switchers with a DC accuracy: with the probe's supply pins ``rail``, only
    those driving that rail (its nets joined by series links)."""
    from kipi.dc.pi.model import net_groups
    groups = net_groups(setup, [rail.net] if rail else [])
    out = []
    for v in setup.vrms:
        acc = v.accuracy_v() if v.kind != "source" and v.has_accuracy() else None
        if acc is None or (rail and not any(rail.net in groups[ph.output.net]
                                            for ph in v.all_phases())):
            continue
        out.append(VrmAccuracy(v.name, v.accuracy_label(), acc))
    return out
