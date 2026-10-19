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
from kipi.noise.result import MECHANISMS, TOTAL, VictimResult
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
    terms = [Termination(d_xy, victim.driver_ohm),
             Termination(r_xy, victim.receiver_ohm, victim.receiver_pf * 1e-12,
                         victim.termination_ohm)]
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
    emf, res.m_h = victim_emf(snap, sources, segs, freqs)
    currents = {"capacitive": src}
    if ground_diff is not None:
        # The driver's ground sits ground_diff above the receiver's (the reference): its
        # output resistance drives the line from there.
        gsrc = np.zeros((len(freqs), net.n), complex)
        gsrc[:, net.nearest(d_xy)] = ground_diff / victim.driver_ohm
        currents["common"] = gsrc
    out = net.solve_many(freqs, terms, obs, currents, {"inductive": emf})
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
    res.warnings = warnings
    return res
