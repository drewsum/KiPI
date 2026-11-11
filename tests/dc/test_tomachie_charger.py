# Copyright (c) 2026 Tomachie LLC.

import copy

import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Stackup, Zone,
)
from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.model import validate
from kipi.dc.pi.tree import solve_tree
from test_tomachie_pdn import PDN
from tree_boards import B, F, T, by_name, pad, poly


def _board():
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [pad("j4_1", "VBUS", "J4", "1", box(0, 0, 1, 1)),
            pad("u1_1", "VBUS", "U1", "1", box(9, 0, 10, 1)),
            pad("u1_2", "BAT", "U1", "2", box(12, 0, 13, 1)),
            pad("j1_1", "BAT", "J1", "1", box(21, 0, 22, 1)),
            pad("u2_1", "BAT", "U2", "1", box(16, 0, 17, 1)),
            pad("j4_2", "GND", "J4", "2", box(0, 0, 1, 1), B),
            pad("u1_3", "GND", "U1", "3", box(10, 0, 11, 1), B),
            pad("j1_2", "GND", "J1", "2", box(21, 0, 22, 1), B),
            pad("u2_2", "GND", "U2", "2", box(16, 0, 17, 1), B)]
    zones = [Zone("a", "VBUS", "", 0, {F: [poly(box(0, 0, 10, 1))]}),
             Zone("b", "BAT", "", 0, {F: [poly(box(12, 0, 22, 1))]}),
             Zone("c", "GND", "", 0, {B: [poly(box(0, 0, 22, 1))]})]
    nets = [Net(1, "VBUS"), Net(2, "BAT"), Net(3, "GND")]
    return BoardSnapshot("charger", "", st, nets, [], [], pads, [], zones, [])


def _pdn(cell=True):
    pdn = copy.deepcopy(PDN)
    vbus, bat = pdn["rails"]
    vbus.update(name="VBUS", voltage={"nominal": 5.0, "min": "not_specified",
                                      "max": "not_specified", "basis": "user"})
    vbus["source"].update(refdes="J4", pins=["J4_1"], output_net="VBUS")
    vbus["loads"][0]["current"] = {"amps": 0.8, "basis": "rollup"}
    bat.update(name="BAT", voltage={"nominal": 3.7, "min": 3.0, "max": 4.2, "basis": "user"})
    bat["source"].update(pins=["U1_2"], output_net="BAT", output_path=[])
    bat["loads"] = [{"role": "offboard_supply", "refdes": "J1", "pins": ["J1_1"],
                     "current": {"amps": 0.8, "basis": "user"}},
                    {"role": "ic", "refdes": "U2", "pins": ["U2_1"],
                     "current": {"amps": 0.1, "basis": "typ"}}]
    reg = {"refdes": "U1", "type": "charger", "type_basis": "datasheet",
           "input_rails": ["VBUS"], "output_rails": ["BAT"]}
    if cell:
        reg["cell"] = {"refdes": "J1", "part_number": "B3B-XH-A", "rail": "BAT",
                       "pins": ["J1_1"]}
    pdn["regulators"] = [reg]
    return pdn


def test_the_cell_is_the_source_and_the_charger_draws_its_input():
    setup, notes = TomachiePdnImporter(_pdn(), _board()).run()
    cell = by_name(setup.vrms, "J1 BAT")
    assert (cell.kind, cell.output.net, cell.output.pins, cell.voltage) == \
        ("source", "BAT", ["1"], 3.7)
    assert not any(v.output.ref == "U1" for v in setup.vrms)
    charge = by_name(setup.loads, "U1 charge input")
    assert (charge.supply.net, charge.current) == ("VBUS", pytest.approx(0.8))
    assert "J1" not in [x.name for x in setup.loads]
    assert validate(setup) == []
    assert any("U1 (BAT): battery charger; the cell J1 supplies BAT" in n for n in notes)
    u2 = by_name(solve_tree(_board(), setup).loads, "U2")
    assert 3.6 < u2.v_effective < 3.7


def test_a_charger_with_no_cell_imports_no_loads():
    setup, notes = TomachiePdnImporter(_pdn(cell=False), _board()).run()
    assert "U2" not in [x.name for x in setup.loads]
    assert any("battery charger U1 with no battery or connector" in n for n in notes)
