"""A buck or boost converter's waveforms, built as harmonic spectra (#69).

Every waveform is periodic and piecewise linear over one switching period, so its Fourier
series is exact in closed form (:func:`pwl_spectrum`); nothing is time-stepped. A phase's
waveforms are shifted by n·T/N to interleave N phases, and the switch node's edges are
passed through the hot loop's ringing (a second-order response at f_ring with damping ζ).
The ringing is the switch node's capacitance trading charge with the hot loop's
inductance, so its current, C_sw·d(v_sw − v_ideal)/dt, flows around the hot loop and is
part of the pulsed current the hot-loop capacitors carry (#151).

The two edges do not ring alike. When the control switch turns on (a buck's high side, a
boost's low side) it takes the inductor current from the other switch's conducting body
diode: a **hard** edge, which must also sweep out the diode's reverse-recovery charge
Qrr. The diode snaps off with that current in the loop inductance, and the node rings.
When the control switch turns off, the inductor current itself carries the node across
(no faster than C_sw·V/I) and the other switch's body diode, turning on, clamps it: a
**soft** edge, damped by the clamp (at least ``soft_zeta``). Which edge is which follows
the inductor current: a valley current below zero makes the turn-on soft too (zero-voltage
switching).

Spectra are two-sided Fourier coefficients c_k for k = 1..K (x(t) = dc + Σ 2·Re(c_k·e^{jω_k t})),
so the amplitude of harmonic k is 2|c_k|. Capacitor currents carry no DC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

F_MAX_CAP_HZ = 3e9


def pwl_spectrum(t: Sequence[float], y: Sequence[float], period: float, k: np.ndarray
                 ) -> Tuple[float, np.ndarray]:
    """The mean and Fourier coefficients c_k of a periodic, continuous, piecewise-linear
    waveform through the points (t, y), t increasing and within one ``period`` (the last
    point joins the first one period later).

    Its second derivative is a train of impulses, the change of slope at each corner, so
    c_k = -1/(T·ω_k²) · Σ Δs_i·e^{-jω_k t_i}.
    """
    mean, terms = pwl_corners(t, y, period, k)
    return mean, terms.sum(axis=1)


def pwl_corners(t: Sequence[float], y: Sequence[float], period: float, k: np.ndarray
                ) -> Tuple[float, np.ndarray]:
    """:func:`pwl_spectrum`'s mean and each corner's term of c_k, (K, corners) in the
    order of ``t``: a group of corners (one edge of a trapezoid) is their sum."""
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    t_next = np.append(t[1:], t[0] + period)
    y_next = np.append(y[1:], y[0])
    dt = t_next - t
    if np.any(dt <= 0):
        raise ValueError("pwl_spectrum: times must increase within one period")
    slope = (y_next - y) / dt
    ds = slope - np.roll(slope, 1)
    mean = float(np.sum(0.5 * (y + y_next) * dt) / period)
    w = 2 * np.pi * np.asarray(k, float) / period
    terms = -np.exp(-1j * np.outer(w, t)) * ds[None, :] / (period * w ** 2)[:, None]
    return mean, terms


def ifft_periodic(mean: float, c: np.ndarray, n: int, periods: int = 1) -> np.ndarray:
    """``n`` samples per period of the waveform with ``mean`` and coefficients ``c`` (for
    k = 1..K, K < n/2), repeated ``periods`` times."""
    K = len(c)
    if 2 * K >= n:
        raise ValueError("ifft_periodic: too few samples for the harmonics")
    X = np.zeros(n, complex)
    X[1:K + 1] = c * n
    X[n - K:] = np.conj(c[::-1]) * n
    x = np.fft.ifft(X).real + mean
    return np.tile(x, periods)


def ringing_response(f: np.ndarray, f_ring: Optional[float], zeta: float) -> np.ndarray:
    """H(jω) = ω0² / (s² + 2ζω0·s + ω0²): an edge through the hot loop's L-C tank."""
    if not f_ring:
        return np.ones_like(f, dtype=complex)
    w0 = 2 * np.pi * f_ring
    s = 2j * np.pi * np.asarray(f)
    return w0 ** 2 / (s ** 2 + 2 * zeta * w0 * s + w0 ** 2)


def harmonic_count(fsw: float, rise: float, fall: float, f_ring: Optional[float],
                   f_max: Optional[float], max_harmonics: int) -> Tuple[int, float, bool]:
    """(K, f_max, capped): harmonics up to f_max, by default ten times the faster edge's
    corner (1/(π·t)) or three times the ringing, whichever is higher, at most 3 GHz.
    ``capped`` is True when ``max_harmonics`` cut it short."""
    if not f_max:
        f_max = max(10.0 / (math.pi * min(rise, fall)), 3.0 * (f_ring or 0.0))
        f_max = min(f_max, F_MAX_CAP_HZ)
    k = max(1, int(math.ceil(f_max / fsw)))
    capped = k > max_harmonics
    k = min(k, max(1, max_harmonics))
    return k, k * fsw, capped


