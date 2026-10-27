"""The estimated transient response at a probe (#166): the load step through the
open-loop PDN impedance with the VRM's loop closed at its bandwidth, in the results table,
the Transient tab (with a 20 MHz bandwidth limit) and the report."""

import math
import os

import numpy as np
import pytest

from kipi.ac.model import Probe
from kipi.ac.solver.sweep import ACResult, ProbeCurve
from kipi.ac.transient import bandwidth_limited, closed_loop, transient, transients
from kipi.dc.pi.model import PinRef, PISetup

F = np.logspace(3, math.log10(300e6), 1500)
W = 2 * np.pi * F


def _rlc(c=100e-6, r=2e-3, l=1e-9):
    return r + 1j * W * l + 1 / (1j * W * c)


def test_a_capacitor_bank_dips_by_about_di_over_wc_and_recovers():
    c = 100e-6
    tr = transient(F, _rlc(c), 50e3, 1.0, 1e6)                # 1 A at 1 A/µs
    rule = 1.0 / (2 * math.pi * 50e3 * c)                     # ΔI / (2π f_bw C)
    assert 0.5 * rule < tr.undershoot_v < 1.2 * rule
    assert tr.t_undershoot_s < tr.t_release_s < tr.t_overshoot_s
    assert tr.overshoot_v == pytest.approx(tr.undershoot_v, rel=0.02)   # the release mirrors
    assert abs(tr.v[tr.t < tr.t_release_s][-1]) < 0.02 * tr.undershoot_v   # settled
    assert abs(tr.v[-1]) < 1e-3 * tr.undershoot_v                  # back to the DC value
    assert tr.i.max() == pytest.approx(1.0) and tr.notes == []


def test_more_capacitance_or_bandwidth_dips_less():
    base = transient(F, _rlc(100e-6), 50e3, 1.0, 1e6).undershoot_v
    assert transient(F, _rlc(400e-6), 50e3, 1.0, 1e6).undershoot_v < base / 2
    assert transient(F, _rlc(100e-6), 200e3, 1.0, 1e6).undershoot_v < base / 2


def test_the_loop_holds_the_rail_below_its_bandwidth_only():
    z = _rlc()
    zc = closed_loop(F, z, 50e3, np.array([0.0, 1e3, 5e6]))
    w = 2 * np.pi * np.array([1e3, 5e6])
    zo = np.abs(2e-3 + 1j * w * 1e-9 + 1 / (1j * w * 100e-6))
    assert zc[0] == 0                                     # the integrator: no DC error
    assert abs(zc[1]) < 0.05 * zo[0]
    assert abs(zc[2]) == pytest.approx(zo[1], rel=0.06)   # the PDN's own, well above


def test_a_fast_edge_into_inductance_spikes_and_the_20_mhz_limit_shrinks_it():
    z = _rlc(l=10e-9)
    tr = transient(F, z, 50e3, 1.0, 100e6)                # 10 ns edge: L di/dt = 1 V
    assert 0.95 < tr.undershoot_v < 1.15
    assert tr.notes == []
    lim = bandwidth_limited(tr, 20e6)
    assert lim.undershoot_v < 0.8 * tr.undershoot_v           # 1 - e^(-10 ns / 8 ns)
    assert tr.undershoot_v > 0.95                         # the original is kept
    slow = transient(F, _rlc(), 50e3, 1.0, 1e6)
    assert bandwidth_limited(slow).undershoot_v == pytest.approx(slow.undershoot_v, rel=0.01)


def test_the_esl_spike_is_l_di_dt_over_the_edge():
    """#200: 5 A at 100 A/µs into 1 nH: a 50 ns plateau of L·di/dt = 0.1 V, on top of
    R·i and the capacitors' droop, then back to the droop once the current is flat."""
    tr = transient(F, _rlc(l=1e-9), 50e3, 5.0, 100e6)
    f_edge = 1 / (math.pi * 50e-9)
    assert tr.edge_l_h == pytest.approx(1e-9 - 1 / ((2 * math.pi * f_edge) ** 2 * 100e-6),
                                        rel=0.01)
    assert tr.spike_v == pytest.approx(0.1, rel=0.01)
    ramp = (tr.t > tr.t_step_s + 15e-9) & (tr.t < tr.t_step_s + 35e-9)
    after = (tr.t > tr.t_step_s + 70e-9) & (tr.t < tr.t_step_s + 90e-9)
    # On the ramp: L·di/dt + R·i (5 to 7.5 mV) + the droop (under 2 mV).
    assert -tr.v[ramp] == pytest.approx(0.1 + 0.007, abs=0.006)
    assert -tr.v[after] == pytest.approx(0.010, abs=0.004)       # 5 A × 2 mΩ, and droop
    assert tr.spike_v < tr.edge_v < tr.spike_v + 0.015
    lo, hi = tr.edge_window()
    assert lo < tr.t_step_s < tr.t_step_s + tr.t_rise_s < hi
    # A 5 µs edge sees the bank's capacitance (its resonance is 0.5 MHz): no spike.
    slow = transient(F, _rlc(l=1e-9), 50e3, 5.0, 1e6)
    assert slow.edge_l_h is None and slow.spike_v is None and slow.edge_v > 0


def test_no_estimate_without_a_step_slew_or_bandwidth():
    z = _rlc()
    assert transient(F, z, None, 1.0, 1e6) is None
    assert transient(F, z, 50e3, None, 1e6) is None
    assert transient(F, z, 50e3, 1.0, 0.0) is None


def test_an_edge_faster_than_the_sweep_is_noted():
    tr = transient(F[F < 1e7], _rlc()[F < 1e7], 50e3, 1.0, 1e9)     # 1 ns, sweep to 10 MHz
    assert any("faster than" in n for n in tr.notes)


