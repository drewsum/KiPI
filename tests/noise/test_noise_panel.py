# Portions Copyright (c) 2026 Tomachie LLC.
"""The Coupled Noise mode's setup panel and its place in the window (#69)."""

import os

import numpy as np
import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication, QRadioButton  # noqa: E402

from kipi.ac.model import CapModel  # noqa: E402
from kipi.dc.pi.model import PinRef  # noqa: E402
from noise_boards import buck_board, buck_setup  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from kipi.noise.gui.panel import NoisePanel
    p = NoisePanel()
    setup = buck_setup()
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"]),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    p.set_board(buck_board(), setup)
    p.set_nets(["VIN", "GND", "SW", "VOUT"])
    yield p
    p.close()


def test_without_an_aggressor_it_says_what_to_add(panel):
    assert not panel.btn_solve.isEnabled()
    assert "Add an aggressor" in panel.lbl_errors.text()


def test_a_new_aggressor_is_filled_from_its_vrm(panel):
    panel.agg_list._add()
    (a,) = panel.setup.aggressors
    assert a.vrm == "U1" and len(a.phases) == 1
    ph = a.phases[0]
    assert (ph.inductor, ph.switch_net, ph.input_caps, ph.output_caps) == \
        ("L1", "SW", ["C1"], ["C2"])
    ed = panel.agg_list.editor
    assert ed.vrm.currentData() == "U1"
    assert "Buck (auto)" in ed.lbl_op.text() and "12 V → 1.8 V" in ed.lbl_op.text()
    assert ed.inductance.value() == pytest.approx(1000.0)            # nH
    assert ed.high_side.value() == PinRef("U1", "VIN", ["1"])
    ticked = [ed.input_caps.item(i).data(Qt.UserRole) for i in range(ed.input_caps.count())
              if ed.input_caps.item(i).checkState() == Qt.Checked]
    assert ticked == ["C1"]
    assert panel.btn_solve.isEnabled(), panel.lbl_errors.text()
    assert panel.cmb_agg.currentData() == a.name


def test_edits_reach_the_setup(panel):
    panel.agg_list._add()
    a = panel.setup.aggressors[0]
    ed = panel.agg_list.editor
    ed.fsw.setValue(800)
    ed.rise.setValue(3.5)
    ed.coss.setValue(450)
    ed.package_l.setValue(0.3)
    assert a.fsw_hz == pytest.approx(800e3) and a.rise_s == pytest.approx(3.5e-9)
    assert a.phases[0].coss_f == pytest.approx(450e-12)
    assert a.phases[0].package_l_h == pytest.approx(0.3e-9)
    assert a.phases[0].qrr_c is None                     # not set: the default
    ed.qrr.setValue(12)
    assert a.phases[0].qrr_c == pytest.approx(12e-9)
    ed.qrr.setValue(0)                                   # a Schottky: none at all
    assert a.phases[0].qrr_c == 0.0
    ed.soft_zeta.setValue(0.3)
    ed.zeta.setValue(0.08)
    assert a.soft_edge_zeta == pytest.approx(0.3) and a.min_ring_zeta == pytest.approx(0.08)
    panel.spin_dead.setValue(35)
    assert panel.setup.noise.dead_time_ns == pytest.approx(35)
    ed.input_caps.item(0).setCheckState(Qt.Unchecked)
    assert a.phases[0].input_caps == []
    ed.inductance.setValue(0)
    assert "inductance" in panel.lbl_errors.text() and not panel.btn_solve.isEnabled()


def test_phases_are_added_and_removed(panel):
    panel.agg_list._add()
    a = panel.setup.aggressors[0]
    ed = panel.agg_list.editor
    ed._add_phase()
    assert len(a.phases) == 2 and ed.cmb_phase.count() == 2 and ed.phase == 1
    ed.dcr.setValue(2.0)
    assert a.phases[1].dcr_ohm == pytest.approx(2e-3) and a.phases[0].dcr_ohm == 0.0
    ed.cmb_phase.setCurrentIndex(0)
    assert ed.dcr.value() == 0.0
    ed._remove_phase()
    assert len(a.phases) == 1 and a.phases[0].dcr_ohm == pytest.approx(2e-3)


