"""The victim net as a circuit (#69): its trace segments as transmission lines, its driver
and receiver as terminations, and the coupled noise as sources.

Each segment is a lossy line two-port with Z0 = √((R′+jωL′)/(G′+jωC′)) and
γ = √((R′+jωL′)(G′+jωC′)), in nodal (Y) form:

    Y = 1/Z0 · [[coth γl, −csch γl], [−csch γl, coth γl]].

Segments join where their ends meet (on any layer: a via joins them), so a branching net
is one network. The driver is its output impedance to ground, the receiver its input
resistance and capacitance in parallel with any termination; other ends are open.

Weak coupling: the victim does not load the aggressor, so the switch node's voltage is a
source. Its charge enters each segment as a current jω·Cm′·l·V_sw, half at each end. A
current loop's flux induces an EMF E along a segment instead: half of it in series at
each end, which is the Norton current ∓(y11 − y12)·E/2 into the segment's two nodes. The
nodal equations Y·V = I are solved at every harmonic, once per mechanism with the same
factorization; the receiver's and driver's voltages are the coupled noise.

Y is symmetric, so one more solve with a unit current at the receiver gives the receiver's
voltage per unit current injected at each node (reciprocity). With it, each segment's share
of the receiver's noise is found without a solve per segment (#152).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

from kipi.noise.extract.capacitive import Segment

RHO_CU_OHM_MM = 1.724e-5
COPPER_T_MM = 0.035
SNAP_MM = 1e-3                      # segment ends this close are one node
LEAK_S = 1e-9                       # keeps an isolated stub's node from floating


@dataclass
class Termination:
    node_xy: Tuple[float, float]
    r_ohm: float                     # to ground (inf: none)
    c_f: float = 0.0
    r_parallel_ohm: Optional[float] = None   # a parallel termination beside it

    def admittance(self, omega: np.ndarray) -> np.ndarray:
        y = (0.0 if not math.isfinite(self.r_ohm) else 1.0 / self.r_ohm) + 1j * omega * self.c_f
        if self.r_parallel_ohm:
            y = y + 1.0 / self.r_parallel_ohm
        return np.broadcast_to(y, np.shape(omega)).astype(complex)


class VictimNetwork:
    def __init__(self, segments: Sequence[Segment], thickness_mm: Dict[int, float],
                 loss_tangent: float = 0.02):
        self.segments = list(segments)
        self.loss_tangent = loss_tangent
        nodes: Dict[Tuple[int, int], int] = {}

        def node(p):
            key = (round(p[0] / SNAP_MM), round(p[1] / SNAP_MM))
            if key not in nodes:
                nodes[key] = len(nodes)
            return nodes[key]

        self.ends = np.array([(node(s.a), node(s.b)) for s in self.segments], dtype=int)
        self.n = len(nodes)
        self.xy = np.zeros((self.n, 2))
        for (kx, ky), i in nodes.items():
            self.xy[i] = (kx * SNAP_MM, ky * SNAP_MM)
        s = self.segments
        self.length = np.array([x.length_mm for x in s])
        self.r = np.array([RHO_CU_OHM_MM / (x.width_mm * thickness_mm.get(x.layer, COPPER_T_MM))
                           for x in s])
        # A split's detour inductance (#146) is spread along its segment.
        self.l = np.array([x.l_h_per_mm + getattr(x, "series_l_h", 0.0) / max(x.length_mm, 1e-9)
                           for x in s])
        self.c = np.array([x.c_f_per_mm for x in s])

    def nearest(self, xy) -> int:
        d = np.hypot(self.xy[:, 0] - xy[0], self.xy[:, 1] - xy[1])
        return int(np.argmin(d))

    def node_injection(self, cm_per_mm: np.ndarray) -> np.ndarray:
        """Each node's share of the segments' Cm′·l (half from each segment end), F."""
        q = np.zeros(self.n)
        half = 0.5 * cm_per_mm * self.length
        np.add.at(q, self.ends[:, 0], half)
        np.add.at(q, self.ends[:, 1], half)
        return q

    def solve(self, freqs: np.ndarray, terminations: Sequence[Termination],
              sources: np.ndarray, observe: Sequence[int]) -> np.ndarray:
        """Node voltages at ``observe`` for each frequency: ``sources`` (F, n) are the
        currents injected at every node."""
        return self.solve_many(freqs, terminations, observe, {"": sources})[""]

    def solve_many(self, freqs: np.ndarray, terminations: Sequence[Termination],
                   observe: Sequence[int], currents: Optional[Dict[str, np.ndarray]] = None,
                   emfs: Optional[Dict[str, np.ndarray]] = None,
                   transfer_to: Optional[int] = None) -> Dict[str, np.ndarray]:
        """Node voltages at ``observe`` (F, len(observe)) for each named set of sources:
        ``currents`` (F, nodes) injected at the nodes, or ``emfs`` (F, segments) induced
        along the segments from end a to end b.

        With ``transfer_to`` (a node), :attr:`transfer` is then the voltage there per unit
        current into each node (F, nodes), and :attr:`emf_gain` each segment's Norton
        current per unit EMF (F, segments): what :meth:`shares` needs."""
        from scipy.sparse import coo_matrix
        from scipy.sparse.linalg import splu
        currents, emfs = currents or {}, emfs or {}
        names = list(dict.fromkeys([*currents, *emfs]))
        out = {n: np.zeros((len(freqs), len(observe)), complex) for n in names}
        term_nodes = np.array([self.nearest(t.node_xy) for t in terminations], dtype=int)
        a, b = self.ends[:, 0], self.ends[:, 1]
        idx = np.arange(self.n)
        rows = np.concatenate([a, b, a, b, idx, term_nodes])
        cols = np.concatenate([a, b, b, a, idx, term_nodes])
        observe = list(observe)
        if transfer_to is not None:
            self.transfer = np.zeros((len(freqs), self.n), complex)
            self.emf_gain = np.zeros((len(freqs), len(self.segments)), complex)
            unit = np.zeros(self.n, complex)
            unit[transfer_to] = 1.0
        for k, f in enumerate(freqs):
            w = 2 * math.pi * f
            zs = self.r + 1j * w * self.l
            ys = w * self.c * (self.loss_tangent + 1j)
            z0 = np.sqrt(zs / ys)
            gl = np.sqrt(zs * ys) * self.length
            y11 = 1.0 / (z0 * np.tanh(gl))
            y12 = -1.0 / (z0 * np.sinh(gl))
            yt = np.array([complex(t.admittance(np.array(w))) for t in terminations],
                          dtype=complex)
            data = np.concatenate([y11, y11, y12, y12, np.full(self.n, LEAK_S + 0j), yt])
            lu = splu(coo_matrix((data, (rows, cols)), shape=(self.n, self.n)).tocsc())
            if transfer_to is not None:
                self.transfer[k] = lu.solve(unit)
                self.emf_gain[k] = 0.5 * (y11 - y12)
            for name in names:
                rhs = np.zeros(self.n, complex)
                if name in currents:
                    rhs += currents[name][k]
                if name in emfs:
                    i = 0.5 * (y11 - y12) * emfs[name][k]
                    np.add.at(rhs, a, -i)
                    np.add.at(rhs, b, i)
                out[name][k] = lu.solve(rhs)[observe]
        return out

    def shares(self, weights: np.ndarray, currents: Optional[np.ndarray] = None,
               emfs: Optional[np.ndarray] = None) -> np.ndarray:
        """Each segment's part of the observed voltage Σ_k 2·Re(w_k·V_k), after a
        :meth:`solve_many` with ``transfer_to``: for ``currents`` (F, segments) entering a
        segment, half at each end, or ``emfs`` (F, segments) along it. ``weights`` (F,)
        are e^{jω_k·t} for the voltage at time t; the shares add up to it."""
        a, b = self.ends[:, 0], self.ends[:, 1]
        x = self.transfer
        v = np.zeros((len(weights), len(self.segments)), complex)
        if currents is not None:
            v += currents * 0.5 * (x[:, a] + x[:, b])
        if emfs is not None:
            v += self.emf_gain * emfs * (x[:, b] - x[:, a])
        return 2.0 * np.real(weights @ v)
