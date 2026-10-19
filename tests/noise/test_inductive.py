"""Inductive coupling (#69): mutual inductances against closed forms, the image plane and
its shielding, the induced EMF in the victim's line, and whole solves."""

import math

import numpy as np
import pytest

from kipi.ac.model import CapModel
from kipi.noise.checks import seed_phases
from kipi.noise.extract.capacitive import Segment
from kipi.noise.extract.peec import (
    mirror, mutual, mutual_matrix, plane_shielding, polyline, segment,
)
from kipi.noise.extract.xsection import GROUND, VICTIM, Rect, solve as xsolve
from kipi.noise.model import Aggressor, Victim
from kipi.noise.network import Termination, VictimNetwork
from kipi.noise.parasitics import phase_layout
from kipi.noise.solver import solve_noise
from noise_boards import buck_board, buck_setup
from synthetic import F, IN2

MU0 = 4e-7 * math.pi


def test_the_matrix_is_the_pairwise_mutual():
    src = [segment([0, 0, 0], [5, 0, 0], 0.5, 0.035), segment([5, 0, 0], [5, 3, 0.2], .3, .3)]
    tgt = [segment([0, 1, 0.1], [4, 1, 0.1], 0.2, 0.035),
           segment([1, -2, 0], [1, 5, 0], 0.2, 0.035),
           segment([6, 0, -0.3], [6, 4, -0.3], 0.2, 0.035)]
    ref = np.array([[mutual(s, t) for t in tgt] for s in src])
    assert np.allclose(mutual_matrix(src, tgt), ref, rtol=1e-12, atol=1e-24)


def test_two_lines_over_a_plane():
    """M′ = μ0/4π·ln(1 + 4h²/s²) for two lines at height h, s apart, over a plane."""
    h, s, L = 0.2, 0.8, 200.0
    a = segment([0, 0, h], [L, 0, h], 0.3, 0.035)
    v = segment([0, s, h], [L, s, h], 0.2, 0.035)
    m = (mutual_matrix([a], [v]) - mutual_matrix([mirror(a, 0.0)], [v]))[0, 0] / L
    assert m == pytest.approx(1e-10 * math.log(1 + 4 * h * h / s / s), rel=0.05)


def test_far_loops_couple_as_dipoles():
    """Coplanar loops far apart: M = −μ0·A1·A2/(4π·d³)."""
    sq = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    a = polyline(sq, [0.05] * 4, 0.035)
    b = polyline([[x + 20, y, z] for x, y, z in sq], [0.05] * 4, 0.035)
    m = mutual_matrix(a, b).sum()
    a1 = a2 = 1e-6                                  # m²
    assert m == pytest.approx(-MU0 * a1 * a2 / (4 * math.pi * 0.020 ** 3), rel=0.05)


def test_a_strips_inductance_matches_the_field_solve():
    """L′ of a flat strip over a plane, by images, against μ0ε0/C0′ from the 2D solve.
    PEEC spreads the current evenly across the strip (its low-frequency distribution),
    the field solve puts it on the surface (the high-frequency limit): the first is a few
    % higher."""
    w, h, L = 1.0, 1.0, 400.0
    a = segment([0, 0, h], [L, 0, h], w, 0.035)
    peec_l = (mutual(a, a) - mutual(a, mirror(a, 0.0))) / L
    W = 20.0
    xs = xsolve([Rect(-W, W, -0.035, 0.0, GROUND),
                 Rect(-w / 2, w / 2, h - 0.0175, h + 0.0175, VICTIM)], [], (-W, W, -0.5, 20.0))
    assert xs.l_h_per_mm < peec_l < 1.1 * xs.l_h_per_mm


def test_a_plane_shields_high_frequencies():
    """1/cosh((1+j)·t/δ): 35 µm is 5.3 skin depths at 100 MHz, 17 at 1 GHz."""
    t = plane_shielding(np.array([1e3, 1e6, 100e6, 1e9]), 0.035)
    assert abs(t[0]) > 0.99
    assert abs(t[2]) == pytest.approx(0.01, rel=0.1)
    assert abs(t[3]) < 1e-6
    assert abs(t[0]) > abs(t[1]) > abs(t[2]) > abs(t[3])


