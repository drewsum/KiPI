"""Capacitive coupling onto a victim net (#69): its line network against the lumped
weak-coupling formula, the cross-sections taken from the board, and whole solves."""

import math

import numpy as np
import pytest

from kipi.ac.model import CapModel
from kipi.core import yaml_io
from kipi.dc.pi.model import PinRef, PISetup
from kipi.noise.checks import guess_ends, seed_phases
from kipi.noise.extract.capacitive import BoardCuts, Segment, _bands, extract
from kipi.noise.model import VICTIM_PRESETS, Aggressor, Victim, logic_margin
from kipi.noise.network import Termination, VictimNetwork
from kipi.noise.report import load_results, save_results
from kipi.noise.solver import solve_noise
from noise_boards import buck_board, buck_setup
from synthetic import F, IN1, IN2


def _line(n=4, length=2.0, cm=10e-15, z0=50.0, er_eff=3.3):
    """A straight line of n segments with the given Cm′ (F/mm), Z0 and εeff."""
    v = 299.792458 / math.sqrt(er_eff)               # mm/ns -> 1e-9 scaling below
    l_per_mm = z0 / (v * 1e9) * 1e0                  # H/mm: Z0 / v, v in mm/s
    c_per_mm = 1.0 / (z0 * v * 1e9)
    segs = []
    for k in range(n):
        a, b = (k * length / n, 0.0), ((k + 1) * length / n, 0.0)
        segs.append(Segment(a, b, F, 0.2, c_per_mm, l_per_mm, {"SW": cm}))
    return segs


def test_a_short_line_matches_the_lumped_formula():
    """Electrically short: V = jω·Cm·(Z_D ∥ Z_R)·V_sw, the same at both ends."""
    segs = _line()
    net = VictimNetwork(segs, {F: 0.035})
    f = np.array([1e6, 10e6])
    w = 2 * np.pi * f
    q = net.node_injection(np.array([s.cm["SW"] for s in segs]))
    src = (1j * w)[:, None] * q[None, :]                # 1 V on the switch node
    terms = [Termination((0.0, 0.0), 33.0), Termination((2.0, 0.0), 1e6, 3e-12)]
    v = net.solve(f, terms, src, [net.nearest((2.0, 0.0)), net.nearest((0.0, 0.0))])
    cm = sum(s.cm["SW"] * s.length_mm for s in segs)
    zr = 1 / (1 / 1e6 + 1j * w * 3e-12)
    expect = 1j * w * cm * (33.0 * zr / (33.0 + zr))
    assert np.allclose(v[:, 0], expect, rtol=0.01)
    assert np.allclose(v[:, 1], expect, rtol=0.01)


def test_a_long_line_still_holds_its_driver_end():
    """At the line's quarter-wave frequency an open far end swings far more than a
    driven near end."""
    segs = _line(n=40, length=100.0)
    net = VictimNetwork(segs, {F: 0.035}, loss_tangent=0.0)
    f = np.array([1e6, 400e6])
    w = 2 * np.pi * f
    q = net.node_injection(np.array([s.cm["SW"] for s in segs]))
    v = net.solve(f, [Termination((0.0, 0.0), 1.0)], (1j * w)[:, None] * q[None, :],
                  [net.nearest((100.0, 0.0)), net.nearest((0.0, 0.0))])
    assert abs(v[1, 0]) > 5 * abs(v[1, 1])


def test_bands_follow_the_stackup():
    snap = buck_board()
    bands = _bands(snap.stackup, 4.3)
    f, in1 = snap.stackup.layer(F), snap.stackup.layer(IN1)
    core = next(b for b in bands if b.z1 == pytest.approx(f.z_bottom_mm))
    assert core.z0 == pytest.approx(in1.z_top_mm) and core.er == 4.5


def test_a_cut_finds_the_switch_node_and_plane():
    snap = buck_board(victim_gap=0.3)
    cuts = BoardCuts(snap, "SIG", ["SW"], 3.0)
    seg = Segment((12.0, 14.4), (13.0, 14.4), F, 0.2)
    rects, _, box = cuts.section(seg)
    groups = {r.group for r in rects}
    assert {"victim", "SW", "ground"} <= groups
    sw = [r for r in rects if r.group == "SW"]
    assert min(abs(r.x1) for r in sw) == pytest.approx(0.4, abs=0.02)   # 0.3 gap + 0.1
    plane = [r for r in rects if r.group == "ground"
             and r.z1 == pytest.approx(snap.stackup.layer(IN1).z_top_mm)]
    assert plane and box[0] == -3.0


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


