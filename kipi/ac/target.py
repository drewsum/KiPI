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
pins (:func:`probe_loads`): +/- the tighter side of its tolerance, from that load's nominal
voltage (or the VRM set point when unset), and its current. Two things that eat into that
tolerance before any load step can be taken off it: the larger side of the VRM's DC
accuracy about its target voltage (:func:`vrm_accuracies`), and how far the load's solved
DC voltage sits from its nominal (:func:`load_drop`), which needs a DC solve. A VRM
targeted above nominal to make up for the copper drop shows up once, in the second (#172).

An LDO's or switcher's input pins load the rail feeding it just as a DC load does
(#117): each phase's input counts as a load whose tolerance is the regulator's input
range, and whose current and drop come from the DC solve (:func:`vrm_inputs`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

import numpy as np

if TYPE_CHECKING:
    from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm


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
        nominal = load.nominal_v if load.nominal_v is not None else nominal_v
        v_ac = _tighter_side(nominal, *load.limits(nominal_v))
        return cls(load.name, load.tolerance_label(), nominal, v_ac) if v_ac else None

    @classmethod
    def of_vrm_input(cls, name: str, vrm: "Vrm", nominal_v: float, lo: Optional[float],
                     hi: Optional[float]) -> Optional["LoadTolerance"]:
        """The AC tolerance a regulator's input range ``lo`` to ``hi`` volts allows on its
        input rail at ``nominal_v``: +/- its tighter side. None as for :meth:`of`."""
        from kipi.dc.pi.model import tolerance_label
        v_ac = _tighter_side(nominal_v, lo, hi)
        label = tolerance_label(vrm.vin_min, vrm.vin_max, vrm.vin_pct, vrm.vin_dv)
        return cls(name, label, nominal_v, v_ac) if v_ac else None

    def label(self) -> str:
        if self.tolerance.endswith("%"):
            return f"{self.name}: {self.tolerance} of {self.nominal_v:g} V"
        return f"{self.name}: {self.tolerance} (±{self.v_ac_v * 1e3:.4g} mV)"


def _tighter_side(nominal: float, lo: Optional[float], hi: Optional[float]
                  ) -> Optional[float]:
    """How far ``nominal`` sits from the nearer end of the range ``lo`` to ``hi`` (either
    None for no limit); None without either end, or with nominal outside the range."""
    sides = [d for d in (None if lo is None else nominal - lo,
                         None if hi is None else hi - nominal) if d is not None]
    return min(sides) if sides and min(sides) > 0 else None


@dataclass
class VrmAccuracy:
    name: str             # the VRM's name
    accuracy: str         # as set: "±1 %", "1.19 to 1.21 V"
    v: float              # its larger side, volts from the target voltage
    nominal_v: Optional[float] = None  # VRM output nominal
    minimum_v: Optional[float] = None  # lowest regulated output in volts
    maximum_v: Optional[float] = None  # highest regulated output in volts

    def label(self) -> str:
        return f"{self.name}: {self.accuracy} (±{self.v * 1e3:.4g} mV)"


@dataclass
class LoadDrop:
    name: str             # the DC load's name
    nominal: float        # the load's nominal, or its VRM's nominal output
    v_effective: float    # its solved effective voltage: supply pins minus ground pins
    v: float              # how far that sits from nominal, volts (never negative)
    kind: str             # "drop" (below nominal) or "boost" (above it)

    def label(self) -> str:
        return (f"{self.name}: {self.v_effective:.4g} V of {self.nominal:g} V "
                f"({self.v * 1e3:.4g} mV {self.kind})")


@dataclass
class ProbeLoad:
    name: str                           # the DC load's name
    tolerance: Optional[LoadTolerance]  # the AC tolerance it allows, if it has one
    current_a: float                    # its total current, 0 for none
    exact: bool = False                 # its pins are the probe's pins
    drop: Optional[LoadDrop] = None     # its DC drop, once the power tree is solved
    vrm_input: bool = False             # an LDO's or switcher's input, not a DC load:
                                        # its current is known only from a DC solve

    def label(self) -> str:
        return self.name


def _same_pins(a: Optional["PinRef"], b: Optional["PinRef"]) -> bool:
    return bool(a and b) and (a.ref, a.net, sorted(a.pins)) == (b.ref, b.net, sorted(b.pins))


def load_drop(dc_result, name: str) -> Optional[LoadDrop]:
    """How far a DC load's solved effective voltage sits from its nominal (its own, or its
    VRM's nominal output; not the VRM's target voltage, so a VRM targeted high to make up
    for the copper drop leaves less of one, #172), out of the power-tree result
    ``dc_result``. None without a solve, or when
    that solve has no voltage for the load: it was not in the setup then, it has no copper
    path to its VRM (#88), or no VRM drives it."""
    from kipi.dc.pi.results import drop_or_boost
    for r in getattr(dc_result, "loads", None) or []:
        if r.name != name or not r.nominal or not math.isfinite(r.v_effective):
            continue
        nominal = r.nominal if r.load_nominal is None else r.load_nominal
        v, kind = drop_or_boost(nominal - r.v_effective)
        return LoadDrop(name, nominal, r.v_effective, v, kind) if kind else None
    return None


def vrm_input_name(vrm: str, phase: int, phases: int) -> str:
    """The name of a regulator's input as a load: "VR1 input", or with several phases,
    "VR1 phase 2 input" (``phase`` counts from 0)."""
    return f"{vrm} input" if phases == 1 else f"{vrm} phase {phase + 1} input"


def vrm_inputs(setup: "PISetup") -> List[Tuple[str, "Vrm", int]]:
    """The input pins of every LDO and switcher, one entry per phase that has them:
    ``(name, vrm, phase)``, the phase counting from 0 (see :meth:`Vrm.all_phases`)."""
    out = []
    for v in setup.vrms:
        if v.kind not in ("ldo", "smps"):
            continue
        phases = v.all_phases()
        out += [(vrm_input_name(v.name, k, len(phases)), v, k)
                for k, ph in enumerate(phases) if ph.input]
    return out


def _vrm_input_solved(dc_result, vrm: str, phase: int
                      ) -> Tuple[Optional[float], Optional[float]]:
    """A regulator phase's (input volts, input amps) in the power-tree result
    ``dc_result``; (None, None) without a solve, or with no input solved for it."""
    for r in getattr(dc_result, "vrms", None) or []:
        if r.name != vrm:
            continue
        if r.phases:
            return ((r.phases[phase].v_in, r.phases[phase].i_in)
                    if phase < len(r.phases) else (None, None))
        return (r.v_in, r.i_in) if phase == 0 else (None, None)
    return None, None


def vrm_input_drop(dc_result, name: str, vrm: str, phase: int,
                   nominal: Optional[float]) -> Optional[LoadDrop]:
    """How far a regulator phase's solved input voltage sits from its input rail's
    ``nominal`` voltage, as a :class:`LoadDrop` named ``name``; None as for
    :func:`load_drop`."""
    from kipi.dc.pi.results import drop_or_boost
    v_in = _vrm_input_solved(dc_result, vrm, phase)[0]
    if v_in is None or not nominal or not math.isfinite(v_in):
        return None
    v, kind = drop_or_boost(nominal - v_in)
    return LoadDrop(name, nominal, v_in, v, kind) if kind else None


def probe_loads(setup: "PISetup", rail: Optional["PinRef"],
                ground: Optional["PinRef"] = None, dc_result=None) -> List[ProbeLoad]:
    """The DC loads on the probe's part: their supply pins on the footprint of the probe's
    supply pins ``rail``, and their ground pins, where both have them, on the footprint of
    its ``ground``. A load whose supply and ground pins are the probe's own comes first,
    ``exact``; a load without ground pins matches on its supply pins alone. With the result
    of a power-tree solve, each load also carries its DC drop (:func:`load_drop`).

    The input of each LDO and switcher phase (:func:`vrm_inputs`) counts as a load too,
    with the regulator's input range as its tolerance; its current and drop come from the
    solve."""
    from kipi.dc.pi.model import net_groups, rail_nominal, vin_range
    if rail is None:
        return []
    out = []
    for load in setup.loads:
        if load.supply.ref != rail.ref or (ground and load.ground
                                           and load.ground.ref != ground.ref):
            continue
        group = net_groups(setup, [load.supply.net])[load.supply.net]
        vrm = next((v for v in setup.vrms if v.voltage
                    and any(ph.output.net in group for ph in v.all_phases())), None)
        tol = (LoadTolerance.of(load, vrm.output_nominal_v())
               if load.has_tolerance() and vrm else None)
        exact = _same_pins(load.supply, rail) and (load.ground is None
                                                   or _same_pins(load.ground, ground))
        out.append(ProbeLoad(load.name, tol or None, abs(load.total_current()), exact,
                             load_drop(dc_result, load.name)))
    for name, v, k in vrm_inputs(setup):
        ph = v.all_phases()[k]
        if ph.input.ref != rail.ref or (ground and ph.ground and ph.ground.ref != ground.ref):
            continue
        nominal = rail_nominal(setup, ph.input.net, v.name)
        tol = nominal and LoadTolerance.of_vrm_input(name, v, nominal, *vin_range(setup, v))
        i_in = _vrm_input_solved(dc_result, v.name, k)[1]
        exact = _same_pins(ph.input, rail) and (ph.ground is None
                                                or _same_pins(ph.ground, ground))
        out.append(ProbeLoad(name, tol or None, abs(i_in or 0.0), exact,
                             vrm_input_drop(dc_result, name, v.name, k, nominal), True))
    out.sort(key=lambda p: not p.exact)
    return out


def vrm_accuracies(setup: "PISetup", rail: Optional["PinRef"] = None) -> List[VrmAccuracy]:
    """LDOs and switchers with a DC accuracy: with the probe's supply pins ``rail``, only
    those driving that rail (its nets joined by series links)."""
    from kipi.dc.pi.model import net_groups, range_unit, range_volts
    groups = net_groups(setup, [rail.net] if rail else [])
    out = []
    for v in setup.vrms:
        acc = v.accuracy_v() if v.kind != "source" and v.has_accuracy() else None
        if acc is None or (rail and not any(rail.net in groups[ph.output.net]
                                            for ph in v.all_phases())):
            continue
        low, high = range_volts(v.voltage, v.acc_min, v.acc_max,
                                range_unit(v.acc_pct, v.acc_dv))
        out.append(VrmAccuracy(v.name, v.accuracy_label(), acc, v.output_nominal_v(), low, high))
    return out
