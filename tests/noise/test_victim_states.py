"""A victim held high and held low (#156): each state's level, threshold and driver
resistance, its noise solved with that driver, and the plot of both."""

import os

import numpy as np
import pytest

from kipi.ac.model import CapModel
from kipi.core import yaml_io
from kipi.dc.pi.model import PISetup
from kipi.noise.checks import seed_phases
from kipi.noise.model import Aggressor, Victim, logic_levels
from kipi.noise.report import load_results, save_results
from kipi.noise.solver import solve_noise
from noise_boards import buck_board, buck_setup


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


def test_a_cmos_line_is_judged_against_vih_high_and_vil_low():
    hi, lo = Victim("X").states()
    assert (hi.name, hi.level_v, hi.threshold_v, hi.driver_ohm) == ("high", 2.4, 2.0, 33.0)
    assert (lo.name, lo.level_v, lo.threshold_v, lo.driver_ohm) == ("low", 0.4, 0.8, 33.0)
    assert hi.toward == -1 and lo.toward == 1
    assert hi.margin_v == pytest.approx(0.4) and lo.margin_v == pytest.approx(0.4)


def test_an_open_drain_pulls_low_through_its_switch():
    v = Victim("X")
    v.apply_preset("open_drain")
    hi, lo = v.states()
    assert (hi.driver_ohm, lo.driver_ohm) == (4700.0, 20.0)
    v.apply_preset("cmos")
    assert v.driver_low_ohm is None and v.states()[1].driver_ohm == 33.0


def test_a_margin_set_by_hand_moves_the_thresholds():
    hi, lo = Victim("X", logic_v=1.8, noise_margin_v=0.05).states()
    assert hi.threshold_v == pytest.approx(1.35 - 0.05)
    assert lo.threshold_v == pytest.approx(0.45 + 0.05)
    assert logic_levels(0.9) == pytest.approx((0.315, 0.585, 0.18, 0.72))   # off the table


def test_the_driving_low_resistance_is_saved():
    s = PISetup(victims=[Victim("SIG", preset="custom", driver_ohm=4700.0,
                                driver_low_ohm=15.0)])
    back = PISetup.from_dict(yaml_io.loads(yaml_io.dumps(s.to_dict())))
    assert back.victims[0].driver_low_ohm == 15.0 and back == s


def test_each_state_is_solved_with_its_own_driver(tmp_path):
    snap = buck_board(victim_gap=0.2)
    od = Victim("SIG", name="od")
    od.apply_preset("open_drain")
    setup = _setup(snap, [Victim("SIG"), od])
    r = solve_noise(snap, setup, "U1 buck")
    cmos, od = r.victims
    # The same driver both ways: one solve, the same noise in both states.
    hi, lo = cmos.states
    assert hi.receiver_v is lo.receiver_v is cmos.receiver_v["total"]
    n = r.waveforms.samples_per_period()
    y = hi.receiver_v.samples(n)
    assert hi.excursion_v == pytest.approx(max(0.0, -y.min()))
    assert lo.excursion_v == pytest.approx(max(0.0, y.max()))
    assert max(hi.excursion_v, lo.excursion_v) == pytest.approx(cmos.peak_v)
    # The open drain: released high, only the pull-up holds the line; pulled low, the
    # switch holds it far harder.
    hi, lo = od.states
    assert hi.receiver_v is od.receiver_v["total"] and lo.receiver_v is not hi.receiver_v
    assert lo.excursion_v < 0.25 * hi.excursion_v
    assert od.peak_v == pytest.approx(float(np.max(np.abs(hi.receiver_v.samples(n)))))
    assert any("held low" in line for line in r.summary_lines())
    back = load_results(save_results(tmp_path / "s.kipi-noise-results", {r.aggressor: r},
                                     "b", setup))["results"]["U1 buck"].victims[1]
    assert [st.name for st in back.states] == ["high", "low"]
    assert np.allclose(back.states[1].receiver_v.c, lo.receiver_v.c)


# ---------------------------------------------------------------------------
# The plot and the editor
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_plot_shows_the_line_held_high_and_low(app):
    from kipi.noise.gui.plot import NoisePlot
    snap = buck_board(victim_gap=0.2)
    od = Victim("SIG")
    od.apply_preset("open_drain")
    r = solve_noise(snap, _setup(snap, [od]), "U1 buck")
    plot = NoisePlot()
    plot.set_result(r)
    plot.set_victim("SIG")
    plot.set_view("states")
    assert len(plot.axes) == 2 and plot.victim_row.isHidden()
    (lh, th, yh, _, ah), (ll, tl, yl, _, al) = plot._curves
    assert ah is plot.axes[0] and al is plot.axes[1]
    assert "held high" in lh and "held low" in ll
    assert np.mean(yh) == pytest.approx(2.4, abs=0.05)
    assert np.mean(yl) == pytest.approx(0.4, abs=0.05)
    assert "held high" in plot.axes[0].get_title()
    plot.chk_spectrum.setChecked(True)
    assert len(plot.axes) == 1 and len(plot._curves) == 2
    plot.set_view("sw")
    assert len(plot.axes) == 1 and plot._curves
    plot.close()


def test_the_editor_sets_the_driving_low_resistance(app):
    from kipi.noise.gui.panel import NoisePanel
    from kipi.noise.gui.panel import _get_optional
    p = NoisePanel()
    setup = _setup(buck_board(victim_gap=0.2), [])
    p.set_board(buck_board(victim_gap=0.2), setup)
    p.set_nets(["VIN", "GND", "SW", "VOUT", "SIG"])
    p.victim_list._add()
    v = p.setup.victims[0]
    ed = p.victim_list.editor
    assert _get_optional(ed.driver_low_ohm) is None and "low: 0.4 V" in ed.lbl_margin.text()
    ed.preset.setCurrentIndex(ed.preset.findData("open_drain"))
    assert v.driver_low_ohm == 20.0 and ed.driver_low_ohm.value() == 20.0
    ed.driver_low_ohm.setValue(8.0)
    assert v.driver_low_ohm == 8.0 and ed.preset.currentData() == "custom"
    p.close()
