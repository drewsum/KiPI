"""Importing a Tomachie power-tree file onto a board."""

import copy
import json

import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Stackup, Zone,
)
from kipi.dc.pi.importers.pdn_figures import dc_resistance
from kipi.dc.pi.importers.tomachie_pdn import (
    PdnImportError, TomachiePdnImporter, import_tomachie_pdn, path_to_rail,
)
from kipi.dc.pi.model import PISetup, validate
from kipi.dc.pi.tree import solve_tree
from tree_boards import B, F, T, board, by_name, pad, poly

PDN = {
    "schema": "tomachie-pdn", "schema_version": 1, "spec_revision": 2,
    "ground_nets": ["GND"],
    "rails": [
        {"name": "VIN", "voltage": {"nominal": 12.0, "min": "not_specified",
                                    "max": "not_specified", "basis": "net_name"},
         "source": {"role": "board_input", "refdes": "J1", "pins": ["J1_1"],
                    "output_net": "VIN", "output_path": []},
         "loads": [{"role": "regulator", "refdes": "U1", "pins": ["U1_1"],
                    "current": {"amps": 1.0, "basis": "rollup"}}],
         "offboard_pins": []},
        {"name": "+3V3", "voltage": {"nominal": 3.3, "min": 3.135, "max": 3.465,
                                     "basis": "user"},
         "source": {"role": "regulator", "refdes": "U1", "pins": ["U1_2"],
                    "output_net": "+3V3",
                    "output_path": [{"refdes": "R99", "class": "resistor", "value": "10k",
                                     "net_in": "+3V3", "net_out": "nRESET",
                                     "resistance_ohm": 10000.0, "datasheet_figures": []}]},
         "loads": [{"role": "ic", "refdes": "U2", "pins": ["U2_1"],
                    "current": {"amps": 1.0, "basis": "typ"},
                    "current_max": {"amps": 1.5, "basis": "max"},
                    "supply_min": {"volts": 3.0, "basis": "min", "spec": "vdd", "page": 4}}],
         "offboard_pins": []},
    ],
    "regulators": [{"refdes": "U1", "type": "linear", "efficiency": "unknown",
                    "input_rails": ["VIN"], "output_rails": ["+3V3"]}],
}


def run(pdn):
    return TomachiePdnImporter(copy.deepcopy(pdn), board()).run()


def test_sources_regulators_and_loads_map_onto_the_board():
    setup, notes = run(PDN)
    j1, u1 = by_name(setup.vrms, "J1 VIN"), by_name(setup.vrms, "U1 +3V3")
    assert (j1.kind, j1.voltage, j1.output.pins) == ("source", 12.0, ["1"])
    assert (u1.kind, u1.voltage, u1.input.net, u1.input.pins) == ("ldo", 3.3, "VIN", ["1"])
    u2 = by_name(setup.loads, "U2")
    # One current per load: the typical; the maximum is named in the notes.
    assert u2.current == 1.0
    assert any("one current per load" in n and "U2 (max 1.5 A)" in n for n in notes)
    # The datasheet minimum (3.0 V) is above the rail's (3.135 V)? No: the rail's is
    # tighter, so the rail's stated range is the tolerance, in % from nominal.
    assert (u2.tol_pct, u2.tol_min, u2.tol_max) == (True, pytest.approx(-5.0),
                                                     pytest.approx(5.0))
    # The regulator's own load entry is carried by its VRM input, not imported twice.
    assert [l.name for l in setup.loads] == ["U2"]
    assert validate(setup) == []
    assert any("R99" in n and "left out" in n for n in notes)


def test_imported_setup_solves():
    setup, _ = run(PDN)
    res = solve_tree(board(), setup)
    assert by_name(res.loads, "U2").passed is True


def test_unknown_current_and_off_board_pin_are_not_known():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["current"] = "unknown"
    del pdn["rails"][1]["loads"][0]["current_max"]
    pdn["rails"][1]["offboard_pins"] = ["U2_1"]
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 0.0
    assert by_name(setup.loads, "U2_1 (off-board)").current == 0.0
    assert any(n.startswith("Current not known, imported as 0 A") and "U2, U2_1 (off-board)"
               in n for n in notes)
    assert validate(setup) == []


def test_load_that_draws_what_it_drives_is_not_known():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0].update(load_dependent=True, load_dependent_reason="LED driver")
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 0.0
    assert any("LED driver" in n for n in notes)
    assert any(n.startswith("Current not known") for n in notes)


def test_source_pin_missing_from_the_board_leaves_out_what_it_powered():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][0]["source"].update(refdes="J5", pins=["J5_1"])   # no J5 on the board
    setup, notes = run(pdn)
    assert setup.vrms == [] and setup.loads == []
    assert any("J5_1 not on the board" in n for n in notes)
    assert any("U1 +3V3" in n and "left out" in n for n in notes)
    assert validate(setup) == []


