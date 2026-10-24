"""Targets apply only above the VRM's control loop bandwidth (the AC solve gives the
open-loop PDN impedance), with a fallback when that leaves nothing to judge."""


import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.html_report import write_ac_report
from kipi.ac.solver import solve_ac
from kipi.ac.solver.sweep import ProbeCurve
from kipi.ac.target import judged_from, target_at

F = np.logspace(3, 8, 51)            # 1 kHz to 100 MHz, 10 per decade


def test_target_applies_only_above_the_bandwidth():
    t = target_at(F, 0.01, 1e7, above_hz=1e5)
    assert np.all(np.isnan(t[F < 1e5]))
    assert np.all(t[(F >= 1e5) & (F <= 1e7)] == 0.01)
    assert np.all(np.isnan(t[F > 1e7]))
    mask = target_at(F, points=[(1e3, 0.01), (1e8, 0.1)], above_hz=1e6)
    assert np.all(np.isnan(mask[F < 1e6])) and np.all(np.isfinite(mask[F >= 1e6]))


def test_judged_from_the_bandwidth():
    assert judged_from(None, F, 0.01, 1e7) == (None, None)
    assert judged_from(2e4, F, 0.01, 1e7) == (2e4, None)
    assert judged_from(2e4, F) == (2e4, None)                 # no target: kept for worst |Z|


def test_bandwidth_above_the_target_frequency_falls_back():
    above, why = judged_from(5e6, F, 0.01, 1e6)
    assert above is None
    assert "target frequency" in why and "whole range" in why
    assert judged_from(1e6, F, 0.01, 1e6)[0] is None           # nothing strictly between


def test_bandwidth_above_the_sweep_falls_back():
    above, why = judged_from(1e9, F, points=[(1e3, 0.01), (1e8, 0.1)])
    assert above is None and "above the sweep" in why


def test_pass_fail_ignores_the_open_loop_rise_below_the_bandwidth():
    z = np.where(F < 1e4, 1.0, 0.005).astype(complex)        # open loop: high at low f
    judged = ProbeCurve("U1", z, target_ohm=0.01, target_hz=1e7, above_hz=2e4)
    assert judged.passed(F) is True
    assert judged.worst(F)[1] >= 2e4
    assert ProbeCurve("U1", z, target_ohm=0.01, target_hz=1e7).passed(F) is False
    free = ProbeCurve("U1", z, above_hz=2e4)                  # no target: worst above it
    assert free.worst(F) == (0.005, pytest.approx(F[F >= 2e4][0]))


def _solve(bandwidth_khz, target_hz=50e6):
    snap = plane_pair()
    setup = plane_setup(snap, f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=10,
                        field_points_per_decade=2)
    setup.vrms[0].bandwidth_khz = bandwidth_khz
    setup.probes[0].target_ohm, setup.probes[0].target_hz = 0.05, target_hz
    return snap, setup, solve_ac(snap, setup, "VRM1", "GND")


def test_solve_judges_above_the_vrm_bandwidth():
    _, _, r = _solve(500.0)
    assert r.bandwidth_hz == 5e5 and r.probes[0].above_hz == 5e5
    t = r.probes[0].target_at(r.freqs)
    assert np.all(np.isnan(t[r.freqs < 5e5]))
    assert not any("bandwidth" in w for w in r.warnings)


def test_solve_falls_back_and_says_why():
    _, _, r = _solve(2e4, target_hz=1e6)                      # 20 MHz above 1 MHz
    assert r.probes[0].above_hz is None
    assert any("target frequency" in w for w in r.warnings)


def test_report_shows_the_bandwidth(tmp_path):
    snap, setup, r = _solve(500.0)
    text = write_ac_report(tmp_path / "r.html", r, snap, setup).read_text("utf-8")
    assert "VRM bandwidth 500 kHz" in text and "open-loop PDN impedance" in text
    assert "from 500 kHz to 50 MHz" in text



def test_a_rail_whose_vrm_has_no_bandwidth_is_not_solved():
    from kipi.ac.checks import validate_ac
    from kipi.ac.solver.fem import ACError
    snap = plane_pair()
    setup = plane_setup(snap)
    assert not any("bandwidth" in e for e in validate_ac(setup, "VRM1", "GND"))
    setup.vrms[0].bandwidth_khz = None                      # #183
    assert any("VRM1: enter its control loop bandwidth" in e
               for e in validate_ac(setup, "VRM1", "GND"))
    with pytest.raises(ACError, match="control loop bandwidth"):
        solve_ac(snap, setup, "VRM1", "GND")


def test_a_new_vrm_starts_at_50_khz():
    from kipi.dc.pi.model import PinRef, PISetup, Vrm
    assert Vrm("U1", PinRef("U1", "+3V3"), 3.3).bandwidth_khz == 50.0      # #190
    d = PISetup(vrms=[Vrm("U1", PinRef("U1", "+3V3"), 3.3, bandwidth_khz=None)]).to_dict()
    assert PISetup.from_dict(d).vrms[0].bandwidth_khz is None              # kept as saved
    del d["vrms"][0]["bandwidth_khz"]
    assert PISetup.from_dict(d).vrms[0].bandwidth_khz == 50.0
