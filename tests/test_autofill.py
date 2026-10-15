"""Setup fields pre-filled from the board, which the user can always change."""

import os

import pytest

from kipi.core.model.board import Footprint, Net, Pad
from kipi.core.model.nets import guess_voltage, voltage_from_name
from kipi.dc.pi.autofill import (
    ground_net, guess_other_side, input_net, next_part, other_net, part_nets,
)
from kipi.dc.pi.model import Load, PinRef, PISetup
from synthetic import make_board

PARTS = {
    "J1": "CONN", "U2": "LDO", "R5": "10m", "FB1": "600R@100MHz", "U3": "FPGA",
    "L1": "1u", "L2": "1u", "L3": "1u", "Q1": "FET", "Q2": "FET", "Q3": "FET",
    "C1": "10u", "C2": "10u", "C3": "100n",
}
PINS = [  # (ref, pin, net)
    ("J1", "1", "+5V"), ("J1", "2", "GND"),
    ("U2", "1", "+5V"), ("U2", "2", "+3V3"), ("U2", "3", "GND"), ("U2", "4", "EN"),
    ("R5", "1", "+3V3"), ("R5", "2", "VSNS"),
    ("FB1", "1", "+3V3"), ("FB1", "2", "VSNS"),
    ("U3", "1", "VSNS"), ("U3", "2", "VSNS"), ("U3", "3", "GND"), ("U3", "4", "GND"),
    ("U3", "5", "VCORE"),
    *[(f"L{k}", p, n) for k in (1, 2, 3) for p, n in (("1", f"SW{k}"), ("2", "VCORE"))],
    *[(f"Q{k}", p, n) for k in (1, 2, 3)
      for p, n in (("1", "+5V"), ("2", "GND"), ("3", f"SW{k}"))],
    ("C1", "1", "+3V3"), ("C1", "2", "GND"), ("C2", "1", "+3V3"), ("C2", "2", "GND"),
    ("C3", "1", "VSNS"), ("C3", "2", "GND"),
]


def board(ground="GND"):
    """Parts on +5V, +3V3, VSNS, VCORE and a ground net called ``ground``."""
    snap = make_board()
    snap.pads, snap.footprints = [], []
    names = sorted({n for _, _, n in PINS})
    snap.nets = [Net(0, "")] + [Net(i + 1, ground if n == "GND" else n)
                                for i, n in enumerate(names)]
    for i, (ref, pin, net) in enumerate(PINS):
        snap.pads.append(Pad(f"p{i}", ground if net == "GND" else net, ref, pin,
                             (float(i), 0.0), "smd"))
    for ref, value in PARTS.items():
        ids = [p.id for p in snap.pads if p.footprint == ref]
        snap.footprints.append(Footprint(f"f{ref}", ref, value, (0.0, 0.0), "top", ids))
    snap.invalidate_index()
    return snap


# ---------------------------------------------------------------------------
# The guesses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,volts", [("-5V", -5.0), ("/AFE/-12V", -12.0), ("VCC-3V3", 3.3),
                                        ("VDD_0V75", 0.75), ("P1V8_PLL", 1.8),
                                        ("USB3_VBUS", 5.0), ("VIN", None), ("VCORE", None),
                                        ("", None)])
def test_voltage_from_name(name, volts):
    assert voltage_from_name(name) == (pytest.approx(volts) if volts is not None else None)


def test_guess_voltage_falls_back_to_one_volt():
    assert guess_voltage("VCORE") == 1.0


def test_part_nets_and_other_side():
    snap = board()
    assert part_nets(snap, "U3") == {"VSNS": ["1", "2"], "GND": ["3", "4"], "VCORE": ["5"]}
    assert part_nets(snap, "nope") == {}
    assert other_net(snap, "R5", "+3V3") == "VSNS"
    assert other_net(snap, "R5", "VSNS") == "+3V3"
    assert other_net(snap, "U2", "+5V") is None             # four nets: not two-terminal
    assert other_net(snap, "R5", "GND") is None             # not one of its nets
    assert guess_other_side(snap, PinRef("FB1", "+3V3", ["1"])) == PinRef("FB1", "VSNS")
    assert guess_other_side(snap, PinRef("U2", "+5V")) is None
    # Never a capacitor, nor across to ground: those aren't series parts.
    assert guess_other_side(snap, PinRef("C3", "VSNS")) is None
    assert guess_other_side(snap, PinRef("J1", "+5V")) is None
    assert guess_other_side(board(ground="RTN"), PinRef("J1", "+5V")) is None
    assert guess_other_side(snap, PinRef("L1", "VCORE"), grounds=["SW1"]) is None
    assert guess_other_side(snap, None) is None