def test_an_emf_divides_between_driver_and_receiver():
    """A short line with an EMF E along it: the receiver sees E·Z_R/(Z_D + Z_R)."""
    v = 299.792458e9 / math.sqrt(3.3)              # mm/s
    segs = [Segment((k * 0.5, 0.0), ((k + 1) * 0.5, 0.0), F, 0.2, 1 / (50 * v), 50 / v)
            for k in range(4)]
    net = VictimNetwork(segs, {F: 0.035})
    f = np.array([1e6])
    emf = np.zeros((1, 4), complex)
    emf[0, 1] = 1.0
    terms = [Termination((0.0, 0.0), 50.0), Termination((2.0, 0.0), 150.0)]
    out = net.solve_many(f, terms, [net.nearest((2.0, 0.0)), net.nearest((0.0, 0.0))],
                         emfs={"e": emf})["e"][0]
    assert abs(out[0]) == pytest.approx(150 / 200, rel=0.01)
    assert abs(out[1]) == pytest.approx(50 / 200, rel=0.01)
    assert out[0] == pytest.approx(-out[1] * 3, rel=0.01)       # opposite ends, opposite signs


def _setup(snap, victims):
    setup = buck_setup()
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1 buck", "U1", fsw_hz=1e6)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    a.phases[0].coss_f = 300e-12
    setup.aggressors.append(a)
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"]),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    setup.victims = victims
    return setup


def test_ground_returns_without_copper_go_through_the_plane():
    snap = buck_board()
    setup = _setup(snap, [])
    lay = phase_layout(snap, setup, setup.aggressors[0].phases[0], 1, "buck", 12.0, 1.8,
                       "VIN", "VOUT", "GND")
    labels = [s.label for s in lay.loops["C2"]]
    assert any("(plane)" in x for x in labels)               # no top-side ground there
    assert not any("(plane)" in x for x in (s.label for s in lay.loops["C1"]))
    assert lay.image_z == pytest.approx(snap.stackup.layer(4).z_top_mm)


def test_a_victim_beside_the_hot_loop_is_coupled_inductively():
    snap = buck_board(victim_gap=0.2, victim_by="hot_loop")
    v = solve_noise(snap, _setup(snap, [Victim("SIG")]), "U1 buck").victims[0]
    assert v.dominant == "inductive"
    assert v.peak_by["inductive"] > 5 * v.peak_by["capacitive"]
    assert abs(v.m_h["C1 loop (phase 1)"]) > 10 * abs(v.m_h["C2 loop (phase 1)"])
    assert set(v.receiver_v) == {"capacitive", "inductive", "total"}
    total = v.receiver_v["capacitive"].c + v.receiver_v["inductive"].c
    assert np.allclose(v.receiver_v["total"].c, total)


def test_the_plane_shields_a_buried_victim_from_the_hot_loop():
    top = buck_board(victim_gap=0.2, victim_by="hot_loop")
    buried = buck_board(victim_gap=0.2, victim_by="hot_loop", victim_layer=IN2)
    a = solve_noise(top, _setup(top, [Victim("SIG")]), "U1 buck").victims[0]
    b = solve_noise(buried, _setup(buried, [Victim("SIG")]), "U1 buck").victims[0]
    assert b.peak_by["inductive"] < 0.05 * a.peak_by["inductive"]


def test_inductive_coupling_falls_with_distance():
    peaks = []
    for gap in (0.2, 1.0, 3.0):
        snap = buck_board(victim_gap=gap, victim_by="hot_loop")
        peaks.append(solve_noise(snap, _setup(snap, [Victim("SIG")]),
                                 "U1 buck").victims[0].peak_by["inductive"])
    assert peaks[0] > peaks[1] > peaks[2] > 0
