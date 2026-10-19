"""Hard and soft switch-node edges: the control switch's turn-on rings and sweeps out the
other switch's reverse-recovery charge, its turn-off is carried across by the inductor
current into a body diode's clamp and barely rings."""

import math

import numpy as np
import pytest

from kipi.noise.model import DEFAULT_QRR_C, AggressorPhase, NoiseSettings, phase_from_dict
from kipi.noise.waveforms import CapBranch, PhaseParts, edge_shapes, synthesize

FSW = 500e3


def _run(topology="buck", qrr=0.0, soft=0.5, iout=5.0, c=300e-12, rise=1e-9, fall=1e-9):
    vin, vout = (12.0, 1.2) if topology == "buck" else (5.0, 12.0)
    duty = vout / vin if topology == "buck" else 1 - vin / vout
    cap = CapBranch("C", 10e-6, 1e-9, 0.0)
    return synthesize(topology, vin, vout, iout, iout * vout / vin, duty, FSW, rise, fall,
                      [PhaseParts(2.2e-6, 150e6, 0.05, c, soft, qrr)], 4000, [cap], [cap],
                      4000 * FSW)


def _edges(wf, topology="buck"):
    """The overshoot past the level each edge settles to: (turn-on, turn-off)."""
    n = wf.samples_per_period()
    x = wf.phases[0].switch_node.samples(n)
    hi = 12.0
    on = slice(0, n // 20)                       # the turn-on edge is at t = 0
    d = int(wf.duty * n)
    off = slice(d, d + n // 20)
    if topology == "buck":                       # rises at turn-on, falls at turn-off
        return x[on].max() - hi, -x[off].min()
    return -x[on].min(), x[off].max() - hi       # a boost: the other way round


@pytest.mark.parametrize("topology", ["buck", "boost"])
def test_the_turn_off_edge_barely_rings(topology):
    hard, soft = _edges(_run(topology), topology)
    assert hard > 0.5 * 12.0                     # ζ = 0.05: most of the swing again
    assert soft < 0.25 * hard
    # With the soft edges damped no more than the hard, both ring alike.
    hard, soft = _edges(_run(topology, soft=0.05), topology)
    assert soft == pytest.approx(hard, rel=0.25)


def test_which_edge_is_hard_follows_the_inductor_current():
    ph = PhaseParts(2.2e-6, 150e6, 0.05, 300e-12, 0.5, 5e-9)
    on, off = edge_shapes("buck", 1.0, 3.0, 1e-9, 1e-9, 12.0, ph)
    assert (on.name, on.hard, off.name, off.hard) == ("rising", True, "falling", False)
    assert off.zeta == 0.5 and on.zeta == 0.05
    on, off = edge_shapes("boost", 1.0, 3.0, 1e-9, 1e-9, 12.0, ph)
    assert (on.name, on.hard, off.name) == ("falling", True, "rising")
    # A valley current below zero (forced PWM at light load) carries the node up at
    # turn-on too: zero-voltage switching, no recovery.
    on, _ = edge_shapes("buck", -0.5, 2.0, 1e-9, 1e-9, 12.0, ph)
    assert not on.hard and on.irr_a == 0.0


def test_a_soft_edge_is_no_faster_than_the_inductor_current_can_carry_it():
    ph = PhaseParts(2.2e-6, 150e6, 0.05, 300e-12)
    _, off = edge_shapes("buck", 0.2, 0.5, 1e-9, 1e-9, 12.0, ph)
    assert off.time_s == pytest.approx(300e-12 * 12.0 / 0.5)
    _, off = edge_shapes("buck", 5.0, 6.0, 1e-9, 1e-9, 12.0, ph)
    assert off.time_s == 1e-9                    # 0.6 ns: the entered time stands


def test_the_recovery_current_carries_qrr():
    ph = PhaseParts(2.2e-6, 150e6, 0.05, 300e-12, 0.5, 10e-9)
    on, _ = edge_shapes("buck", 4.0, 6.0, 2e-9, 2e-9, 12.0, ph)
    didt = 4.0 / 2e-9
    assert 0.5 * on.irr_a * (on.irr_a / didt + 2e-9) == pytest.approx(10e-9)
    bare, rr = _run(), _run(qrr=10e-9)
    # The high side draws Qrr more from the input every period.
    assert rr.phases[0].pulsed.mean - bare.phases[0].pulsed.mean == pytest.approx(10e-9 * FSW)
    # The snap-off rings the node higher; the soft edge is untouched.
    assert _edges(rr)[0] > _edges(bare)[0] + 1.0
    assert _edges(rr)[1] == pytest.approx(_edges(bare)[1], abs=0.05)
    assert "reverse recovery 10 nC" in rr.phases[0].edges
    assert "falling edge soft" in rr.phases[0].edges


def test_qrr_and_the_soft_edge_damping_are_kept_and_defaulted():
    assert NoiseSettings().soft_edge_zeta == 0.5
    assert AggressorPhase().qrr_c is None and DEFAULT_QRR_C > 0
    ph = phase_from_dict({"qrr_c": 0.0}, lambda d: None)
    assert ph.qrr_c == 0.0                       # a Schottky low side: none, not default
    assert phase_from_dict({}, lambda d: None).qrr_c is None
    assert math.isclose(NoiseSettings(soft_edge_zeta=0.7).soft_edge_zeta, 0.7)


def test_a_solve_reports_its_edges_and_the_assumed_qrr():
    from kipi.ac.model import CapModel
    from kipi.noise.checks import seed_phases
    from kipi.noise.model import Aggressor
    from kipi.noise.solver import solve_noise
    from noise_boards import buck_board, buck_setup
    snap = buck_board()
    setup = buck_setup(board_model=False)
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1 buck", "U1", fsw_hz=1e6)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    setup.aggressors.append(a)
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"])]
    r = solve_noise(snap, setup, "U1 buck")
    lines = r.summary_lines()
    assert any("rising edge hard (reverse recovery 5 nC" in x for x in lines)
    assert any("No Qrr entered" in w for w in r.warnings)
    a.phases[0].qrr_c = 0.0
    r = solve_noise(snap, setup, "U1 buck")
    assert not any("No Qrr entered" in w for w in r.warnings)
    assert any("rising edge hard, ζ" in x for x in r.summary_lines())
    assert np.isfinite(r.waveforms.phases[0].switch_node.c).all()