def test_ground_named_like_a_ground():
    snap = board()
    assert ground_net(snap, "U3", ["VSNS"]) == "GND"
    assert ground_net(snap, "U3", ["VSNS"], allowed=["VSNS", "VCORE"]) is None
    assert ground_net(snap, "L1", ["VCORE"]) is None        # no ground pins
    assert ground_net(None, "U3") is None


def test_ground_with_an_unusual_name():
    snap = board(ground="RTN")
    # Not named like a ground, but the board's biggest net ...
    assert ground_net(snap, "U3", ["VSNS"]) == "RTN"
    # ... or a ground the setup already uses, even if it isn't ticked.
    assert ground_net(snap, "U2", ["+3V3"], allowed=["+3V3"], known=["RTN"]) == "RTN"
    # A known ground wins over the name heuristic.
    assert ground_net(board(), "U3", ["VSNS"], known=["VCORE"]) == "VCORE"


def test_input_rail_of_a_regulator():
    snap = board()
    assert input_net(snap, "U2", ["+3V3", "GND"]) == "+5V"
    assert input_net(snap, "U2", ["+3V3", "GND"], allowed=["+3V3", "GND"]) is None
    assert input_net(snap, "U3", ["VCORE", "GND"]) is None   # VSNS isn't named like a rail
    assert input_net(snap, "U3", ["VSNS", "GND"]) == "VCORE"
    assert input_net(snap, "J1", ["+5V", "GND"]) is None


def test_next_part_in_sequence():
    snap = board()
    assert next_part(snap, "L1", "VCORE") == "L2"
    assert next_part(snap, "L1", "VCORE", taken=["L2"]) == "L3"
    assert next_part(snap, "L3", "VCORE") is None
    assert next_part(snap, "Q1", "GND") == "Q2"
    assert next_part(snap, "U2", "GND") == "U3"
    assert next_part(snap, "U2", "+5V") is None              # U3 isn't on +5V
    assert next_part(snap, "J1", "+5V") is None


# ---------------------------------------------------------------------------
# In the editors
# ---------------------------------------------------------------------------

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def panel_for(snap, setup, nets):
    from kipi.dc.gui.pi_panel import PIPanel
    panel = PIPanel()
    panel.set_board(snap, setup, "test")
    panel.set_nets(nets)
    return panel


def pick(picker, ref=None, net=None, pins=None):
    if net is not None:
        picker.cmb_net.setCurrentIndex(picker.cmb_net.findText(net))
    if ref is not None:
        picker.cmb_part.setCurrentIndex(picker.cmb_part.findData(ref))
    if pins is not None:
        picker.txt_pins.setText(pins)
        picker.txt_pins.editingFinished.emit()


ALL = ["+5V", "+3V3", "VSNS", "VCORE", "GND"]


def test_link_fills_its_other_side_and_resistance(app):
    setup = PISetup()
    panel = panel_for(board(), setup, ALL)
    panel.link_list._add()
    k, ed = setup.links[0], panel.link_list.editor
    pick(ed.a, net="VSNS", ref="R5")
    assert (k.name, k.side_a, k.side_b) == ("R5", PinRef("R5", "VSNS"), PinRef("R5", "+3V3"))
    assert k.resistance == pytest.approx(0.01) and ed.b.value() == k.side_b
    # A ferrite: the other side follows; no resistance in its value, so none is set.
    ed.r.setValue(0)
    pick(ed.a, ref="FB1")
    assert k.side_b == PinRef("FB1", "+3V3") and k.resistance == 0
    # The user can still point side B elsewhere, and that is kept.
    pick(ed.b, net="+3V3", ref="C1")
    assert k.side_b.ref == "C1"
    ed.r.setValue(5.0)
    assert k.side_b.ref == "C1" and k.resistance == pytest.approx(0.005)
    # A part on more than two nets leaves side B alone.
    pick(ed.a, net="VSNS", ref="U3")
    assert k.side_b.ref == "C1"
    panel.close()


def test_new_link_prefers_a_two_terminal_part(app):
    setup = PISetup()
    panel = panel_for(board(), setup, ["+3V3", "GND", "VSNS"])
    panel.link_list._add()
    k = setup.links[0]
    # Linked to VSNS, not to the ground, through the first two-terminal part between them.
    assert (k.side_a, k.side_b) == (PinRef("FB1", "+3V3"), PinRef("FB1", "VSNS"))
    # Between VCORE and VSNS only U3 bridges (no capacitors between rail and ground).
    panel.set_nets(["VCORE", "GND", "VSNS"])
    panel.link_list._add()
    k = setup.links[1]
    assert (k.side_a, k.side_b) == (PinRef("U3", "VCORE"), PinRef("U3", "VSNS"))
    panel.close()


