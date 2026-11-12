# Portions Copyright (c) 2026 Tomachie LLC.
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


def test_probe_from_a_dc_load_takes_its_pins(app):
    from kipi.ac.gui.panel import ACPanel
    from kipi.ac.model import Probe
    from kipi.dc.pi.model import Load, PinRef
    panel = ACPanel()
    panel.setup.loads.append(Load("SoC", PinRef("U1", "+1V0", ["1", "3"]), 5.0,
                                  ground=PinRef("U1", "GND", ["2"])))
    panel.setup.probes.append(Probe("SoC", PinRef("U2", "+1V0"), PinRef("U2", "GND")))
    panel.setEnabled(True)
    probe = panel.probe_from_load(panel.setup.loads[0])
    assert probe.name == "SoC 1"                      # the name is taken
    assert probe.rail == PinRef("U1", "+1V0", ["1", "3"])
    assert probe.ground == PinRef("U1", "GND", ["2"])
    assert probe.rail is not panel.setup.loads[0].supply
    panel.probe_list.add(probe)
    assert panel.setup.probes[-1] is probe and panel.probe_list.current() is probe


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
    assert (probe.step_a, probe.slew_a_per_us) == (15, 50)    # its load step too (#166)
    assert (ed.step.value(), ed.slew.value()) == (15, 50)
    # Opened again, the calculator starts from the probe's own load step.
    monkeypatch.setattr(target_calc.TargetCalculator, "last", (0.0, 1.0, 2.0))
    seen = []
    monkeypatch.setattr(target_calc.TargetCalculator, "exec",
                        lambda dlg: seen.append((dlg.i_step.value(), dlg.slew.value())) or 0)
    ed.btn_calc.click()
    assert seen == [(15, 50)]


# --- Copy tolerance and current from the probe's DC load (#87) -----------------------------

def _setup():
    from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm
    s = PISetup()
    s.vrms.append(Vrm("VR1", PinRef("U9", "+1V0_VRM"), 1.0))
    s.vrms.append(Vrm("VR2", PinRef("U8", "+3V3"), 3.3))
    s.links.append(DcLink("FB1", PinRef("FB1", "+1V0_VRM"), PinRef("FB1", "+1V0"), 0.0))
    s.loads += [Load("FPGA core", PinRef("U2", "+1V0"), 5.0, tol_min=-3, tol_max=3),
                Load("SoC", PinRef("U1", "+1V0"), 2.0, tol_min=-5, tol_max=5),
                Load("No tolerance", PinRef("U3", "+1V0"), 1.0),
                Load("IO", PinRef("U1", "+3V3"), 1.0, tol_min=-10, tol_max=10)]
    return s


def test_probe_loads_are_the_loads_on_the_probes_part():
    from kipi.ac.target import probe_loads
    from kipi.dc.pi.model import PinRef
    ls = probe_loads(_setup(), PinRef("U1", "+1V0", ["1"]), PinRef("U1", "GND"))
    assert [(p.name, p.current_a, p.exact) for p in ls] == [("SoC", 2.0, False),
                                                            ("IO", 1.0, False)]
    assert ls[0].tolerance.v_ac_v == pytest.approx(0.05)    # 5 % of VR1, through the link
    assert ls[0].tolerance.label() == "SoC: ±5 % of 1 V"
    assert ls[1].tolerance.v_ac_v == pytest.approx(0.33)
    assert probe_loads(_setup(), PinRef("U5", "+5V")) == []
    assert probe_loads(_setup(), None) == []


def test_a_load_on_the_probes_pins_comes_first():
    from kipi.ac.target import probe_loads
    from kipi.dc.pi.model import PinRef
    s = _setup()
    io = s.loads[3]
    io.supply, io.ground = PinRef("U1", "+1V0", ["2", "1"]), PinRef("U1", "GND", ["3"])
    ls = probe_loads(s, PinRef("U1", "+1V0", ["1", "2"]), PinRef("U1", "GND", ["3"]))
    assert [(p.name, p.exact) for p in ls] == [("IO", True), ("SoC", False)]
    ls = probe_loads(s, PinRef("U1", "+1V0", ["1", "2"]), PinRef("U1", "GND", ["4"]))
    assert [(p.name, p.exact) for p in ls] == [("SoC", False), ("IO", False)]
    # Ground pins on another part: not this probe's load
    assert [p.name for p in probe_loads(s, PinRef("U1", "+1V0"), PinRef("J1", "GND"))]         == ["SoC"]