def test_coupling_falls_with_distance():
    peaks = []
    for gap in (0.2, 0.5, 2.0):
        snap = buck_board(victim_gap=gap)
        r = solve_noise(snap, _setup(snap, [Victim("SIG")]), "U1 buck")
        peaks.append(r.victims[0].peak_v)
        assert r.victims[0].length_mm == pytest.approx(12.5)
    assert peaks[0] > 2 * peaks[1] > 4 * peaks[2] > 0


def test_a_victim_behind_the_plane_is_shielded():
    top = buck_board(victim_gap=0.2)
    buried = buck_board(victim_gap=0.2, victim_layer=IN2)
    a = solve_noise(top, _setup(top, [Victim("SIG")]), "U1 buck").victims[0]
    b = solve_noise(buried, _setup(buried, [Victim("SIG")]), "U1 buck").victims[0]
    assert b.cm_f["SW"] < 0.01 * a.cm_f["SW"]


def test_the_driver_holds_the_line_down():
    snap = buck_board(victim_gap=0.2)
    weak = Victim("SIG")
    weak.apply_preset("open_drain")
    r = solve_noise(snap, _setup(snap, [Victim("SIG"), Victim("SIG", name="weak",
                                                             preset="open_drain",
                                                             driver_ohm=4700.0)]), "U1 buck")
    strong, weak = r.victims
    assert weak.peak_v > 5 * strong.peak_v
    assert strong.passed and strong.dominant == "capacitive"
    assert strong.ratio == pytest.approx(strong.peak_v / 0.4)


def test_victims_save_and_reopen(tmp_path):
    snap = buck_board(victim_gap=0.2)
    setup = _setup(snap, [Victim("SIG", driver=PinRef("U3", "SIG", ["1"]),
                                 receiver=PinRef("U4", "SIG", ["1"]))])
    r = solve_noise(snap, setup, "U1 buck")
    assert r.victims[0].warnings == []
    back = load_results(save_results(tmp_path / "v.kipi-noise-results",
                                     {r.aggressor: r}, "b", setup))
    got = back["results"]["U1 buck"].victims[0]
    assert got.peak_v == pytest.approx(r.victims[0].peak_v)
    assert np.allclose(got.receiver_v["total"].c, r.victims[0].receiver_v["total"].c)
    assert back["setup"].victims == setup.victims


def test_victims_in_the_setup_file():
    s = PISetup(victims=[Victim("SIG", name="clock", preset="custom", driver_ohm=12.0,
                                termination_ohm=50.0, logic_v=1.8, noise_margin_v=0.05,
                                driver=PinRef("U3", "SIG", ["1"]))])
    s.noise.segment_mm, s.noise.window_mm = 0.25, 5.0
    assert PISetup.from_dict(yaml_io.loads(yaml_io.dumps(s.to_dict()))) == s
    assert "U3" in s.part_refs() and "SIG" in s.pin_nets()


def test_noise_margins():
    assert logic_margin(3.3) == pytest.approx(0.4)
    assert logic_margin(1.8) == pytest.approx(0.18)
    assert logic_margin(0.9) == pytest.approx(0.135)          # off the table: 15 %
    v = Victim("X", logic_v=1.8)
    assert v.margin() == pytest.approx(0.18)
    v.apply_preset("lvds")
    assert v.margin() == 0.1 and v.termination_ohm == 50.0
    v.noise_margin_v = 0.02
    assert v.margin() == 0.02
    assert set(VICTIM_PRESETS) >= {"cmos", "open_drain", "series", "parallel", "lvds",
                                   "analog"}


def test_ends_are_guessed_farthest_apart():
    snap = buck_board(victim_gap=0.2)
    drv, rcv = guess_ends(snap, "SIG")
    assert {drv.ref, rcv.ref} == {"U3", "U4"}


def test_extraction_reuses_cuts_along_a_straight_run():
    snap = buck_board(victim_gap=2.5)
    ext = extract(snap, "SIG", ["SW"], 0.5, 3.0)
    assert len(ext.segments) == 25
    assert ext.solves < 12 and ext.cached > 20