def test_load_takes_its_parts_ground(app):
    setup = PISetup()
    panel = panel_for(board(), setup, ALL)
    panel.load_list._add()
    ld, ed = setup.loads[0], panel.load_list.editor
    pick(ed.supply, net="VSNS", ref="U3")
    assert ld.ground == PinRef("U3", "GND") and ed.ground.value() == ld.ground
    # Another part's ground pins, picked by the user, are kept when the supply changes.
    pick(ed.ground, net="GND", ref="C3")
    pick(ed.supply, net="+3V3", ref="U2")
    assert ld.ground.ref == "C3"
    # A part with no ground pins leaves the ground as it is.
    pick(ed.ground, net="GND", ref="U2")
    pick(ed.supply, net="+3V3", ref="R5")
    assert ld.ground == PinRef("U2", "GND")
    panel.close()


def test_load_ground_with_an_unusual_name(app):
    setup = PISetup()
    panel = panel_for(board(ground="RTN"), setup, ["VSNS", "RTN"])
    panel.load_list._add()
    pick(panel.load_list.editor.supply, net="VSNS", ref="U3")
    assert setup.loads[0].ground == PinRef("U3", "RTN")
    panel.close()


def test_load_ground_only_on_ticked_nets(app):
    setup = PISetup()
    panel = panel_for(board(), setup, ["VSNS"])
    panel.load_list._add()
    assert setup.loads[0].ground is None
    # ... unless the setup already uses that ground.
    setup.loads[0].ground = PinRef("C3", "GND")
    panel.load_list._add()
    assert setup.loads[1].ground == PinRef("U3", "GND")
    panel.close()


def test_vrm_voltage_ground_and_input(app):
    setup = PISetup()
    panel = panel_for(board(), setup, ALL)
    panel.vrm_list._add()
    v, ed = setup.vrms[0], panel.vrm_list.editor
    ed.voltage.setValue(2.5)
    pick(ed.output, net="VCORE")                   # no voltage in the name: kept
    assert v.voltage == pytest.approx(2.5)
    pick(ed.output, net="+3V3", ref="U2")
    assert v.voltage == pytest.approx(3.3) and v.name == "U2"
    assert v.ground == PinRef("U2", "GND") and v.input is None     # a source has no input
    ed.kind.setCurrentIndex(ed.kind.findData("ldo"))
    assert v.input == PinRef("U2", "+5V") and ed.input.value() == v.input
    # The user's own values are kept.
    ed.voltage.setValue(3.0)
    pick(ed.input, net="+5V", ref="J1")
    assert v.voltage == pytest.approx(3.0) and v.input.ref == "J1"
    panel.close()


def test_new_vrm_is_named_after_its_part(app):
    setup = PISetup()
    panel = panel_for(board(), setup, ["+5V", "GND"])
    panel.vrm_list._add()
    v = setup.vrms[0]
    assert v.output.ref in v.name and v.voltage == pytest.approx(5.0)
    assert v.ground == PinRef(v.output.ref, "GND")
    panel.close()


def test_new_phases_take_the_next_parts(app):
    setup = PISetup()
    panel = panel_for(board(), setup, ALL)
    panel.vrm_list._add()
    v, ed = setup.vrms[0], panel.vrm_list.editor
    ed.kind.setCurrentIndex(ed.kind.findData("smps"))
    pick(ed.output, net="VCORE", ref="L1", pins="2")
    pick(ed.input, net="+5V", ref="Q1", pins="1")
    pick(ed.ground, net="GND", ref="Q1", pins="2")
    ed.n_phases.setValue(3)
    assert [(p.output, p.input, p.ground) for p in v.phases] == [
        (PinRef(f"L{k}", "VCORE", ["2"]), PinRef(f"Q{k}", "+5V", ["1"]),
         PinRef(f"Q{k}", "GND", ["2"])) for k in (2, 3)]
    # Past the last part in sequence, a phase repeats the last one for the user to edit.
    ed.n_phases.setValue(4)
    assert v.phases[-1].output == PinRef("L3", "VCORE", ["2"])
    panel.close()


def test_probe_takes_its_parts_ground(app):
    from kipi.ac.gui.panel import ACPanel
    setup = PISetup()
    setup.loads.append(Load("U3", PinRef("U3", "VSNS"), 1.0))
    panel = ACPanel()
    panel.set_board(board(ground="RTN"), setup, "test")
    panel.set_nets(["VSNS", "+3V3", "RTN"])
    panel.probe_list._add()
    p, ed = setup.probes[0], panel.probe_list.editor
    pick(ed.rail, net="+3V3", ref="U2")
    assert p.ground == PinRef("U2", "RTN")
    pick(ed.rail, net="VSNS", ref="U3")
    assert p.ground == PinRef("U3", "RTN") and ed.ground.value() == p.ground
    panel.close()