def test_the_capacitor_models_are_shared(panel):
    assert panel.cap_list.lst.count() == 2
    assert "10u" in panel.cap_list.lst.item(0).text()


def test_a_result_shows_its_parasitics(panel):
    from kipi.noise.solver import solve_noise
    panel.agg_list._add()
    r = solve_noise(panel.snapshot, panel.setup, panel.setup.aggressors[0].name)
    shown = []
    panel.result_selected.connect(shown.append)
    panel.show_result(r)
    assert shown == [r]
    assert "hot loop" in panel.txt_notes.text() and panel.btn_save.isEnabled()


@pytest.fixture
def window(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    yield w
    w.close()


def test_the_mode_shows_its_panel_and_waveforms_tab(window):
    def shown(widget):
        i = window.views.indexOf(widget)
        return (window.views.isTabVisible(i) if hasattr(window.views, "isTabVisible")
                else window.views.isTabEnabled(i))

    assert not shown(window.noise_plot)
    window.findChild(QRadioButton, "mode_noise").setChecked(True)
    assert window.mode == "noise"
    assert window.setup_stack.currentWidget() is window.noise_panel
    assert window.solve_dock.windowTitle() == "Coupled Noise setup"
    assert shown(window.noise_plot) and not shown(window.plot)
    assert "Coupled Noise" in window.act_open_results.toolTip()
    window.findChild(QRadioButton, "mode_ac").setChecked(True)
    assert not shown(window.noise_plot) and shown(window.plot)


def test_a_result_is_plotted(window):
    from kipi.noise.solver import solve_noise
    from noise_boards import buck_board as board
    snap = board()
    setup = buck_setup()
    window.noise_panel.set_board(snap, setup)
    window.noise_panel.agg_list._add()
    window.findChild(QRadioButton, "mode_noise").setChecked(True)
    from kipi.ac.model import Probe
    setup.probes = [Probe("LOAD", PinRef("U2", "VOUT", ["1"]), PinRef("U2", "GND", ["2"]))]
    setup.aggressors[0].ripple_probes = ["LOAD"]
    setup.noise.board_model = True
    r = solve_noise(snap, setup, setup.aggressors[0].name)
    window.noise_panel.show_result(r)
    assert window.noise_plot.result is r
    assert window.views.currentWidget() is window.noise_plot
    for key in ("sw", "il", "cin", "cout", "ripple", "probes"):
        window.noise_plot.set_view(key)
        window.noise_plot.chk_spectrum.setChecked(key == "il")
        assert window.noise_plot._curves, key


@pytest.fixture
def vpanel(app):
    from kipi.noise.gui.panel import NoisePanel
    p = NoisePanel()
    setup = buck_setup()
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"]),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    p.set_board(buck_board(victim_gap=0.2), setup)
    p.set_nets(["VIN", "GND", "SW", "VOUT", "SIG"])
    p.agg_list._add()
    p.setup.aggressors[0].phases[0].coss_f = 300e-12
    yield p
    p.close()


def test_a_new_victim_is_a_signal_net_with_its_ends(vpanel):
    vpanel.victim_list._add()
    (v,) = vpanel.setup.victims
    assert v.net == "SIG" and {v.driver.ref, v.receiver.ref} == {"U3", "U4"}
    ed = vpanel.victim_list.editor
    assert ed.preset.currentData() == "cmos" and "400 mV" in ed.lbl_margin.text()


def test_a_preset_fills_the_terminations_and_edits_make_it_custom(vpanel):
    vpanel.victim_list._add()
    v = vpanel.setup.victims[0]
    ed = vpanel.victim_list.editor
    ed.preset.setCurrentIndex(ed.preset.findData("parallel"))
    assert (v.preset, v.driver_ohm, v.termination_ohm) == ("parallel", 25.0, 50.0)
    assert ed.termination.value() == 50.0
    ed.driver_ohm.setValue(10.0)
    assert ed.preset.currentData() == "custom" and v.driver_ohm == 10.0
    ed.logic.setCurrentIndex(ed.logic.findData(1.8))
    assert v.logic_v == 1.8 and v.margin() == pytest.approx(0.18)


def test_victim_results_fill_the_table_and_plot(vpanel, window):
    from kipi.noise.solver import solve_noise
    vpanel.victim_list._add()
    r = solve_noise(vpanel.snapshot, vpanel.setup, vpanel.setup.aggressors[0].name)
    picked = []
    vpanel.victim_selected.connect(picked.append)
    vpanel.show_result(r)
    t = vpanel.tbl_victims
    assert t.rowCount() == 1 and t.item(0, 0).text() == "SIG"
    assert t.item(0, 5).text() == "PASS"
    assert picked == ["SIG"]                      # the worst victim is selected
    plot = window.noise_plot
    plot.set_result(r)
    plot.set_victim("SIG")
    assert plot.cmb_view.currentData() == "victim" and plot._curves
    assert "off the scale" in plot.lbl.text()


def test_the_victim_plot_shows_the_superposition_and_ticked_mechanisms(vpanel, window):
    from kipi.noise.solver import solve_noise
    vpanel.victim_list._add()
    r = solve_noise(vpanel.snapshot, vpanel.setup, vpanel.setup.aggressors[0].name)
    plot = window.noise_plot
    plot.set_result(r)
    plot.set_victim("SIG")
    assert not plot.victim_row.isHidden()
    labels = [c[0] for c in plot._curves]
    assert labels == [f"{r.victims[0].receiver} (receiver)",
                      f"{r.victims[0].driver} (driver)"]
    plot.chk_curves["capacitive"].setChecked(True)
    plot.chk_curves["driver"].setChecked(False)
    assert [c[0] for c in plot._curves] == [f"{r.victims[0].receiver} (receiver)",
                                            f"{r.victims[0].receiver} capacitive"]
    plot.chk_curves["total"].setChecked(False)
    plot.chk_curves["capacitive"].setChecked(False)
    assert plot._curves == [] and "Tick a curve" in plot.lbl.text()
    plot.set_view("sw")
    assert plot.victim_row.isHidden()


def test_a_curve_is_hidden_by_its_tickbox(vpanel, window):
    from kipi.ac.gui.plot import COLORS
    from kipi.noise.solver import solve_noise
    r = solve_noise(vpanel.snapshot, vpanel.setup, vpanel.setup.aggressors[0].name)
    plot = window.noise_plot
    plot.set_result(r)
    plot.set_view("ripple")                       # Vin and Vout ripple
    assert not plot.curve_scroll.isHidden() and len(plot.chk_shown) == 2
    assert [c[0] for c in plot._curves] == ["Vin ripple", "Vout ripple"]
    plot.chk_shown[0].setChecked(False)
    assert [c[0] for c in plot._curves] == ["Vout ripple"]
    assert plot.ax.get_lines()[0].get_color() == COLORS[1]   # keeps its colour
    # Hidden in the spectrum too, and still hidden after another view.
    plot.chk_spectrum.setChecked(True)
    assert [c[0] for c in plot._curves] == ["Vout ripple"]
    plot.set_view("sw")
    plot.set_view("ripple")
    assert not plot.chk_shown[0].isChecked() and len(plot._curves) == 1
    plot.chk_shown[1].setChecked(False)
    assert plot._curves == [] and "Tick a curve" in plot.lbl.text()
    plot.chk_shown[0].setChecked(True)
    assert [c[0] for c in plot._curves] == ["Vin ripple"]
    # The victim view has its own tickboxes.
    plot.set_view("victim")
    assert plot.curve_scroll.isHidden()


def test_the_bandwidth_limit_filters_every_view(vpanel, window):
    from kipi.noise.solver import solve_noise
    vpanel.victim_list._add()
    r = solve_noise(vpanel.snapshot, vpanel.setup, vpanel.setup.aggressors[0].name)
    plot = window.noise_plot
    plot.set_result(r)
    for key in ("sw", "il", "cin", "ripple", "victim", "states"):
        plot.set_view(key)
        plot.chk_bw.setChecked(False)
        full = [np.ptp(c[2]) for c in plot._curves]
        plot.chk_bw.setChecked(True)
        limited = [np.ptp(c[2]) for c in plot._curves]
        assert full and len(limited) == len(full), key
        assert all(b <= a * 1.001 for a, b in zip(full, limited)), key
        assert "20 MHz bandwidth limit" in plot.lbl.text()
    plot.set_view("sw")
    sw = r.waveforms.phases[0].switch_node
    assert np.ptp(plot._curves[0][2]) < np.ptp(sw.samples(r.waveforms.samples_per_period()))
    plot.chk_bw.setChecked(False)


def test_a_20mhz_limit_is_a_single_pole():
    from kipi.noise.waveforms import Spectrum, bandwidth_limited
    sp = Spectrum("x", "V", 1.0, np.ones(40, complex))
    out = bandwidth_limited(sp, 1e6, 20e6)
    assert out.mean == 1.0 and abs(out.c[19]) == pytest.approx(1 / np.sqrt(2))
    assert abs(out.c[0]) == pytest.approx(1.0, abs=2e-3)


def test_the_ac_probes_to_show_ripple_at_are_ticked(app):
    """#157: the aggressor lists the AC setup's probes; the ticked ones are kept."""
    from kipi.ac.model import Probe
    from kipi.noise.gui.panel import NoisePanel
    p = NoisePanel()
    setup = buck_setup()
    setup.probes = [Probe("LOAD", PinRef("U2", "VOUT", ["1"]), PinRef("U2", "GND", ["2"])),
                    Probe("VIN", PinRef("C1", "VIN", ["1"]), PinRef("C1", "GND", ["2"]))]
    p.set_board(buck_board(), setup)
    p.set_nets(["VIN", "GND", "SW", "VOUT"])
    p.agg_list._add()
    a = setup.aggressors[0]
    ed = p.agg_list.editor
    assert [ed.probes.item(i).data(Qt.UserRole) for i in range(ed.probes.count())] == \
        ["LOAD", "VIN"]
    assert a.ripple_probes == []
    ed.probes.item(0).setCheckState(Qt.Checked)
    assert a.ripple_probes == ["LOAD"]
    setup.probes = []                                   # the probe went: still shown, marked
    ed.load(a)
    assert "no such probe" in ed.probes.item(0).text()
    p.close()


def test_the_visual_settings_show_a_drawing(panel):
    """Every field whose meaning is easier to see than to read has a drawing under its
    tooltip (#160)."""
    from kipi.noise.gui.plot import NoisePlot
    agg = panel.agg_list.editor
    vic = panel.victim_list.editor
    plot = NoisePlot()
    drawn = {
        "converter": (agg.vrm, agg.topology, agg.vin, agg.load_a),
        "sw_period": (agg.fsw, agg.duty, panel.spin_periods),
        "board_model": (agg.probes, panel.chk_board),
        "hot_loop": (agg.rds_ls,),
        "victim_terms": (vic.net,),
        "noise_margin": (vic.logic, vic.margin, panel.cmb_scan_logic),
        "scan_radius": (panel.spin_radius, panel.spin_top, panel.chk_scan),
        "spectrum": (panel.spin_fmax, panel.spin_harm, plot.chk_spectrum),
        "interp": (panel.spin_fpd,),
        "coupling": (panel.cmb_coupling, *plot.chk_curves.values()),
        "bw_limit": (plot.chk_bw,),
    }
    for name, widgets in drawn.items():
        for w in widgets:
            assert f"/{name}-" in w.toolTip(), (name, w)
    plot.close()