def test_an_uneven_tolerance_gives_its_tighter_side():
    from kipi.ac.target import probe_loads
    from kipi.dc.pi.model import PinRef
    s = _setup()
    soc = s.loads[1]
    soc.tol_min, soc.tol_max = -2.0, 5.0                     # -2 % of 1 V is tighter
    t = probe_loads(s, PinRef("U1", "+1V0"))[0].tolerance
    assert t.v_ac_v == pytest.approx(0.02) and t.label() == "SoC: -2 / +5 % of 1 V"
    soc.tol_min, soc.tol_max, soc.tol_pct = 0.97, 1.01, False  # volts: +10 mV is tighter
    t = probe_loads(s, PinRef("U1", "+1V0"))[0].tolerance
    assert t.v_ac_v == pytest.approx(0.01) and t.label() == "SoC: 0.97 to 1.01 V (±10 mV)"
    soc.tol_min, soc.tol_max = 1.05, None                     # 1 V is outside it: nothing
    assert probe_loads(s, PinRef("U1", "+1V0"))[0].tolerance is None


def test_dialog_copies_the_probes_load(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import LoadTolerance, ProbeLoad
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    none = TargetCalculator()
    assert not none.btn_copy.isEnabled() and not none.btn_current.isEnabled()
    assert not none.load.isEnabled()
    d = TargetCalculator(loads=[ProbeLoad("SoC", LoadTolerance("SoC", "±5 %", 1.2, 0.06),
                                          2.5)])
    assert not d.load.isEnabled() and d.load.currentText() == "SoC"
    d.btn_copy.click()
    d.btn_current.click()
    assert d.v_ac.value() == pytest.approx(60)                # ±5 % of 1.2 V
    assert d.i_step.value() == pytest.approx(2.5)


def test_dialog_picks_among_the_parts_loads(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import LoadTolerance, ProbeLoad
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    a = ProbeLoad("A", LoadTolerance("A", "±5 %", 1.0, 0.05), 1.0)
    b = ProbeLoad("B", None, 4.0)
    d = TargetCalculator(loads=[a, b])
    assert d.load.isEnabled() and d.btn_copy.menu() is None
    d.load.setCurrentIndex(1)
    assert not d.btn_copy.isEnabled() and "B has no tolerance" in d.btn_copy.toolTip()
    d.btn_current.click()
    assert d.i_step.value() == pytest.approx(4.0)
    d.load.setCurrentIndex(0)
    d.btn_copy.click()
    assert d.v_ac.value() == pytest.approx(50)
    # One load on the probe's own pins: that one, no choice
    exact = TargetCalculator(loads=[ProbeLoad("B", None, 4.0, exact=True), a])
    assert not exact.load.isEnabled() and exact.selected_load().name == "B"


def test_vrm_accuracies_on_the_probes_rail():
    from kipi.ac.target import vrm_accuracies
    from kipi.dc.pi.model import PinRef
    s = _setup()
    vr1, vr2 = s.vrms
    vr1.kind = vr2.kind = "smps"
    vr1.acc_min, vr1.acc_max = -1.0, 2.0                      # +2 % of 1 V is larger
    vr2.acc_min, vr2.acc_max, vr2.acc_pct = 3.25, 3.31, False  # -50 mV of 3.3 V
    accs = vrm_accuracies(s, PinRef("U1", "+1V0"))             # through the link to VR1
    assert [(a.name, a.label()) for a in accs] == [("VR1", "VR1: -1 / +2 % (±20 mV)")]
    assert accs[0].v == pytest.approx(0.02)
    assert [a.v for a in vrm_accuracies(s)] == [pytest.approx(0.02), pytest.approx(0.05)]
    vr2.kind = "source"                                        # sources have none
    assert [a.name for a in vrm_accuracies(s)] == ["VR1"]
    vr1.acc_min = vr1.acc_max = None
    assert vrm_accuracies(s, PinRef("U1", "+1V0")) == []


def test_dialog_subtracts_a_vrm_accuracy(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import VrmAccuracy
    monkeypatch.setattr(TargetCalculator, "last", (50.0, 0.0, 0.0))
    assert not TargetCalculator().btn_accuracy.isEnabled()
    d = TargetCalculator(accuracies=[VrmAccuracy("VR1", "±1 %", 0.012)])
    d.btn_accuracy.click()
    assert d.v_ac.value() == pytest.approx(38)                 # 50 mV - 12 mV
    for _ in range(4):
        d.btn_accuracy.click()
    assert d.v_ac.value() == 0                                 # never below zero


def test_each_side_of_the_accuracy_comes_off_its_own_side_of_the_tolerance(app, monkeypatch):
    # #172: a load allowed -5 / +3 % of 1 V behind a VRM that may sit -3 / +1 % off it has
    # 50 - 30 = 20 mV left below and 30 - 10 = 20 mV above, not 30 - 30 = 0 mV.
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import LoadDrop, ProbeLoad, probe_loads, vrm_accuracies
    from kipi.dc.pi.model import PinRef
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    s = _setup()
    soc, vr1 = s.loads[1], s.vrms[0]
    soc.tol_min, soc.tol_max = -5.0, 3.0
    vr1.kind, vr1.acc_min, vr1.acc_max = "ldo", -3.0, 1.0
    rail = PinRef("U1", "+1V0")
    load, acc = probe_loads(s, rail)[0], vrm_accuracies(s, rail)[0]
    assert load.tolerance.room() == (pytest.approx(0.05), pytest.approx(0.03))
    assert acc.sides() == (pytest.approx(0.03), pytest.approx(0.01))
    d = TargetCalculator(loads=[load], accuracies=[acc])
    d.btn_copy.click()
    assert d.v_ac.value() == pytest.approx(30)
    d.btn_accuracy.click()
    assert d.v_ac.value() == pytest.approx(20)
    # A drop uses up room below (20 -> 15 mV), a boost room above (20 -> 16 mV)
    d.subtract_drop(ProbeLoad("SoC", None, 2.0, drop=LoadDrop("SoC", 1.0, 0.995, 0.005,
                                                              "drop")))
    assert d.v_ac.value() == pytest.approx(15)
    d.btn_copy.click()
    d.btn_accuracy.click()
    d.subtract_drop(ProbeLoad("SoC", None, 2.0, drop=LoadDrop("SoC", 1.0, 1.004, 0.004,
                                                              "boost")))
    assert d.v_ac.value() == pytest.approx(16)
    # Typed in: the room is unknown, so the larger side comes off the value
    d.v_ac.setValue(40)
    d.btn_accuracy.click()
    assert d.v_ac.value() == pytest.approx(10)                # 40 - 30 mV


def test_an_accuracy_with_one_end_uses_up_one_side():
    from kipi.dc.pi.model import PinRef, Vrm
    v = Vrm("VR1", PinRef("U9", "+1V0"), 1.0, kind="smps")
    v.acc_min, v.acc_max = -2.0, None
    assert v.accuracy_sides() == (pytest.approx(0.02), 0.0)
    v.acc_min, v.acc_max, v.acc_pct = 0.99, 1.03, False       # volts
    assert v.accuracy_sides() == (pytest.approx(0.01), pytest.approx(0.03))


# --- Subtract the solved DC drop at the load's pins ----------------------------------------

def _tree(*loads):
    """A power-tree result holding ``(name, nominal, v_effective)`` load reports."""
    from kipi.dc.pi.results import LoadReport, TreeResult
    return TreeResult(loads=[
        LoadReport(name=n, group="+1V0", current=1.0, nominal=nom, v_supply=v, v_supply_min=v,
                   v_ground=0.0, v_effective=v, drop=nom - v, tolerance="", passed=True)
        for n, nom, v in loads])


def test_load_drop_is_the_solved_effective_voltage_against_nominal():
    from kipi.ac.target import load_drop
    d = load_drop(_tree(("SoC", 1.0, 0.973)), "SoC")
    assert (d.v, d.kind) == (pytest.approx(0.027), "drop")
    assert d.nominal == 1.0 and d.v_effective == pytest.approx(0.973)
    assert d.label() == "SoC: 0.973 V of 1 V (27 mV drop)"
    boost = load_drop(_tree(("SoC", 1.0, 1.004)), "SoC")       # a remote-sensed VRM
    assert (boost.v, boost.kind) == (pytest.approx(0.004), "boost")
    assert load_drop(_tree(("SoC", 1.0, 0.97)), "IO") is None  # not in that solve
    assert load_drop(_tree(("SoC", 1.0, float("nan"))), "SoC") is None   # disconnected (#88)
    assert load_drop(_tree(("SoC", 0.0, 0.0)), "SoC") is None   # no VRM drives it
    assert load_drop(None, "SoC") is None                       # no solve yet


def test_probe_loads_carry_their_drop_once_solved():
    from kipi.ac.target import probe_loads
    from kipi.dc.pi.model import PinRef
    s, rail = _setup(), PinRef("U1", "+1V0")
    assert probe_loads(s, rail)[0].drop is None                 # no DC result
    ls = probe_loads(s, rail, None, _tree(("SoC", 1.0, 0.95)))
    assert ls[0].drop.v == pytest.approx(0.05) and ls[1].drop is None


def test_dialog_subtracts_the_dc_drop(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import LoadDrop, ProbeLoad
    monkeypatch.setattr(TargetCalculator, "last", (50.0, 0.0, 0.0))
    unsolved = TargetCalculator(loads=[ProbeLoad("SoC", None, 2.0)])
    assert not unsolved.btn_drop.isEnabled()
    assert "solve the power tree" in unsolved.btn_drop.toolTip()
    assert not TargetCalculator().btn_drop.isEnabled()           # no load at all
    d = TargetCalculator(loads=[ProbeLoad("SoC", None, 2.0,
                                          drop=LoadDrop("SoC", 1.0, 0.988, 0.012, "drop"))])
    assert d.btn_drop.isEnabled() and "12 mV drop at SoC's pins" in d.btn_drop.toolTip()
    d.btn_drop.click()
    assert d.v_ac.value() == pytest.approx(38)                   # 50 mV - 12 mV
    for _ in range(4):
        d.btn_drop.click()
    assert d.v_ac.value() == 0                                   # never below zero
    # Solved, but sitting exactly at the target voltage: nothing to take off
    flat = TargetCalculator(loads=[ProbeLoad("SoC", None, 2.0,
                                             drop=LoadDrop("SoC", 1.0, 1.0, 0.0, "drop"))])
    assert not flat.btn_drop.isEnabled()
    assert "sits at its VRM's target voltage" in flat.btn_drop.toolTip()


def test_the_calculator_gets_the_panels_dc_result(app, monkeypatch):
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
                        lambda dlg: seen.append([p.drop for p in dlg.loads]) or 0)
    panel.probe_list.editor.btn_calc.click()
    assert seen[-1] == [None, None]                   # no DC solve yet
    panel.set_dc_result(_tree(("SoC", 1.0, 0.94)))
    panel.probe_list.editor.btn_calc.click()
    assert seen[-1][0].v == pytest.approx(0.06) and seen[-1][1] is None
    panel.set_board(panel.snapshot, panel.setup)   # a new board forgets it
    assert panel.dc_result is None


def test_probe_editor_offers_the_parts_loads(app, monkeypatch):
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
                        lambda dlg: seen.append([[p.name for p in dlg.loads],
                                                 [a.name for a in dlg.accuracies]]) or 0)
    panel.setup.vrms[0].kind = "ldo"
    panel.setup.vrms[0].acc_min, panel.setup.vrms[0].acc_max = -1.0, 1.0
    panel.probe_list.editor.btn_calc.click()
    assert seen == [[["SoC", "IO"], ["VR1"]]]


def test_slew_rate_starts_at_10_a_per_us(app):
    from kipi.ac.gui import target_calc
    assert target_calc.TargetCalculator.last == (0.0, 0.0, 10.0)
    assert target_calc.TargetCalculator().slew.value() == 10.0


# --- LDO and switcher inputs as loads (#117) -----------------------------------------------

def _regulators():
    """A 5 V source feeding an LDO (VR3, 4.5 to 5.5 V in) and a two-phase switcher (VR4)."""
    from kipi.dc.pi.model import PinRef, PISetup, Vrm, VrmPhase
    s = PISetup()
    s.vrms.append(Vrm("VR5", PinRef("U7", "+5V"), 5.0))
    s.vrms.append(Vrm("VR3", PinRef("U4", "+1V8", ["3"]), 1.8, kind="ldo",
                      input=PinRef("U4", "+5V", ["1"]), ground=PinRef("U4", "GND", ["2"]),
                      vin_min=4.5, vin_max=5.5))
    s.vrms.append(Vrm("VR4", PinRef("U6", "+0V9", ["5"]), 0.9, kind="smps",
                      input=PinRef("U6", "+5V", ["1"]), ground=PinRef("U6", "GND", ["9"]),
                      phases=[VrmPhase(PinRef("U6", "+0V9", ["6"]), PinRef("U6", "+5V", ["2"]),
                                       PinRef("U6", "GND", ["9"]))]))
    return s


def _vrm_tree():
    """A power-tree result with VR3's and VR4's inputs solved."""
    from kipi.dc.pi.results import PhaseReport, TreeResult, VrmReport
    vr3 = VrmReport("VR3", "ldo", "+1V8", 1.8, 1.8, None, 0.5, 4.97, 0.501, 0.9, 2.49)
    vr4 = VrmReport("VR4", "smps", "+0V9", 0.9, 0.9, None, 20.0, 4.95, 4.0, 18.0, 19.8,
                    phases=[PhaseReport(1, 0.9, 10.0, 4.96, 1.9),
                            PhaseReport(2, 0.9, 10.0, 4.94, 2.1)])
    return TreeResult(vrms=[vr3, vr4])


def test_regulator_inputs_one_per_phase():
    from kipi.ac.target import vrm_inputs
    s = _regulators()
    assert [(n, v.name, k) for n, v, k in vrm_inputs(s)] == [
        ("VR3 input", "VR3", 0), ("VR4 phase 1 input", "VR4", 0),
        ("VR4 phase 2 input", "VR4", 1)]                 # the source has no input
    s.vrms[2].phases[0].input = None                     # a phase without input pins
    assert [n for n, _, _ in vrm_inputs(s)] == ["VR3 input", "VR4 phase 1 input"]


def test_an_ldo_input_is_a_load_with_its_input_range():
    from kipi.ac.target import probe_loads
    from kipi.dc.pi.model import PinRef
    s = _regulators()
    ls = probe_loads(s, PinRef("U4", "+5V", ["1"]), PinRef("U4", "GND", ["2"]))
    assert [(p.name, p.exact, p.vrm_input) for p in ls] == [("VR3 input", True, True)]
    t = ls[0].tolerance
    assert t.v_ac_v == pytest.approx(0.5) and t.label() == "VR3 input: 4.5 to 5.5 V (±500 mV)"
    assert ls[0].current_a == 0 and ls[0].drop is None           # no DC solve yet
    vr3 = s.vrms[1]
    vr3.vin_min, vr3.vin_max, vr3.vin_pct = -10.0, 5.0, True       # +5 % of 5 V is tighter
    t = probe_loads(s, PinRef("U4", "+5V"))[0].tolerance
    assert t.v_ac_v == pytest.approx(0.25) and t.label() == "VR3 input: -10 / +5 % of 5 V"
    vr3.vin_min = vr3.vin_max = None                             # no input range
    assert probe_loads(s, PinRef("U4", "+5V"))[0].tolerance is None
    s.vrms[0].output.net = "+12V"                                # nothing drives +5V
    vr3.vin_min, vr3.vin_max, vr3.vin_pct = 4.5, 5.5, False
    assert probe_loads(s, PinRef("U4", "+5V"))[0].tolerance is None
    # Ground pins on another part: not this probe's load
    assert probe_loads(s, PinRef("U4", "+5V"), PinRef("J1", "GND")) == []


def test_regulator_inputs_take_current_and_drop_from_the_solve():
    from kipi.ac.target import probe_loads
    from kipi.dc.pi.model import PinRef
    s, tree = _regulators(), _vrm_tree()
    ldo = probe_loads(s, PinRef("U4", "+5V", ["1"]), None, tree)[0]
    assert ldo.current_a == pytest.approx(0.501)
    assert ldo.drop.v == pytest.approx(0.03) and ldo.drop.kind == "drop"
    assert ldo.drop.label() == "VR3 input: 4.97 V of 5 V (30 mV drop)"
    ls = probe_loads(s, PinRef("U6", "+5V", ["2"]), PinRef("U6", "GND", ["9"]), tree)
    assert [(p.name, p.exact) for p in ls] == [("VR4 phase 2 input", True),
                                               ("VR4 phase 1 input", False)]
    assert ls[0].current_a == pytest.approx(2.1) and ls[0].drop.v == pytest.approx(0.06)
    assert ls[1].current_a == pytest.approx(1.9) and ls[1].drop.v == pytest.approx(0.04)
    assert ls[0].tolerance is None                     # VR4 has no input range


def test_probe_from_a_regulator_input_takes_its_pins(app):
    from qtpy.QtWidgets import QMenu
    from kipi.ac.gui.panel import ACPanel
    from kipi.dc.pi.model import Load, PinRef
    panel = ACPanel()
    panel.setEnabled(True)
    menu = QMenu()
    panel._fill_load_menu(menu)
    assert [a.text() for a in menu.actions()] == ["No DC loads or regulator inputs in the setup"]
    panel.setup = _regulators()
    panel.setup.loads.append(Load("SoC", PinRef("U1", "+0V9"), 5.0))
    panel._fill_load_menu(menu)
    assert [a.text() for a in menu.actions() if not a.isSeparator()] == [
        "SoC", "VR3 input", "VR4 phase 1 input", "VR4 phase 2 input"]
    menu.actions()[-1].trigger()
    probe = panel.setup.probes[-1]
    assert probe.name == "VR4 phase 2 input"
    assert probe.rail == PinRef("U6", "+5V", ["2"]) and probe.ground == PinRef("U6", "GND", ["9"])
    assert probe.rail is not panel.setup.vrms[2].phases[0].input
    assert panel.probe_from_vrm_input(panel.setup.vrms[1], 0).name == "VR3 input"


def test_dialog_asks_for_a_dc_solve_for_a_regulators_current(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import ProbeLoad
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    d = TargetCalculator(loads=[ProbeLoad("VR3 input", None, 0.0, True, vrm_input=True)])
    assert not d.btn_current.isEnabled() and "solve it on the DC tab" in d.btn_current.toolTip()
    assert "no input range" in d.btn_copy.toolTip()
    d = TargetCalculator(loads=[ProbeLoad("VR3 input", None, 0.501, True, vrm_input=True)])
    assert "in the last DC solve, 0.501 A" in d.btn_current.toolTip()
    d.btn_current.click()
    assert d.i_step.value() == pytest.approx(0.501)


def test_a_loads_own_ac_tolerance_is_offered_apart():
    """#174: the load's AC tolerance, its min below and its max above the DC voltage."""
    from kipi.ac.target import probe_loads
    from kipi.dc.pi.model import PinRef
    s = _setup()
    soc = s.loads[1]
    assert probe_loads(s, PinRef("U1", "+1V0"))[0].ac_tolerance is None
    soc.ac_tol_min, soc.ac_tol_max = -1.5, 2.0                 # % of VR1's 1 V
    t = probe_loads(s, PinRef("U1", "+1V0"))[0].ac_tolerance
    assert t.v_ac_v == pytest.approx(0.015) and t.room() == pytest.approx((0.015, 0.02))
    assert t.label() == "SoC: AC -1.5 / +2 % of 1 V"
    soc.ac_tol_min, soc.ac_tol_max, soc.ac_tol_pct, soc.ac_tol_dv = None, 0.03, False, True
    t = probe_loads(s, PinRef("U1", "+1V0"))[0].ac_tolerance
    assert t.v_ac_v == pytest.approx(0.03) and t.label() == "SoC: AC max +0.03 V (±30 mV)"
    soc.ac_tol_max = -0.01                                    # no room: nothing
    assert probe_loads(s, PinRef("U1", "+1V0"))[0].ac_tolerance is None
    s.vrms.clear()                    # ±V needs no VRM; % does
    soc.ac_tol_max = 0.02
    assert probe_loads(s, PinRef("U1", "+1V0"))[0].ac_tolerance.v_ac_v == pytest.approx(0.02)
    soc.ac_tol_pct, soc.ac_tol_dv = True, False
    assert probe_loads(s, PinRef("U1", "+1V0"))[0].ac_tolerance is None


def test_dialog_copies_the_ac_tolerance_only_when_there_is_one(app, monkeypatch):
    from kipi.ac.gui.target_calc import TargetCalculator
    from kipi.ac.target import LoadTolerance, ProbeLoad
    monkeypatch.setattr(TargetCalculator, "last", (0.0, 0.0, 0.0))
    assert not TargetCalculator().btn_copy_ac.isEnabled()      # greyed out by default
    total = LoadTolerance("A", "±5 %", 1.0, 0.05)
    ac = LoadTolerance("A", "AC -2 / +3 %", 1.0, 0.02, 0.02, 0.03)
    a = ProbeLoad("A", total, 1.0, ac_tolerance=ac)
    b = ProbeLoad("B", total, 1.0)
    d = TargetCalculator(loads=[b, a])
    assert not d.btn_copy_ac.isEnabled() and "B has no AC tolerance" in d.btn_copy_ac.toolTip()
    d.load.setCurrentIndex(1)
    assert d.btn_copy_ac.isEnabled() and "<img" in d.btn_copy_ac.toolTip()
    d.btn_copy_ac.click()
    assert d.v_ac.value() == pytest.approx(20)                 # its tighter side
    d.btn_copy.click()
    assert d.v_ac.value() == pytest.approx(50)                 # the total, as before