@dataclass
class Spectrum:
    """One waveform: its mean and Fourier coefficients at k·fsw, k = 1..K."""
    name: str
    unit: str                       # "V" or "A"
    mean: float
    c: np.ndarray

    def rms(self) -> float:
        return math.sqrt(self.mean ** 2 + 2.0 * float(np.sum(np.abs(self.c) ** 2)))

    def ac_rms(self) -> float:
        return math.sqrt(2.0 * float(np.sum(np.abs(self.c) ** 2)))

    def amplitude(self) -> np.ndarray:
        """Single-sided amplitude of each harmonic (peak)."""
        return 2.0 * np.abs(self.c)

    def samples(self, n: int, periods: int = 1) -> np.ndarray:
        return ifft_periodic(self.mean, self.c, n, periods)


@dataclass
class PhaseWaveforms:
    switch_node: Spectrum           # with ringing
    inductor: Spectrum
    pulsed: Spectrum                # the hot loop's pulsed current (HS switch, buck; rectifier, boost)
    edges: str = ""                 # how each edge switches, for the notes


@dataclass
class SmpsWaveforms:
    topology: str
    fsw_hz: float
    duty: float
    vin: float
    vout: float
    iout: float
    ripple_a: List[float]           # peak-to-peak inductor ripple, per phase
    f_max_hz: float
    phases: List[PhaseWaveforms]
    input_caps: Dict[str, Spectrum] = field(default_factory=dict)    # per reference
    output_caps: Dict[str, Spectrum] = field(default_factory=dict)
    input_ripple: Optional[Spectrum] = None      # rail voltage ripple, AC only
    output_ripple: Optional[Spectrum] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def harmonics(self) -> int:
        return len(self.phases[0].switch_node.c) if self.phases else 0

    def freqs(self) -> np.ndarray:
        return self.fsw_hz * np.arange(1, self.harmonics + 1)

    def samples_per_period(self) -> int:
        """A power of two of at least 4K samples per period."""
        return 1 << max(8, int(math.ceil(math.log2(4 * max(self.harmonics, 1)))))

    def time(self, periods: int = 2) -> np.ndarray:
        n = self.samples_per_period()
        return np.arange(n * periods) / (n * self.fsw_hz)


@dataclass
class CapBranch:
    """A capacitor as the converter sees it: its impedance plus its mounting path."""
    ref: str
    capacitance_f: float
    esr_ohm: float
    esl_h: float
    path_l_h: float = 0.0
    path_r_ohm: float = 0.0

    def impedance(self, omega: np.ndarray) -> np.ndarray:
        return (self.esr_ohm + self.path_r_ohm + 1j * omega * (self.esl_h + self.path_l_h)
                + 1.0 / (1j * omega * self.capacitance_f))


def split_current(total: np.ndarray, omega: np.ndarray, caps: List[CapBranch]
                  ) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """Each capacitor's share of ``total`` (flowing into the rail's capacitors), divided
    by their admittances, and the rail's ripple voltage (total × the bank's impedance)."""
    if not caps:
        return {}, np.zeros_like(total)
    y = np.array([1.0 / c.impedance(omega) for c in caps])
    ysum = y.sum(axis=0)
    return {c.ref: total * y[i] / ysum for i, c in enumerate(caps)}, total / ysum


@dataclass
class PhaseParts:
    """What one phase's waveforms need besides the operating point."""
    inductance_h: float
    f_ring: Optional[float] = None
    zeta: float = 1.0
    c_sw_f: float = 0.0             # the switch node's capacitance, which carries the ringing
    soft_zeta: float = 0.5          # the least damping of a soft (diode-clamped) edge
    qrr_c: float = 0.0              # the reverse-recovery charge a hard edge sweeps out


@dataclass
class EdgeShape:
    """One edge of a phase's switch node: how long it takes and how it rings."""
    name: str                       # "rising" or "falling"
    time_s: float
    hard: bool
    zeta: float
    irr_a: float = 0.0              # reverse-recovery peak current (hard edges)
    i_a: float = 0.0                # the inductor current at the edge


