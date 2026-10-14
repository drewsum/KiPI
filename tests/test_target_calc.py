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
