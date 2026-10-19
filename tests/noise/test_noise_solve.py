"""A Coupled Noise solve (#69): the hot loop and switch node from the board, the waveforms
from them, and the results saved and reopened."""

import math
from pathlib import Path

import numpy as np
import pytest

from kipi.ac.model import CapModel
from kipi.core.progress import ProgressReporter
from kipi.noise.checks import NoiseError, seed_phases
from kipi.noise.model import Aggressor
from kipi.noise.report import load_results, save_results
from kipi.noise.solver import solve_noise
from noise_boards import buck_board, buck_setup

ROOT = Path(__file__).resolve().parents[2]


def _setup(snap, coss=300e-12, **agg):
    setup = buck_setup()
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1 buck", "U1", fsw_hz=1e6, rise_s=2e-9, fall_s=2e-9, **agg)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    a.phases[0].coss_f = coss
    setup.aggressors.append(a)
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"]),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    return setup


class _Partials(ProgressReporter):
    def __init__(self):
        super().__init__()
        self.got = []

    def partial(self, result):
        self.got.append(result)


def test_the_hot_loop_and_switch_node_come_from_the_board():
    snap = buck_board()
    setup = _setup(snap)
    progress = _Partials()
    r = solve_noise(snap, setup, "U1 buck", progress)
    assert progress.got and progress.got[0] is r          # the waveforms are shown early
    (h,) = r.hot_loops
    # A ~4 x 4 mm loop 0.2 mm over its ground plane, plus the capacitor's 0.4 nH ESL.
    board = h.cap_loops_h["C1"]
    assert 0.1e-9 < board < 3e-9
    assert h.loop_l_h == pytest.approx(board + 0.4e-9)
    assert h.image_plane == "In1.Cu"
    # The switch node's copper over the plane: at least its 4 x 4 mm pour at 0.2 mm, εr 4.5,
    # plus the track and pads beyond it and their fringing.
    plate = 8.854e-15 * 4.5 * 16 / 0.2
    assert plate < h.c_copper_f < 1.6 * plate
    assert h.c_sw_f == pytest.approx(300e-12 + h.c_copper_f)
    assert h.f_ring_hz == pytest.approx(1 / (2 * math.pi * math.sqrt(h.loop_l_h * h.c_sw_f)))
    assert 0 < h.zeta < 1
    assert not h.coss_default


def test_a_bigger_switch_node_has_more_capacitance():
    small = solve_noise(buck_board(2.0), _setup(buck_board(2.0)), "U1 buck")
    big = solve_noise(buck_board(8.0), _setup(buck_board(8.0)), "U1 buck")
    assert big.hot_loops[0].c_copper_f > 3 * small.hot_loops[0].c_copper_f


def test_the_waveforms_follow_the_operating_point():
    snap = buck_board()
    r = solve_noise(snap, _setup(snap), "U1 buck")
    wf = r.waveforms
    assert wf.topology == "buck" and wf.duty == pytest.approx(1.8 / 12)
    assert wf.phases[0].inductor.mean == pytest.approx(5.0)
    assert set(wf.input_caps) == {"C1"} and set(wf.output_caps) == {"C2"}
    assert wf.f_max_hz >= 10 / (math.pi * 2e-9)
    x = wf.phases[0].switch_node.samples(wf.samples_per_period())
    assert x.max() > 12.0                                 # it rings past the input
    assert any("Phase 1: hot loop" in line for line in r.summary_lines())


def test_missing_coss_and_models_are_warned():
    snap = buck_board()
    setup = _setup(snap, coss=None)
    setup.caps = []
    r = solve_noise(snap, setup, "U1 buck")
    assert r.hot_loops[0].coss_default
    text = " ".join(r.warnings)
    assert "Coss" in text and "C1 has no capacitor model" in text
    assert r.waveforms.input_caps["C1"].ac_rms() > 0      # its value from the board: 10 µF


def test_a_bad_setup_does_not_solve():
    snap = buck_board()
    setup = _setup(snap)
    setup.aggressors[0].phases[0].inductance_h = 0.0
    with pytest.raises(NoiseError, match="inductance"):
        solve_noise(snap, setup, "U1 buck")
    with pytest.raises(NoiseError, match="No aggressor"):
        solve_noise(snap, setup, "nope")


def test_results_save_and_reopen(tmp_path):
    snap = buck_board()
    setup = _setup(snap)
    r = solve_noise(snap, setup, "U1 buck")
    path = save_results(tmp_path / "x.kipi-noise-results", {r.aggressor: r}, snap.board_name,
                        setup)
    back = load_results(path)
    got = back["results"]["U1 buck"]
    assert back["setup"].aggressors[0].name == "U1 buck"
    assert np.allclose(got.waveforms.phases[0].switch_node.c,
                       r.waveforms.phases[0].switch_node.c)
    assert got.hot_loops[0].f_ring_hz == pytest.approx(r.hot_loops[0].f_ring_hz)
    assert got.summary_lines() == r.summary_lines()


def test_other_modes_results_name_their_mode(tmp_path):
    from kipi.ac.report import load_results as load_ac
    from kipi.dc.pi.report import load_results as load_dc
    snap = buck_board()
    setup = _setup(snap)
    r = solve_noise(snap, setup, "U1 buck")
    path = save_results(tmp_path / "x.kipi-noise-results", {r.aggressor: r}, "b", setup)
    with pytest.raises(ValueError, match="Coupled Noise mode"):
        load_ac(path)
    with pytest.raises(ValueError, match="Coupled Noise mode"):
        load_dc(path, ProgressReporter())


def test_the_example_multiphase_buck():
    """examples/multiphase_buck: four phases from its VRM, with its inductors' values."""
    from kipi.core.model import snapshot_io
    from kipi.dc.pi.model import PISetup
    ex = ROOT / "examples" / "multiphase_buck"
    snap = snapshot_io.load(ex / "multiphase_buck.kipi.json.gz")
    setup = PISetup.load(ex / "multiphase_buck.kipi-setup.yml")
    setup.noise.board_model = False                 # ~40 s on this board; tested apart
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1", "U1", fsw_hz=600e3)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    assert [p.inductor for p in a.phases] == ["L1", "L2", "L3", "L4"]
    assert all(p.inductance_h == pytest.approx(150e-9) for p in a.phases)
    setup.aggressors.append(a)
    r = solve_noise(snap, setup, "U1")
    assert len(r.hot_loops) == 4
    total = sum(p.inductor.c for p in r.waveforms.phases)
    # Four interleaved phases: only every fourth harmonic is left in their sum.
    assert np.abs(total[0]) < 1e-9 and np.abs(total[3]) > 0.01


def test_the_ringing_is_damped_at_least_as_set():
    """The loop's resistance alone barely damps a real switch node; the settings' least
    damping stands in for the losses the model leaves out, and 0 turns it off."""
    snap = buck_board()
    setup = _setup(snap)
    h = solve_noise(snap, setup, "U1 buck").hot_loops[0]
    assert h.zeta_loop < 0.05 and h.zeta == pytest.approx(0.05)
    assert any("least damping" in line
               for line in solve_noise(snap, setup, "U1 buck").summary_lines())
    setup.noise.min_ring_zeta = 0.0
    h0 = solve_noise(snap, setup, "U1 buck").hot_loops[0]
    assert h0.zeta == pytest.approx(h0.zeta_loop) == pytest.approx(h.zeta_loop)
