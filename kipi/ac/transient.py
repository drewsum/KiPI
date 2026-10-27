"""Estimated transient response at a probe (#166): the rail's voltage while the load steps
up by ``step_a`` and, once the rail has settled, back down, each edge at the probe's slew
rate.

The AC solve gives the open-loop PDN impedance Z(f): the VRM is out of circuit. A step
into that alone never recovers, since nothing recharges the capacitors, so the converter
is put back as a transconductance amplifier driving the rail from its voltage error, with
proportional-integral compensation:

    G(s) = gm (1 + w_z / s) / (1 + s / w_p)^2,    w_z = w_c / 4,  w_p = 8 w_c

where w_c is the VRM's control loop bandwidth and gm puts the loop gain T = Z G at 1
there. The double pole makes the loop give up well above the bandwidth, even into the
rising impedance of an inductive PDN. The rail then sees the closed-loop impedance
Z / (1 + T): the PDN's own above the bandwidth, and nothing at DC (the loop's integrator
holds the rail at its DC value, which the DC solve gives). Only the bandwidth comes from
the setup: the compensation is a typical one, about 60 degrees of phase margin into a
capacitive PDN, not the converter's own.

The response is worked out on a uniform FFT grid: the load current's spectrum times
-Z / (1 + T), back to time. Z is interpolated between sweep points (jw Z, smooth for a
capacitive PDN, on log f) and, past either end of the sweep, extended as the series
resistance and capacitance or inductance of the end point (a causal extension, as holding
a complex value would not be). The time step resolves the load step's edge; one too fast
for the sweep sees that extension above it. The spectrum is tapered to nothing at the top
of the grid (a raised cosine from DC, a Hann window), which keeps the sampled edges from
ringing: tapered over only the top half, a fast edge's ESL spike rang 10 % past its
corners (#200). The load is held long enough for the rail to settle, then the hold shown is cut
to the settling time: the pulse's response is the step's less the step's again, delayed.

**The edge** (#200). While the load current ramps, the PDN's inductance (the capacitors'
ESL, their mounting and the planes' spreading inductance) drops L·di/dt across it: a
plateau as long as the edge, on top of the capacitors' droop. It is short beside the
window, which is sized for the loop's recovery, so it is reported on its own: the
inductance the edge sees, Im Z / ω of the open-loop impedance at the edge's frequency
1 / (π·t_rise), times the slew rate, and the rail's largest dip while the current ramps.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

ZERO_RATIO = 4.0        # the PI zero, this far below the bandwidth
POLE_RATIO = 8.0        # the loop's double roll-off pole, this far above it
HOLD_PER_WC = 60.0      # each edge is followed by this many 1 / w_c before the next
SHOWN_PER_WC = 20.0     # and is shown followed by at least this many: the loop's recovery
MAX_SAMPLES = 1 << 21   # time samples at most; past it the time step grows
EDGE_SAMPLES = 20       # time steps across the load step's edge, at least
SETTLED = 0.02          # the rail has settled once within this share of the peak
SHOWN_SETTLED = 0.01    # the hold shown lasts until within this share, and half again


@dataclass
class Transient:
    t: np.ndarray                 # seconds
    i: np.ndarray                 # the load current step, amps
    v: np.ndarray                 # the rail's deviation from its DC value, volts
    step_a: float
    t_rise_s: float
    t_release_s: float            # when the load steps back down
    undershoot_v: float           # the largest dip below the DC value (positive)
    t_undershoot_s: float
    overshoot_v: float            # the largest rise above it (positive)
    t_overshoot_s: float
    notes: List[str] = field(default_factory=list)
    t_step_s: float = 0.0         # when the load starts to step up
    edge_l_h: Optional[float] = None  # the inductance the edge sees; None: capacitive there
    edge_v: float = 0.0           # the largest dip while the load ramps up (positive)

    @property
    def spike_v(self) -> Optional[float]:
        """L·di/dt over the edge: the ESL spike, apart from the capacitors' droop."""
        if self.edge_l_h is None or self.t_rise_s <= 0:
            return None
        return self.edge_l_h * self.step_a / self.t_rise_s

    def edge_window(self) -> tuple:
        """(start, end), seconds: the step-up edge and a little either side, to zoom to."""
        return self.t_step_s - 2 * self.t_rise_s, self.t_step_s + 8 * self.t_rise_s


