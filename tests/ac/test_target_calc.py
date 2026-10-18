"""The Target Impedance/Frequency Calculator: Z = V_ac / I_step up to f = 1 / (pi t_rise),
with t_rise = I_step / slew, and its result lands in the probe's target fields."""

import math
import os

import pytest

from kipi.ac.target import target_from_transient


def test_target_from_transient():
    t = target_from_transient(0.05, 10.0, 100e6)          # 50 mV, 10 A at 100 A/µs
    assert t.z_ohm == pytest.approx(5e-3)
    assert t.t_rise_s == pytest.approx(100e-9)
    assert t.f_hz == pytest.approx(1 / (math.pi * 100e-9))  # 3.18 MHz


@pytest.mark.parametrize("args", [(0, 10, 1e8), (0.05, 0, 1e8), (0.05, 10, 0), (-1, 1, 1)])
def test_no_target_without_all_three_inputs(args):
    assert target_from_transient(*args) is None


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_dialog_computes_and_remembers_inputs(app, monkeypatch):
    from qtpy.QtWidgets import QDialogButtonBox
    from kipi.ac.gui.target_calc import TargetCalculator
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    d = TargetCalculator(probe_name="U1")
    ok = d.buttons.button(QDialogButtonBox.Ok)
    assert not ok.isEnabled() and d.lbl_z.text() == "–"
    d.v_ac.setValue(50)
    d.i_step.setValue(10)
    d.slew.setValue(100)
    assert ok.isEnabled()
    assert "5 mΩ" in d.lbl_z.text() and "100 ns" in d.lbl_t.text()
    assert "3.183 MHz" in d.lbl_f.text()
    d.accept()
    assert TargetCalculator.last == (50, 10, 100)
    again = TargetCalculator()
    assert again.target().z_ohm == pytest.approx(5e-3)


def test_calculator_fills_the_probe_target(app, monkeypatch):
    from kipi.ac.gui import target_calc
    from kipi.ac.gui.panel import ACPanel
    from kipi.ac.model import Probe
    from kipi.dc.pi.model import PinRef
    panel = ACPanel()
    probe = Probe("U1", PinRef("U1", "+1V0", ["1"]), PinRef("U1", "GND", ["2"]))
    panel.setup.probes.append(probe)
    panel.setEnabled(True)                                # as set_board does
    panel.probe_list.reload()
    panel.probe_list.lst.setCurrentRow(0)
    ed = panel.probe_list.editor
    assert ed.obj is probe and ed.btn_calc.isEnabled()

    def run(dlg):
        dlg.v_ac.setValue(30)                             # 30 mV, 15 A at 50 A/µs
        dlg.i_step.setValue(15)
        dlg.slew.setValue(50)
        dlg.accept()
        return dlg.result()
    monkeypatch.setattr(target_calc.TargetCalculator, "last", (0.0, 0.0, 0.0))
    monkeypatch.setattr(target_calc.TargetCalculator, "exec", run)
    ed.btn_calc.click()
    assert probe.target_ohm == pytest.approx(2e-3)
    assert probe.target_hz == pytest.approx(1 / (math.pi * 0.3e-6), rel=1e-4)


# --- Copy tolerance from DC load (#87) --------------------------------------------------------

def _setup():
    from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm
    s = PISetup()
    s.vrms.append(Vrm("VR1", PinRef("U9", "+1V0_VRM"), 1.0))
    s.vrms.append(Vrm("VR2", PinRef("U8", "+3V3"), 3.3))
    s.links.append(DcLink("FB1", PinRef("FB1", "+1V0_VRM"), PinRef("FB1", "+1V0"), 0.0))
    s.loads += [Load("FPGA core", PinRef("U2", "+1V0"), 5.0, tolerance_pct=3),
                Load("SoC", PinRef("U1", "+1V0"), 2.0, tolerance_pct=5),
                Load("No tolerance", PinRef("U3", "+1V0"), 1.0),
                Load("IO", PinRef("U1", "+3V3"), 1.0, tolerance_pct=10)]
    return s