def edge_shapes(topology: str, lo: float, hi: float, rise: float, fall: float,
                swing: float, ph: PhaseParts) -> Tuple[EdgeShape, EdgeShape]:
    """The control switch's turn-on edge (at t = 0) and turn-off edge (at D·T).

    Turn-on is hard while the valley current ``lo`` flows out of the switch node (into
    the inductor): the other switch's body diode is conducting it. The diode keeps
    conducting, in reverse, until Qrr is swept out at the edge's di/dt = lo/t, then snaps
    off over the edge: Qrr = ½·Irr·(Irr/(di/dt) + t). Turn-off is soft while the peak
    current ``hi`` does: it carries the node across, no faster than C_sw·V/I, into the
    other body diode's clamp. A soft edge is damped at least ``soft_zeta``.
    """
    buck = topology == "buck"
    names = ("rising", "falling") if buck else ("falling", "rising")
    times = (rise, fall) if buck else (fall, rise)
    out = []
    for name, t, i, turn_on in zip(names, times, (lo, hi), (True, False)):
        hard = (i > 0) if turn_on else not (i > 0)
        if not hard and ph.c_sw_f > 0 and i:
            t = max(t, ph.c_sw_f * swing / abs(i))
        zeta = ph.zeta if hard else max(ph.zeta, ph.soft_zeta)
        irr = 0.0
        if hard and turn_on and ph.qrr_c > 0 and i > 0:
            didt = i / t
            irr = 0.5 * didt * (math.sqrt(t * t + 8 * ph.qrr_c / didt) - t)
        out.append(EdgeShape(name, t, hard, zeta, irr, i))
    return out[0], out[1]


def describe_edges(edges: Sequence[EdgeShape], qrr_c: float) -> str:
    """The edges for the notes: hard or soft, and why."""
    parts = []
    for e in edges:
        if e.hard and e.irr_a:
            parts.append(f"{e.name} edge hard (reverse recovery {qrr_c * 1e9:.3g} nC, "
                         f"{e.irr_a:.3g} A peak), ζ = {e.zeta:.3g}")
        elif e.hard:
            parts.append(f"{e.name} edge hard, ζ = {e.zeta:.3g}")
        else:
            parts.append(f"{e.name} edge soft (the inductor's {abs(e.i_a):.3g} A carries "
                         f"it, {e.time_s * 1e9:.3g} ns, into a body diode's clamp), "
                         f"ζ = {e.zeta:.3g}")
    return "; ".join(parts)