def closed_loop(freqs, z, bandwidth_hz: float, f) -> np.ndarray:
    """The closed-loop impedance at ``f`` (Hz, 0 allowed) of the open-loop PDN impedance
    ``z`` sampled at ``freqs``, with the converter's loop crossing over at
    ``bandwidth_hz``."""
    f = np.asarray(f, dtype=float)
    zo = open_loop_at(freqs, z, f)
    wc = 2 * math.pi * bandwidth_hz
    s = 2j * math.pi * f
    with np.errstate(divide="ignore", invalid="ignore"):
        shape = (1 + wc / ZERO_RATIO / s) / (1 + s / (POLE_RATIO * wc)) ** 2
    sc = 1j * wc
    shape_c = (1 + wc / ZERO_RATIO / sc) / (1 + sc / (POLE_RATIO * wc)) ** 2
    gm = 1.0 / abs(open_loop_at(freqs, z, np.array([bandwidth_hz]))[0] * shape_c)
    with np.errstate(divide="ignore", invalid="ignore"):
        zc = zo / (1 + zo * gm * shape)
    zc[f == 0] = 0.0                     # the integrator holds the rail at DC
    return zc


def open_loop_at(freqs, z, f) -> np.ndarray:
    """The open-loop impedance ``z`` (complex, at ``freqs``) at ``f`` > 0: jw Z
    interpolated on log f inside the sweep, and past its ends the series R and C or L of
    the end point (:func:`_extended`)."""
    freqs = np.asarray(freqs, dtype=float)
    z = np.asarray(z, dtype=complex)
    f = np.asarray(f, dtype=float)
    out = np.zeros(f.shape, dtype=complex)
    pos = f > 0
    w = 2 * math.pi * f[pos]
    jwz = 2j * math.pi * freqs * z
    lf = np.log(f[pos])
    lfs = np.log(freqs)
    inside = np.interp(lf, lfs, jwz.real) + 1j * np.interp(lf, lfs, jwz.imag)
    zi = inside / (1j * w)
    below, above = f[pos] < freqs[0], f[pos] > freqs[-1]
    zi[below] = _extended(z[0], 2 * math.pi * freqs[0], w[below])
    zi[above] = _extended(z[-1], 2 * math.pi * freqs[-1], w[above])
    out[pos] = zi
    return out


def _extended(z0: complex, w0: float, w) -> np.ndarray:
    """``z0`` (at ``w0``) taken as a resistance in series with a capacitance or an
    inductance, at ``w``."""
    if z0.imag < 0:
        return z0.real + 1.0 / (1j * w * (-1.0 / (w0 * z0.imag)))
    return z0.real + 1j * w * (z0.imag / w0)


def load_step(t, step_a: float, t_rise: float, t_on: float, t_off: float) -> np.ndarray:
    """A trapezoidal load step: 0 until ``t_on``, up to ``step_a`` in ``t_rise``, back to
    0 from ``t_off``, again in ``t_rise``."""
    t = np.asarray(t, dtype=float)
    up = np.clip((t - t_on) / t_rise, 0.0, 1.0) if t_rise > 0 else (t >= t_on) * 1.0
    down = np.clip((t - t_off) / t_rise, 0.0, 1.0) if t_rise > 0 else (t >= t_off) * 1.0
    return step_a * (up - down)