def test_load_tolerances_on_the_probes_rail_own_part_first():
    from kipi.ac.target import load_tolerances
    from kipi.dc.pi.model import PinRef
    ts = load_tolerances(_setup(), PinRef("U1", "+1V0", ["1"]))
    assert [t.name for t in ts] == ["SoC", "FPGA core"]      # through the link to VR1
    assert ts[0].v_ac_v == pytest.approx(0.05) and ts[0].nominal_v == 1.0
    assert ts[1].label() == "FPGA core: ±3 % of 1 V"
    assert [t.name for t in load_tolerances(_setup())] == ["FPGA core", "SoC", "IO"]
    assert load_tolerances(_setup(), PinRef("U5", "+5V")) == []


def test_dialog_copies_a_load_tolerance(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import LoadTolerance
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    assert not TargetCalculator().btn_copy.isEnabled()        # no loads to copy from
    d = TargetCalculator(loads=[LoadTolerance("SoC", 5, 1.2)])
    assert d.btn_copy.isEnabled() and d.btn_copy.menu() is None
    d.btn_copy.click()
    assert d.v_ac.value() == pytest.approx(60)                # ±5 % of 1.2 V
    two = TargetCalculator(loads=[LoadTolerance("A", 5, 1.0), LoadTolerance("B", 2, 3.3)])
    actions = two.btn_copy.menu().actions()
    assert [a.text() for a in actions] == ["A: ±5 % of 1 V", "B: ±2 % of 3.3 V"]
    actions[1].trigger()
    assert two.v_ac.value() == pytest.approx(66)


def test_probe_editor_offers_the_rails_loads(app, monkeypatch):
    from kipi.ac.gui import target_calc
    from kipi.ac.gui.panel import ACPanel
    from kipi.ac.model import Probe
    from kipi.dc.pi.model import PinRef
    panel = ACPanel()
    panel.setup = _setup()
    panel.setup.probes.append(Probe("U1", PinRef("U1", "+1V0", ["1"]), PinRef("U1", "GND")))
    panel.setEnabled(True)
    panel.probe_list.reload()
    panel.probe_list.lst.setCurrentRow(0)
    seen = []
    monkeypatch.setattr(target_calc.TargetCalculator, "exec",
                        lambda dlg: seen.append([[t.name for t in dlg.loads],
                                                 [c.name for c in dlg.currents]]) or 0)
    panel.probe_list.editor.btn_calc.click()
    assert seen == [[["SoC", "FPGA core"], ["SoC", "FPGA core", "No tolerance"]]]


# --- Copy current from DC load ------------------------------------------------------------

def test_load_currents_on_the_probes_rail_own_part_first():
    from kipi.ac.target import load_currents
    from kipi.dc.pi.model import PinRef
    cs = load_currents(_setup(), PinRef("U1", "+1V0", ["1"]))
    assert [(c.name, c.current_a) for c in cs] == [("SoC", 2.0), ("FPGA core", 5.0),
                                                   ("No tolerance", 1.0)]
    assert cs[0].label() == "SoC: 2 A"
    assert len(load_currents(_setup())) == 4
    assert load_currents(_setup(), PinRef("U5", "+5V")) == []


def test_dialog_copies_a_load_current(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import LoadCurrent
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    assert not TargetCalculator().btn_current.isEnabled()
    d = TargetCalculator(currents=[LoadCurrent("SoC", 2.5)])
    assert d.btn_current.menu() is None
    d.btn_current.click()
    assert d.i_step.value() == pytest.approx(2.5)
    two = TargetCalculator(currents=[LoadCurrent("A", 1.0), LoadCurrent("B", 4.0)])
    actions = two.btn_current.menu().actions()
    assert [a.text() for a in actions] == ["A: 1 A", "B: 4 A"]
    actions[1].trigger()
    assert two.i_step.value() == pytest.approx(4.0)


def test_slew_rate_starts_at_10_a_per_us(app):
    from kipi.ac.gui import target_calc
    assert target_calc.TargetCalculator.last == (0.0, 0.0, 10.0)
    assert target_calc.TargetCalculator().slew.value() == 10.0