def test_wrong_schema_is_refused():
    with pytest.raises(PdnImportError):
        TomachiePdnImporter({"schema": "other"}, board())


def test_path_to_rail_follows_the_chain_only():
    parts = [{"refdes": "L1", "net_in": "SW", "net_out": "+5V"},
             {"refdes": "R40", "net_in": "SW", "net_out": "FB"}]
    assert [e["refdes"] for e in path_to_rail(parts, "SW", "+5V")] == ["L1"]


@pytest.mark.parametrize("figs,expect", [
    ([{"spec": "dcr", "max": "0.080", "unit": "ohm"},
      {"spec": "impedance_at_frequency", "typ": "330", "unit": "ohm"}], (None, 0.08)),
    ([{"spec": "rds_on", "typ": "9.2", "max": "11", "unit": "mΩ"},
      {"spec": "rds_on", "typ": "14.7", "max": "18.5", "unit": "mΩ"}], (0.0147, 0.0185)),
    ([{"spec": "dc_resistance", "typ": "64", "max": "68", "unit": "mOhm"}], (0.064, 0.068)),
])
def test_dc_resistance_never_takes_an_impedance(figs, expect):
    typ, mx, _ = dc_resistance({"resistance_ohm": "unknown", "datasheet_figures": figs})
    assert typ == (None if expect[0] is None else pytest.approx(expect[0]))
    assert mx == pytest.approx(expect[1])


def test_regulator_quiescent_dropout_and_input_range():
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(
        quiescent_current={"amps": 6.5e-6, "basis": "typ"},
        dropout_voltage={"volts": 0.29, "basis": "max"},
        input_voltage_min={"volts": 1.6, "basis": "min"},
        input_voltage_max={"volts": 6.0, "basis": "max"})
    setup, _ = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.quiescent_a, u1.dropout_v, u1.vin_min, u1.vin_max) == (6.5e-6, 0.29, 1.6, 6.0)


def test_source_pin_named_by_pin_name_uses_the_parts_pads_on_its_output_net():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][0]["source"]["pins"] = ["J1_VBUS"]     # a pin name, not a pad number
    setup, notes = run(pdn)
    j1 = by_name(setup.vrms, "J1 VIN")
    assert (j1.output.ref, j1.output.net, j1.output.pins) == ("J1", "VIN", ["1"])
    assert any("J1_VBUS not on the board" in n for n in notes)
    assert any("J1's pads on its output net VIN" in n for n in notes)


def test_rail_whose_source_is_not_on_the_board_is_noted():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["source"].update(pins=[], output_net="NOWHERE")
    _, notes = run(pdn)
    assert any(n.startswith("Rail +3V3: regulator U1 output pins not found") for n in notes)