def transient(freqs, z, bandwidth_hz: Optional[float], step_a: Optional[float],
              slew_a_per_s: Optional[float]) -> Optional[Transient]:
    """The estimated response to a load step of ``step_a`` at ``slew_a_per_s`` at a probe
    whose open-loop impedance is ``z`` at ``freqs``; None without a positive step, slew
    rate and bandwidth."""
    if not (step_a and step_a > 0 and slew_a_per_s and slew_a_per_s > 0
            and bandwidth_hz and bandwidth_hz > 0):
        return None
    freqs = np.asarray(freqs, dtype=float)
    notes = []
    t_rise = step_a / slew_a_per_s
    wc = 2 * math.pi * bandwidth_hz
    hold = max(HOLD_PER_WC / wc, 20 * t_rise)
    lead = 0.05 * hold
    window = lead + 2 * (hold + t_rise)
    dt = min(0.25 / freqs[-1], t_rise / EDGE_SAMPLES)     # Nyquist at twice the sweep
    n = int(math.ceil(window / dt))
    if n > MAX_SAMPLES:
        n = MAX_SAMPLES
        dt = window / n
    if dt > t_rise / 4:
        notes.append(f"The load step rises in {_si(t_rise, 's')}, faster than the "
                     f"{_si(dt, 's')} time step resolves, so its edges are drawn slower "
                     "than they are")
    elif t_rise < 2.0 / freqs[-1]:
        notes.append(f"The load step rises in {_si(t_rise, 's')}, faster than the sweep "
                     f"(to {_si(freqs[-1], 'Hz')}) covers: above it the impedance is "
                     "extended from its last point")
    n += n % 2
    t = np.arange(n) * dt
    t_off = lead + t_rise + hold
    i = load_step(t, step_a, t_rise, lead, t_off)
    f = np.fft.rfftfreq(n, dt)
    zc = closed_loop(freqs, z, bandwidth_hz, f)
    v = np.fft.irfft(-zc * _taper(f) * np.fft.rfft(i), n)
    before = (t > t_off - 0.1 * hold) & (t < t_off - 0.02 * hold)
    # The response to the step up alone, short of the release (the taper smooths each
    # edge a little either side).
    step = v[t < t_off - 0.02 * hold]
    peak = max(float(np.max(np.abs(step))), 1e-30)
    if before.any() and np.max(np.abs(v[before])) > SETTLED * peak:
        notes.append("The rail had not settled before the load stepped back down: the "
                     "loop is slow or poorly damped into this PDN")
    else:
        # Show a hold only as long as the rail takes to settle: the pulse's response is
        # the step's less the step's again, delayed to the release.
        k_peak = int(np.argmax(np.abs(step)))
        out_of = np.flatnonzero(np.abs(step[k_peak:]) > SHOWN_SETTLED * peak)
        settle = t[k_peak + out_of[-1]] - lead if out_of.size else t[k_peak] - lead
        shown = min(max(1.5 * settle, 20 * t_rise, SHOWN_PER_WC / wc), hold)
        k = int(round((t_rise + shown) / dt))
        m = int(math.ceil((lead + 2 * (shown + t_rise)) / dt))
        m += m % 2
        if m < len(step):
            t, t_off = t[:m], lead + k * dt
            v = step[:m] - np.r_[np.zeros(k), step[:m - k]]
            i = load_step(t, step_a, t_rise, lead, t_off)
    f_edge = 1.0 / (math.pi * t_rise)
    x = open_loop_at(freqs, z, np.array([f_edge]))[0].imag
    edge_l = x / (2 * math.pi * f_edge) if x > 0 else None
    return _with_peaks(Transient(t, i, v, step_a, t_rise, t_off, 0.0, 0.0, 0.0, 0.0, notes,
                                 lead, edge_l))


def _taper(f) -> np.ndarray:
    """1 at DC, falling as a raised cosine to 0 at the grid's top frequency ``f[-1]``: the
    edges' content, a tenth of the way up or less, loses a few percent at most."""
    x = np.clip(f / f[-1], 0.0, 1.0)
    return 0.5 * (1.0 + np.cos(np.pi * x))


def _with_peaks(tr: Transient) -> Transient:
    """``tr`` with its undershoot and overshoot (and when they happen) found in ``v``."""
    k_lo, k_hi = int(np.argmin(tr.v)), int(np.argmax(tr.v))
    tr.undershoot_v, tr.t_undershoot_s = float(max(-tr.v[k_lo], 0.0)), float(tr.t[k_lo])
    tr.overshoot_v, tr.t_overshoot_s = float(max(tr.v[k_hi], 0.0)), float(tr.t[k_hi])
    # The edge, and as long again as the spectrum's taper smooths it over.
    dt = tr.t[1] - tr.t[0] if len(tr.t) > 1 else 0.0
    ramp = (tr.t >= tr.t_step_s) & (tr.t <= tr.t_step_s + tr.t_rise_s + max(4 * dt,
                                                                          0.2 * tr.t_rise_s))
    tr.edge_v = float(max(-tr.v[ramp].min(), 0.0)) if ramp.any() else 0.0
    return tr


def bandwidth_limited(tr: Transient, f_bw: float = 20e6) -> Transient:
    """``tr`` as an oscilloscope's bandwidth limit shows it: the rail's deviation through
    a single pole at ``f_bw`` (−3 dB there), as a scope's 20 MHz limit is, with its
    undershoot and overshoot found again."""
    n = len(tr.t)
    f = np.fft.rfftfreq(n, tr.t[1] - tr.t[0])
    v = np.fft.irfft(np.fft.rfft(tr.v) / (1 + 1j * f / f_bw), n)
    return _with_peaks(dataclasses.replace(tr, v=v, notes=list(tr.notes)))


def _si(x: float, unit: str) -> str:
    if x == 0 or not math.isfinite(x):
        return f"{x:g} {unit}"
    e = max(-15, min(9, 3 * math.floor(math.log10(abs(x)) / 3)))
    prefix = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[e]
    return f"{x / 10 ** e:.3g} {prefix}{unit}"


def transients(result) -> dict:
    """Probe name -> its estimated :class:`Transient`, for the probes of an AC result that
    have a load step (and a VRM bandwidth to close the loop with)."""
    bw = getattr(result, "bandwidth_hz", None)
    out = {}
    for c in result.probes:
        tr = c.transient(result.freqs, bw) if hasattr(c, "transient") else None
        if tr is not None:
            out[c.name] = tr
    return out