def _result():
    return ACResult(rail="VRM1", rail_nets=["+1V0"], ground="GND", freqs=F,
                    probes=[ProbeCurve("U1", _rlc(), 0.03, 1e6, step_a=1.0, slew_a_per_us=1.0),
                            ProbeCurve("U2", _rlc())], field_freqs=F[::100],
                    bandwidth_hz=50e3)


def test_a_probe_curve_carries_its_step_and_allowed_swing():
    r = _result()
    trs = transients(r)
    assert list(trs) == ["U1"]                           # U2 has no load step
    assert r.probes[0].allowed_v() == pytest.approx(0.03)     # 30 mΩ × 1 A
    assert r.probes[1].allowed_v() is None
    r.bandwidth_hz = None
    assert transients(r) == {}


def test_the_load_step_is_saved_with_the_setup():
    s = PISetup()
    s.probes.append(Probe("U1", PinRef("U1", "+1V0", ["1"]), PinRef("U1", "GND", ["2"]),
                          step_a=2.0, slew_a_per_us=5.0))
    back = PISetup.from_dict(s.to_dict()).probes[0]
    assert (back.step_a, back.slew_a_per_us) == (2.0, 5.0)
    old = s.to_dict()
    for k in ("step_a", "slew_a_per_us"):
        del old["probes"][0][k]                           # saved before #166
    assert PISetup.from_dict(old).probes[0].step_a is None


# --- GUI -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    pytest.importorskip("matplotlib")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_transient_tab_plots_and_limits_bandwidth(app):
    from kipi.ac.gui.transient_plot import TransientPlot
    p = TransientPlot()
    p.set_result(_result())
    full = p.transients["U1"].undershoot_v
    assert len(p.ax_v.lines) > 1 and "estimate" in p.lbl.text()
    p.chk_bw.setChecked(True)
    assert "20 MHz" in p.ax_i.get_title()
    assert p.transients["U1"].undershoot_v <= full
    p.chk_bw.setChecked(False)
    assert p.transients["U1"].undershoot_v == full
    # U1's 1 µs edge sees the bank's capacitance; zoomed, the plot shows just its edge.
    assert "no ESL spike" in p.lbl.text()
    tr = p.transients["U1"]
    p.chk_edge.setChecked(True)
    k = p._scale[0]
    lo, hi = p.ax_v.get_xlim()
    assert lo / k <= tr.t_step_s and (tr.t_step_s + tr.t_rise_s) < hi / k < 0.2 * tr.t[-1]
    p.chk_edge.setChecked(False)
    assert p.ax_v.get_xlim()[1] / p._scale[0] == pytest.approx(tr.t[-1])
    fast = _result()
    fast.probes[0].slew_a_per_us = 100.0
    p.set_result(fast)
    assert "ESL spike L·di/dt = 1 nH × 100 A/µs = 100 mV" in p.lbl.text()
    r = _result()
    r.probes[0].step_a = None
    p.set_result(r)
    assert p.transients == {} and "load step" in p.lbl.text()
    p.clear()
    assert p.result is None


def test_the_results_table_shows_the_swing(app):
    from kipi.ac.gui.panel import ACPanel
    panel = ACPanel()
    r = _result()
    panel._display(r)
    t = panel.tbl_probes
    headers = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]
    assert headers[5:7] == ["Undershoot (mV)", "Overshoot (mV)"]
    tr = transients(r)["U1"]
    assert float(t.item(0, 5).text()) == pytest.approx(tr.undershoot_v * 1e3, rel=1e-3)
    assert t.item(1, 5).text() == ""


def test_the_probe_editor_saves_the_load_step(app):
    from kipi.ac.gui.panel import ACPanel
    panel = ACPanel()
    probe = Probe("U1", PinRef("U1", "+1V0", ["1"]), PinRef("U1", "GND", ["2"]))
    panel.setup.probes.append(probe)
    panel.setEnabled(True)
    panel.probe_list.reload()
    panel.probe_list.lst.setCurrentRow(0)
    ed = panel.probe_list.editor
    ed.step.setValue(3.0)
    ed.slew.setValue(20.0)
    assert (probe.step_a, probe.slew_a_per_us) == (3.0, 20.0)
    ed.step.setValue(0)
    assert probe.step_a is None


def test_the_main_window_has_a_transient_tab_in_ac_mode(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    try:
        i = w.views.indexOf(w.transient_plot)
        assert w.views.tabText(i) == "Transient"
        w._show_ac_result(_result())
        assert "U1" in w.transient_plot.transients
        w._show_ac_result(None)
        assert w.transient_plot.result is None
    finally:
        w.close()


# --- Report --------------------------------------------------------------------------------

def test_the_report_has_a_transient_section(tmp_path):
    pytest.importorskip("plotly")
    from ac_boards import plane_pair, plane_setup
    from kipi.ac.html_report import write_ac_report
    from kipi.ac.solver import solve_ac
    snap = plane_pair()
    setup = plane_setup(snap, f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20,
                        field_points_per_decade=2)
    setup.vrms[0].bandwidth_khz = 200.0
    setup.probes[0].step_a, setup.probes[0].slew_a_per_us = 1.0, 10.0
    result = solve_ac(snap, setup, "VRM1", "GND")
    assert transients(result)
    text = write_ac_report(tmp_path / "r.html", result, snap, setup).read_text("utf-8")
    assert "id='transient'" in text and 'id="trplot"' in text and "Undershoot" in text
    assert "ESL spike" in text
    result.probes[0].step_a = None
    text = write_ac_report(tmp_path / "s.html", result, snap, setup).read_text("utf-8")
    assert "id='transient'" not in text
