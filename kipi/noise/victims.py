"""The noise an aggressor couples onto a victim net (#69).

The victim's traces are extracted into segments (:mod:`kipi.noise.extract.capacitive`),
joined into a network of lines (:mod:`kipi.noise.network`) terminated by its driver and
receiver, and driven by each phase's switch node through the segments' mutual
capacitance. Each mechanism is solved on its own, then added up as phasors.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.model import PISetup
from kipi.noise.checks import guess_ends, pin_position
from kipi.noise.extract.capacitive import extract
from kipi.noise.extract.xsection import Cache
from kipi.noise.model import Aggressor, Victim
from kipi.noise.network import Termination, VictimNetwork
from kipi.noise.extract.inductive import loop_sources, victim_emf
from kipi.noise.result import MECHANISMS, TOTAL, StateResult, VictimResult
from kipi.noise.waveforms import SmpsWaveforms, Spectrum


def ends(snap: BoardSnapshot, victim: Victim):
    """The victim's driver and receiver pins (guessed where not set), and a note when
    they were guessed."""
    driver, receiver = victim.driver, victim.receiver
    if driver is not None and receiver is not None:
        return driver, receiver, ""
    g_drv, g_rcv = guess_ends(snap, victim.net)
    if driver is None and receiver is None:
        driver, receiver = g_drv, g_rcv
    else:
        known = driver or receiver
        other = g_rcv if g_drv == known else g_drv
        driver, receiver = (driver or other), (receiver or other)
    if driver is None or receiver is None:
        return driver, receiver, ""
    return driver, receiver, (f"Driver and receiver not both set: taken as "
                              f"{driver.label()} and {receiver.label()}")


def couple(snap: BoardSnapshot, setup: PISetup, victim: Victim, agg: Aggressor,
           wf: SmpsWaveforms, cache: Optional[Cache] = None,
           check_cancel=None, layouts: Sequence = (),
           ground_diff: Optional[np.ndarray] = None) -> VictimResult:
    """The victim's noise from each mechanism: capacitive from the switch nodes,
    inductive from the capacitor loops in ``layouts`` (each phase's
    :class:`~kipi.noise.parasitics.PhaseLayout`), and common-impedance from
    ``ground_diff``, its driver's ground against its receiver's at each harmonic (from the
    board model; None without one)."""
    s = setup.noise
    warnings: List[str] = []
    driver, receiver, guessed = ends(snap, victim)
    if guessed:
        warnings.append(guessed)
    res = VictimResult(victim.name, victim.net, driver.label() if driver else "",
                       receiver.label() if receiver else "", victim.margin())
    switch_nets = [ph.switch_net for ph in agg.phases]
    if not any(switch_nets):
        warnings.append("No phase has a switch node: no capacitive coupling")
    ext = extract(snap, victim.net, switch_nets, s.segment_mm, s.window_mm,
                  setup.ac.default_er, cache, check_cancel)
    segs = ext.segments
    res.solves = ext.solves
    if not segs:
        res.warnings = warnings + [f"{victim.net} has no tracks to couple onto"]
        return res
    # Splits in the reference plane (#146): each crossing's detour inductance goes in
    # series with the segment after it.
    from kipi.noise.splits import plane_splits
    res.splits = plane_splits(snap, segs, [victim.net, *switch_nets], setup)
    for sp in res.splits:
        text = sp.describe()
        warnings.append(text[0].upper() + text[1:])
    res.length_mm = float(sum(x.length_mm for x in segs))
    res.coupled_mm = float(sum(x.length_mm for x in segs if x.cm))
    res.z0_ohm = float(sum(x.z0_ohm * x.length_mm for x in segs) / res.length_mm)
    for n in dict.fromkeys(switch_nets):
        if n:
            res.cm_f[n] = float(sum(x.cm.get(n, 0.0) * x.length_mm for x in segs))
    thick = {c.id: c.thickness_mm for c in snap.stackup.copper}
    net = VictimNetwork(segs, thick, setup.ac.loss_tangent)
    d_xy, r_xy = pin_position(snap, driver), pin_position(snap, receiver)
    if d_xy is None or r_xy is None:
        res.warnings = warnings + ["Its driver or receiver pins are not on the board"]
        return res
    obs = [net.nearest(r_xy), net.nearest(d_xy)]
    for xy, label in ((d_xy, res.driver), (r_xy, res.receiver)):
        if math.dist(xy, net.xy[net.nearest(xy)]) > 1.0:
            warnings.append(f"{label} is over 1 mm from the net's nearest track end")
    freqs = wf.freqs()
    omega = 2 * np.pi * freqs
    src = np.zeros((len(freqs), net.n), complex)
    for ph, phase_wf in zip(agg.phases, wf.phases):
        if not ph.switch_net:
            continue
        q = net.node_injection(np.array([x.cm.get(ph.switch_net, 0.0) for x in segs]))
        if q.any():
            src += (1j * omega * phase_wf.switch_node.c)[:, None] * q[None, :]
    sources = loop_sources(layouts, wf)
    if not sources:
        warnings.append("No capacitor loops (set the switching device's pins and the "
                        "capacitors): no inductive coupling")
    emf, res.m_h = victim_emf(snap, sources, segs, freqs, victim.net)

    def solve(driver_ohm: float, transfer: bool = False):
        terms = [Termination(d_xy, driver_ohm),
                 Termination(r_xy, victim.receiver_ohm, victim.receiver_pf * 1e-12,
                             victim.termination_ohm)]
        currents = {"capacitive": src}
        if ground_diff is not None:
            # The driver's ground sits ground_diff above the receiver's (the reference):
            # its output resistance drives the line from there.
            gsrc = np.zeros((len(freqs), net.n), complex)
            gsrc[:, net.nearest(d_xy)] = ground_diff / driver_ohm
            currents["common"] = gsrc
        return net.solve_many(freqs, terms, obs, currents, {"inductive": emf},
                              transfer_to=obs[0] if transfer else None)

    out = solve(victim.driver_ohm, transfer=True)
    n_t = wf.samples_per_period()
    for col, store in ((0, res.receiver_v), (1, res.driver_v)):
        total = np.zeros(len(freqs), complex)
        for key in MECHANISMS:
            if key not in out:
                continue
            store[key] = Spectrum(f"{victim.name} {key}", "V", 0.0, out[key][:, col])
            total += out[key][:, col]
        store[TOTAL] = Spectrum(f"{victim.name} total", "V", 0.0, total)
    res.peak_by = {k: float(np.max(np.abs(sp.samples(n_t)))) for k, sp in res.receiver_v.items()
                   if k != TOTAL}
    res.peak_v = float(np.max(np.abs(res.receiver_v[TOTAL].samples(n_t))))
    _shares(res, snap, victim, agg, wf, net, segs, emf, ground_diff, d_xy, n_t)
    # Held high and held low (#156): a driver whose resistance differs between them (an
    # open drain) is solved again. Each state is judged on the noise toward its threshold.
    totals = {victim.driver_ohm: res.receiver_v[TOTAL]}
    for st in victim.states():
        if st.name == "high" and st.level_v < st.threshold_v:
            warnings.append(f"Driven high through {st.driver_ohm:.4g} Ω it reaches only "
                            f"{st.level_v:.3g} V, under its {st.threshold_v:.3g} V threshold")
        if st.driver_ohm not in totals:
            o = solve(st.driver_ohm)
            totals[st.driver_ohm] = Spectrum(f"{victim.name} {st.name}", "V", 0.0,
                                             sum(o[k][:, 0] for k in MECHANISMS if k in o))
        sp = totals[st.driver_ohm]
        y = sp.samples(n_t)
        res.states.append(StateResult(st.name, st.level_v, st.threshold_v, st.driver_ohm, sp,
                                      max(0.0, float(np.max(st.toward * y)))))
        res.peak_v = max(res.peak_v, float(np.max(np.abs(y))))
    res.warnings = warnings
    return res


def _shares(res: VictimResult, snap: BoardSnapshot, victim: Victim, agg: Aggressor,
            wf: SmpsWaveforms, net: VictimNetwork, segs, emf: np.ndarray,
            ground_diff: Optional[np.ndarray], d_xy, n_t: int):
    """Where along the victim its peak noise couples in (#152): each segment's part of the
    receiver's voltage at the instant of its largest |v| (the line driven high), by
    mechanism, signed so that a positive share adds to the peak. The shares and the common-
    ground part at the driver add up to the peak."""
    y = res.receiver_v[TOTAL].samples(n_t)
    i = int(np.argmax(np.abs(y)))
    sign = 1.0 if y[i] >= 0 else -1.0
    k = np.arange(1, len(wf.freqs()) + 1)
    w = np.exp(2j * np.pi * k * i / n_t)
    omega = 2 * np.pi * wf.freqs()
    cap = np.zeros((len(k), len(segs)), complex)
    for ph, phase_wf in zip(agg.phases, wf.phases):
        if ph.switch_net:
            cm = np.array([x.cm.get(ph.switch_net, 0.0) * x.length_mm for x in segs])
            cap += (1j * omega * phase_wf.switch_node.c)[:, None] * cm[None, :]
    res.peak_t_s = i / (n_t * wf.fsw_hz)
    res.segment_v = {"capacitive": sign * net.shares(w, currents=cap),
                     "inductive": sign * net.shares(w, emfs=emf)}
    if ground_diff is not None:
        x = net.transfer[:, net.nearest(d_xy)]
        res.common_v = sign * 2.0 * float(np.real(w @ (ground_diff / victim.driver_ohm * x)))
    z = {c.id: c.z_mid_mm for c in snap.stackup.copper}
    res.segments_xyz = np.array([[(*x.a, z.get(x.layer, 0.0)), (*x.b, z.get(x.layer, 0.0))]
                                 for x in segs])
    first = min(range(len(segs)), key=lambda j: min(math.dist(segs[j].a, d_xy),
                                                     math.dist(segs[j].b, d_xy)))
    res.driver_xyz = np.array([*d_xy, z.get(segs[first].layer, 0.0)])