def _load_switch(**reg):
    """U1 as a power switch passing VIN (12 V) to its output rail."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["voltage"] = {"nominal": 12.0, "min": "not_specified",
                                  "max": "not_specified", "basis": "net_name"}
    pdn["rails"][1]["source"]["output_path"] = []
    pdn["regulators"][0] = {"refdes": "U1", "type": "load_switch", "type_basis": "schematic",
                            "input_rails": ["VIN"], "output_rails": ["+3V3"], **reg}
    return pdn


def test_load_switch_is_a_dc_connection_with_its_on_resistance():
    figs = [{"spec": "rds_on", "typ": "89", "max": "120", "unit": "mΩ", "page": 4}]
    setup, notes = run(_load_switch(datasheet_resistances=figs,
                                    quiescent_current={"amps": 9e-5, "basis": "typ"}))
    # Not a VRM: an LDO making 12 V from 12 V would be out of regulation.
    assert [v.name for v in setup.vrms] == ["J1 VIN"]
    u1 = by_name(setup.links, "U1")
    assert (u1.side_a.net, u1.side_a.pins, u1.side_b.net, u1.side_b.pins) == \
        ("VIN", ["1"], "+3V3", ["2"])
    assert u1.resistance == pytest.approx(0.089)
    assert any("one resistance per DC connection" in n and "U1 120 mΩ" in n for n in notes)
    # Its quiescent current is drawn at its input pins.
    iq = by_name(setup.loads, "U1 quiescent")
    assert (iq.supply.net, iq.current) == ("VIN", 9e-5)
    assert validate(setup) == []
    assert any("U1 (+3V3): load switch, imported as a DC connection" in n for n in notes)
    res = solve_tree(board(), setup)
    assert by_name(res.loads, "U2").passed is True


def test_load_switch_without_an_on_resistance_is_a_short_and_said():
    setup, notes = run(_load_switch())
    u1 = by_name(setup.links, "U1")
    assert u1.resistance == 0.0
    assert [l.name for l in setup.loads] == ["U2"]       # no quiescent current in the file
    assert any("U1: on-resistance not known; modeled as a short" in n for n in notes)


def filtered_board():
    """J1 -> VIN -> FB1 -> VIN_F -> U1 (switch) -> SWOUT -> FB2 -> +3V3 -> U2."""
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    spans = {"VIN": (0, 7), "VIN_F": (8, 10), "SWOUT": (12, 14), "+3V3": (15, 22)}
    pads = [pad("j1_1", "VIN", "J1", "1", box(0, 0, 1, 1)),
            pad("fb1_1", "VIN", "FB1", "1", box(6, 0, 7, 1)),
            pad("fb1_2", "VIN_F", "FB1", "2", box(8, 0, 9, 1)),
            pad("u1_1", "VIN_F", "U1", "1", box(9, 0, 10, 1)),
            pad("u1_2", "SWOUT", "U1", "2", box(12, 0, 13, 1)),
            pad("fb2_1", "SWOUT", "FB2", "1", box(13, 0, 14, 1)),
            pad("fb2_2", "+3V3", "FB2", "2", box(15, 0, 16, 1)),
            pad("u2_1", "+3V3", "U2", "1", box(21, 0, 22, 1)),
            pad("j1_2", "GND", "J1", "2", box(0, 0, 1, 1), B),
            pad("u1_3", "GND", "U1", "3", box(10, 0, 11, 1), B),
            pad("u2_2", "GND", "U2", "2", box(21, 0, 22, 1), B)]
    zones = [Zone(n, n, "", 0, {F: [poly(box(a, 0, b, 1))]}) for n, (a, b) in spans.items()]
    zones.append(Zone("gnd", "GND", "", 0, {B: [poly(box(0, 0, 22, 1))]}))
    nets = [Net(i + 1, n) for i, n in enumerate([*spans, "GND"])]
    return BoardSnapshot("filtered", "", st, nets, [], [], pads, [], zones, [])


def test_load_switch_behind_an_input_filter_with_a_part_on_its_output():
    ferrite = {"class": "ferrite", "resistance_ohm": "unknown",
               "datasheet_figures": [{"spec": "dcr", "max": "0.080", "unit": "ohm"},
                                     {"spec": "impedance_at_frequency", "typ": "330",
                                      "unit": "ohm"}]}
    pdn = _load_switch(datasheet_resistances=[{"spec": "rds_on", "typ": "0.089",
                                               "unit": "ohm"}])
    pdn["rails"][0]["loads"][0].update(
        input_net="VIN_F", input_filter=[dict(ferrite, refdes="FB1", net_in="VIN",
                                              net_out="VIN_F")])
    pdn["rails"][1]["source"].update(output_net="SWOUT", output_path=[
        dict(ferrite, refdes="FB2", net_in="SWOUT", net_out="+3V3")])
    setup, _ = TomachiePdnImporter(pdn, filtered_board()).run()
    links = {k.name: (k.side_a.net, k.side_b.net, k.resistance) for k in setup.links}
    assert links == {"U1": ("VIN_F", "SWOUT", pytest.approx(0.089)),
                     "FB1": ("VIN", "VIN_F", pytest.approx(0.08)),
                     "FB2": ("SWOUT", "+3V3", pytest.approx(0.08))}
    assert [l.name for l in setup.loads] == ["U2"]       # powered through all three
    assert by_name(solve_tree(filtered_board(), setup).loads, "U2").passed is True


def test_datasheet_minimum_supply_tightens_the_rail_minimum():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["supply_min"]["volts"] = 3.2      # above the rail's 3.135 V
    setup, _ = run(pdn)
    u2 = by_name(setup.loads, "U2")
    assert (u2.tol_pct, u2.tol_min, u2.tol_max) == (False, 3.2, 3.465)


def test_minimum_current_and_only_a_maximum():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["current_min"] = {"amps": 0.2, "basis": "min"}
    setup, notes = run(pdn)
    assert any("U2 (min 200 mA, max 1.5 A)" in n for n in notes)
    pdn["rails"][1]["loads"][0]["current"] = "unknown"
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 1.5
    assert any("U2: only a maximum current is known; 1.5 A is used" in n for n in notes)


def test_import_keeps_the_rest_of_the_setup(tmp_path):
    base = PISetup(html_report=True)
    base.settings.max_edge_mm = 0.7
    path = tmp_path / "board.pdn.json"
    path.write_text(json.dumps(PDN), encoding="utf-8")
    setup, _ = import_tomachie_pdn(path, board(), base)
    assert setup.html_report and setup.settings.max_edge_mm == 0.7
    assert [l.name for l in setup.loads] == ["U2"] and base.loads == []