def synthesize(topology: str, vin: float, vout: float, iout: float, iin: float, duty: float,
               fsw: float, rise: float, fall: float, phases: List[PhaseParts],
               k_count: int, input_caps: List[CapBranch], output_caps: List[CapBranch],
               f_max: float) -> SmpsWaveforms:
    """Every waveform of an N-phase buck or boost.

    Each phase's switch node is a trapezoid (edges centred on t = 0 and D·T for a buck;
    a boost's switch node is low during D·T), its inductor current a triangle around the
    phase's share of the current, and its pulsed current the inductor current while the
    hot-loop switch conducts (buck: the high side during D·T; boost: the rectifier for the
    rest of the period), with the switch node's edge times, plus the ringing's current.
    By KCL at the switch node, with the inductor current steady over an edge, a buck's
    high side carries C_sw·dv/dt more and a boost's rectifier C_sw·dv/dt less.

    Each edge rings by its own damping (:func:`edge_shapes`). A hard edge's recovery
    current, a triangle of charge Qrr ending as the edge ends, flows through the hot loop
    (a buck's high side carries it, a boost's rectifier carries it backwards) and out of
    the switch node (into it, for a boost), which answers through the tank's impedance
    Z = jωL·H: the overshoot of about Irr·Z0 the snap-off leaves.
    """
    T = 1.0 / fsw
    n = len(phases)
    k = np.arange(1, k_count + 1)
    f = k * fsw
    omega = 2 * np.pi * f
    DT = duty * T
    out = []
    ripples = []
    sum_l = np.zeros(k_count, complex)
    sum_p = np.zeros(k_count, complex)
    mean_p = 0.0
    for i, ph in enumerate(phases):
        shift = np.exp(-1j * omega * i * T / n)
        buck = topology == "buck"
        sigma = 1.0 if buck else -1.0
        if buck:
            iavg = iout / n
            dI = (vin - vout) * duty / (ph.inductance_h * fsw)
        else:
            iavg = iin / n
            dI = vin * duty / (ph.inductance_h * fsw)
        ripples.append(dI)
        lo, hi = iavg - dI / 2, iavg + dI / 2
        il_t, il_y = [0.0, DT], [lo, hi]
        on, off = edge_shapes(topology, lo, hi, rise, fall, vin if buck else vout, ph)
        t_on, t_off = on.time_s, off.time_s
        vt = [-t_on / 2, t_on / 2, DT - t_off / 2, DT + t_off / 2]
        vy = [0.0, vin, vin, 0.0] if buck else [vout, 0.0, 0.0, vout]

        def i_l(t, lo=lo, hi=hi):
            t = t % T
            return lo + (hi - lo) * t / DT if t <= DT else hi - (hi - lo) * (t - DT) / (T - DT)

        if buck:                     # high side conducts during D·T
            py = [0.0, i_l(t_on / 2), i_l(DT - t_off / 2), 0.0]
        else:                        # rectifier conducts from D·T to T
            py = [i_l(-t_on / 2), 0.0, 0.0, i_l(DT + t_off / 2)]
        v_mean, v_terms = pwl_corners(_wrap(vt, T), _order(vt, vy, T), T, k)
        on_corner = np.array([i < 2 for _, i in sorted((x % T, i) for i, x in enumerate(vt))])
        v_on, v_off = v_terms[:, on_corner].sum(axis=1), v_terms[:, ~on_corner].sum(axis=1)
        l_mean, l_c = pwl_spectrum(il_t, il_y, T, k)
        p_mean, p_c = pwl_spectrum(_wrap(vt, T), _order(vt, py, T), T, k)
        h_on = ringing_response(f, ph.f_ring, on.zeta)
        ring = v_on * (h_on - 1.0) + v_off * (ringing_response(f, ph.f_ring, off.zeta) - 1.0)
        if on.irr_a and ph.f_ring and ph.c_sw_f > 0:
            # The recovery current: up at di/dt until the edge starts, snapping off over it.
            # The hot loop carries all of it; the node, held by the conducting diode until
            # the snap, answers the snap alone: a drop of Irr over the edge (its slow
            # return over the period is the bookkeeping that keeps it periodic).
            t_a = on.irr_a * t_on / on.i_a
            rt = [-t_on / 2 - t_a, -t_on / 2, t_on / 2]
            rr_mean, rr_c = pwl_spectrum(_wrap(rt, T), _order(rt, [0.0, on.irr_a, 0.0], T),
                                         T, k)
            st = [-t_on / 2, t_on / 2]
            _, snap_c = pwl_spectrum(_wrap(st, T), _order(st, [on.irr_a, 0.0], T), T, k)
            w0 = 2 * np.pi * ph.f_ring
            l_loop = 1.0 / (w0 * w0 * ph.c_sw_f)
            ring = ring - sigma * 1j * omega * l_loop * h_on * snap_c
            p_c = p_c + sigma * rr_c
            p_mean += sigma * rr_mean
        v_c = v_on + v_off + ring
        p_c = p_c + sigma * 1j * omega * ph.c_sw_f * ring
        sw = Spectrum(f"SW{i + 1}", "V", v_mean, v_c * shift)
        ind = Spectrum(f"I(L{i + 1})", "A", l_mean, l_c * shift)
        pul = Spectrum(f"Pulsed {i + 1}", "A", p_mean, p_c * shift)
        out.append(PhaseWaveforms(sw, ind, pul, describe_edges((on, off), ph.qrr_c)
                                  if ph.f_ring else ""))
        sum_l += ind.c
        sum_p += pul.c
        mean_p += p_mean

    wf = SmpsWaveforms(topology, fsw, duty, vin, vout, iout, ripples, f_max, out)
    # Current into each rail's capacitors (AC): the bank takes what the source or load
    # does not. Buck: the high side pulls the pulsed current from the input, the
    # inductors push their current into the output. Boost: the inductors pull from the
    # input, the rectifiers push the pulsed current into the output.
    if topology == "buck":
        into_in, into_out = -sum_p, sum_l
    else:
        into_in, into_out = -sum_l, sum_p
    for caps, total, store, attr in ((input_caps, into_in, wf.input_caps, "input_ripple"),
                                     (output_caps, into_out, wf.output_caps, "output_ripple")):
        shares, v = split_current(total, omega, caps)
        for ref, c in shares.items():
            store[ref] = Spectrum(f"I({ref})", "A", 0.0, c)
        if caps:
            name = "Vin ripple" if attr == "input_ripple" else "Vout ripple"
            setattr(wf, attr, Spectrum(name, "V", 0.0, v))
    return wf


def _wrap(t: Sequence[float], period: float) -> List[float]:
    """Corner times moved into [0, period), sorted."""
    return sorted(x % period for x in t)


def _order(t: Sequence[float], y: Sequence[float], period: float) -> List[float]:
    """The values of :func:`_wrap`'s corners, in its order."""
    pairs = sorted(((x % period), v) for x, v in zip(t, y))
    return [v for _, v in pairs]
