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


def test_load_that_draws_what_it_drives_is_solved_at_its_own_current():
    """Its own (static) current is solved, the least it draws; what it drives is on top."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0].update(load_dependent=True, load_dependent_reason="LED driver")
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 1.0
    assert any("LED driver" in n for n in notes)
    assert any(n.startswith("Draw what they drive") for n in notes)
    pdn["rails"][1]["loads"][0].update(current="unknown")
    del pdn["rails"][1]["loads"][0]["current_max"]
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").current == 0.0
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


def test_load_switch_feeding_two_rails_is_one_dc_connection():
    """One switch is the source of two rails (the second behind a 0R from the first): one
    switch, one on-resistance, one quiescent current - not two links in parallel."""
    figs = [{"spec": "rds_on", "typ": "48", "max": "50", "unit": "mΩ", "page": 6}]
    pdn = _load_switch(datasheet_resistances=figs,
                       quiescent_current={"amps": 3.7e-4, "basis": "typ"})
    second = copy.deepcopy(pdn["rails"][1])
    second.update(name="+3V3_B", loads=[])
    pdn["rails"].append(second)
    pdn["regulators"][0]["output_rails"] = ["+3V3", "+3V3_B"]
    setup, _ = run(pdn)
    assert [l.name for l in setup.links if l.name.startswith("U1")] == ["U1"]
    assert by_name(setup.links, "U1").resistance == pytest.approx(0.048)
    assert [l.name for l in setup.loads if "quiescent" in l.name] == ["U1 quiescent"]


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


def test_a_switchers_output_inductor_is_inside_its_regulation_loop():
    """A switcher regulates its rail after its output inductor (the feedback divider is on
    the rail), so the inductor's DCR lowers nothing: a short, said once. The same part
    after a load switch, which regulates nothing, is a real drop (test above)."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="switching", efficiency=0.9)
    pdn["rails"][0]["loads"][0].update(
        input_net="VIN_F", input_filter=[{"refdes": "FB1", "class": "ferrite",
                                          "net_in": "VIN", "net_out": "VIN_F",
                                          "resistance_ohm": 0.08, "datasheet_figures": []}])
    pdn["rails"][1]["source"].update(output_net="SWOUT", output_path=[
        {"refdes": "FB2", "class": "inductor", "net_in": "SWOUT", "net_out": "+3V3",
         "resistance_ohm": "unknown",
         "datasheet_figures": [{"spec": "dcr", "typ": "27", "max": "35", "unit": "mOhm"}]}])
    setup, notes = TomachiePdnImporter(pdn, filtered_board()).run()
    assert by_name(setup.links, "FB2").resistance == 0.0
    assert any(n.startswith("FB2: inside U1's regulation loop") for n in notes)
    assert by_name(setup.links, "FB1").resistance == pytest.approx(0.08)   # before U1: a drop
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


def test_isolated_converter_without_its_own_output_ground_is_a_switcher():
    """An isolated DC/DC converter draws its output power over its efficiency from its
    input rail; with no output ground pins of its own found it is solved as a switcher."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="isolated", efficiency=0.8)
    setup, notes = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.kind, u1.efficiency, u1.input.net) == ("smps", 0.8, "VIN")
    assert not any("not known whether linear" in n for n in notes)


def test_a_pmics_linear_output_draws_its_current_not_its_power():
    """A PMIC with bucks and LDOs types each output in output_kinds: an LDO output draws
    its own current from the input, whatever the part's efficiency."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="switching", efficiency="unknown",
                                output_kinds={"+3V3": "linear"})
    setup, notes = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.kind, u1.efficiency) == ("ldo", 1.0)
    assert not any("efficiency not read" in n for n in notes)


def test_source_pin_on_another_net_leaves_the_rail_out():
    """A file naming one regulator pin as the source of two rails is wrong about one of
    them: that rail is left out and said, and the rest still solves."""
    pdn = copy.deepcopy(PDN)
    rail = copy.deepcopy(pdn["rails"][1])
    rail["name"] = "VREF"
    rail["source"]["output_net"] = "VREF"
    rail["loads"] = []
    pdn["rails"].append(rail)
    setup, notes = run(pdn)
    assert [v.name for v in setup.vrms if v.name.startswith("U1")] == ["U1 +3V3"]
    assert any(n.startswith("Rail VREF: the file names") and "already drive" in n
               for n in notes)
    assert validate(setup) == []


@pytest.mark.parametrize("basis", ["signal_pin_only", "not_a_supply"])
def test_a_part_on_a_rail_by_no_supply_pin_is_not_a_load(basis):
    """A part on the rail only by a detect, enable or reference pin (0 A) is left out of
    the loads and named once in the notes."""
    pdn = copy.deepcopy(PDN)
    detect = copy.deepcopy(pdn["rails"][1]["loads"][0])
    detect.update(refdes="U9", current={"amps": 0.0, "basis": basis})
    pdn["rails"][1]["loads"].append(detect)
    setup, notes = run(pdn)
    assert "U9" not in [l.name for l in setup.loads]
    assert any(n.startswith("On a rail by a detect") and "U9" in n for n in notes)


def test_loads_and_regulators_return_through_their_ground_pads():
    """Each load and VRM gets its pads on the file's ground net as its ground pins, so the
    return current flows in the ground copper rather than an ideal ground."""
    setup, _ = run(PDN)
    grounds = {x.name: x.ground for x in [*setup.vrms, *setup.loads]}
    assert all(g is not None and g.net == "GND" for g in grounds.values()), grounds


def test_a_ground_with_no_source_returns_to_an_ideal_ground():
    """A load returning on a ground net no source VRM references (an isolated converter's
    output ground) goes to an ideal ground, named once, so the setup still solves."""
    pdn = copy.deepcopy(PDN)
    pdn["ground_nets"] = ["GND_ISO", "GND"]          # U2's own pads are on GND only
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").ground.net == "GND"
    snap = board()
    for p in snap.pads:
        if p.footprint == "U2" and p.net == "GND":
            p.net = "GND_ISO"
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), snap).run()
    assert by_name(setup.loads, "U2").ground is None
    assert any(n.startswith("No supply returns through GND_ISO") for n in notes)
    assert not validate(setup)


def test_a_regulator_standing_in_as_a_source_is_not_a_ground_reference():
    """A regulator of unknown type is modeled as an ideal source; its ground pins would
    pin the ground copper to 0 V there as well as at the board input, hiding the ground
    drop between them, so it gets none - the board input stays the one reference."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0]["type"] = "unknown"
    setup, notes = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.kind, u1.ground) == ("source", None)
    assert by_name(setup.vrms, "J1 VIN").ground.net == "GND"
    assert by_name(setup.loads, "U2").ground.net == "GND"


def isolated_board():
    """board() with its ground split: GND under the input side (x 0..11), GND_ISO under
    the output side (x 12..22); U1 has a pad on each, U2 returns on GND_ISO."""
    snap = board()
    snap.pads = [p for p in snap.pads if p.id != "u2_2"] + [
        pad("u1_4", "GND_ISO", "U1", "4", box(12, 0, 13, 1), B),
        pad("u2_2", "GND_ISO", "U2", "2", box(21, 0, 22, 1), B)]
    snap.zones = [z for z in snap.zones if z.net != "GND"] + [
        Zone("g1", "GND", "", 0, {B: [poly(box(0, 0, 11, 1))]}),
        Zone("g2", "GND_ISO", "", 0, {B: [poly(box(12, 0, 22, 1))]})]
    snap.nets = list(snap.nets) + [Net(len(snap.nets) + 1, "GND_ISO")]
    return snap


def test_an_isolated_converter_holds_its_output_ground_at_its_own_pins():
    """An isolated converter returns its input on its input rail's ground and holds its
    output rail's ground (isolated from it) at 0 V at its own pins there, so the loads
    behind it return through real copper, not an ideal ground."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="isolated", efficiency=0.8)
    pdn["ground_nets"] = ["GND", "GND_ISO"]
    pdn["rails"][0]["ground"] = "GND"
    pdn["rails"][1]["ground"] = "GND_ISO"
    snap = isolated_board()
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), snap).run()
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.kind, u1.ground.net, u1.input_ground.net) == ("isolated", "GND_ISO", "GND")
    assert by_name(setup.loads, "U2").ground.net == "GND_ISO"
    assert not validate(setup)
    assert not any("ideal ground" in n for n in notes)
    u2 = by_name(solve_tree(snap, setup).loads, "U2")
    assert u2.v_ground > 0                        # the return drop in GND_ISO's copper


def test_a_part_joining_two_grounds_is_a_dc_connection():
    """A 0R or net tie between two ground nets (the file's ``grounds``) is a DC connection,
    so a return reaches the reference through it; a capacitor or a 1M bleed is not."""
    pdn = copy.deepcopy(PDN)
    pdn["ground_nets"] = ["GND", "GND_ISO"]
    pdn["grounds"] = [{"net": "GND", "connections": [
        {"refdes": "U1", "class": "resistor", "value": "0R", "to": "GND_ISO"},
        {"refdes": "C9", "class": "capacitor", "value": "100n", "to": "GND_ISO"},
        {"refdes": "R77", "class": "resistor", "value": "1M", "to": "GND_ISO"}]}]
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), isolated_board()).run()
    [tie] = [k for k in setup.links if k.name == "U1"]
    assert {tie.side_a.net, tie.side_b.net} == {"GND", "GND_ISO"} and tie.resistance == 0.0
    assert not any(k.name in ("C9", "R77") for k in setup.links)


def _behind_ferrite_pdn(load):
    return {"schema": "tomachie-pdn", "schema_version": 1, "spec_revision": 7,
            "ground_nets": ["GND"],
            "rails": [{"name": "VIN", "voltage": {"nominal": 12.0, "min": 11.0, "max": 13.0,
                                                  "basis": "user"},
                       "source": {"role": "board_input", "refdes": "J1", "pins": ["J1_1"],
                                  "output_net": "VIN", "output_path": []},
                       "loads": [load], "offboard_pins": []}],
            "regulators": []}


def test_a_load_behind_a_ferrite_is_reached_through_it():
    """U1's supply pin sits behind FB1 (input_net / input_filter): FB1 is a DC connection,
    so U1 is powered from VIN through it rather than disconnected."""
    fb1 = {"refdes": "FB1", "class": "ferrite", "net_in": "VIN", "net_out": "VIN_F",
           "resistance_ohm": "unknown",
           "datasheet_figures": [{"spec": "dcr", "max": "0.080", "unit": "ohm"}]}
    pdn = _behind_ferrite_pdn({"role": "ic", "refdes": "U1", "pins": ["U1_1"],
                               "current": {"amps": 0.2, "basis": "typ"},
                               "supply_min": {"volts": 10.8, "basis": "min"},
                               "input_net": "VIN_F", "input_filter": [fb1]})
    setup, _ = TomachiePdnImporter(pdn, filtered_board()).run()
    assert {k.name: (k.side_a.net, k.side_b.net) for k in setup.links} == \
        {"FB1": ("VIN", "VIN_F")}
    assert by_name(solve_tree(filtered_board(), setup).loads, "U1").passed is True


def test_a_pull_resistor_returns_through_the_part_it_pulls():
    """FB1 standing in for a pull resistor from VIN to U1_1: its return is U1's ground
    pins, not the resistor's (it has none)."""
    pdn = _behind_ferrite_pdn({"role": "pull_resistor", "refdes": "FB1", "pins": ["FB1_1"],
                               "pull_to": "U1_1", "current": {"amps": 0.001, "basis": "v_over_r"}})
    setup, _ = TomachiePdnImporter(pdn, filtered_board()).run()
    load = by_name(setup.loads, "FB1")
    assert load.ground is not None and (load.ground.ref, load.ground.net) == ("U1", "GND")


FB1 = {"refdes": "FB1", "class": "ferrite", "net_in": "VIN", "net_out": "VIN_F",
       "resistance_ohm": 0.05, "datasheet_figures": []}


def test_a_datasheet_supply_range_on_a_board_input_rail_is_the_loads_tolerance():
    """On a rail a board input feeds, the rail's range is the source's own (11 to 13 V):
    the load's datasheet range takes its place, lower and upper end alike."""
    pdn = _behind_ferrite_pdn({"role": "ic", "refdes": "U1", "pins": ["U1_1"],
                               "current": {"amps": 0.2, "basis": "typ"},
                               "supply_min": {"volts": 10.8, "basis": "min"},
                               "supply_max": {"volts": 12.6, "basis": "max"},
                               "input_net": "VIN_F", "input_filter": [FB1]})
    setup, _ = TomachiePdnImporter(pdn, filtered_board()).run()
    u1 = by_name(setup.loads, "U1")
    assert (u1.tol_pct, u1.tol_min, u1.tol_max) == (False, 10.8, 12.6)


def test_an_ic_with_no_stated_supply_range_is_named():
    pdn = _behind_ferrite_pdn({"role": "ic", "refdes": "U1", "pins": ["U1_1"],
                               "current": {"amps": 0.2, "basis": "typ"},
                               "input_net": "VIN_F", "input_filter": [FB1]})
    _, notes = TomachiePdnImporter(pdn, filtered_board()).run()
    assert any(n.startswith("No supply voltage range is stated") and "U1" in n for n in notes)


def test_a_rail_behind_a_placed_output_gets_its_series_part_and_keeps_its_loads():
    """One regulator output named as two rails, the second behind a series part (a
    current-sense shunt): the part becomes a DC connection, in either order of the rails."""
    pdn = copy.deepcopy(PDN)
    behind = {"name": "nRESET", "voltage": {"nominal": 3.3, "basis": "user"},
              "source": {"role": "regulator", "refdes": "U1", "pins": ["U1_2"],
                         "output_net": "+3V3",
                         "output_path": [{"refdes": "R99", "class": "resistor",
                                          "value": "10m", "net_in": "+3V3",
                                          "net_out": "nRESET", "resistance_ohm": 0.01,
                                          "datasheet_figures": []}]},
              "loads": [], "offboard_pins": []}
    pdn["rails"][1]["source"]["output_path"] = []
    for rails in (pdn["rails"] + [behind], [pdn["rails"][0], behind, pdn["rails"][1]]):
        p = dict(pdn, rails=rails)
        setup, notes = run(p)
        assert [v.name for v in setup.vrms if v.kind != "source"] in (["U1 +3V3"],
                                                                      ["U1 nRESET"])
        # Its series part is taken up (this test board has no R99 pads to link it with).
        assert any(n.startswith("R99: series part between +3V3 and nRESET") for n in notes)
        assert any("fed from U1's output +3V3 through R99" in n
                   or "U1's output, placed with another rail it feeds" in n for n in notes)
        assert "U2" in [l.name for l in setup.loads]          # +3V3's load is kept
        assert not any("already drive" in n for n in notes)


def _edge_pdn():
    """VIN_F is a board input one series part (FB1) from its connector J1; U2's load
    on +3V3 is a motor its driver U2 sends off the board at connector J9."""
    return {
        "schema": "tomachie-pdn", "schema_version": 1, "spec_revision": 8,
        "ground_nets": ["GND"],
        "rails": [
            {"name": "VIN_F", "voltage": {"nominal": 12.0, "min": 10.0, "max": 14.0,
                                          "basis": "user"},
             "source": {"role": "board_input", "refdes": "J1", "pins": ["J1_1"],
                        "output_net": "VIN",
                        "output_path": [{"refdes": "FB1", "class": "ferrite",
                                         "net_in": "VIN", "net_out": "VIN_F",
                                         "resistance_ohm": 0.05, "datasheet_figures": []}],
                        "return": {"pins": ["J1_2"], "net": "GND"}},
             "loads": [{"role": "motor_output", "refdes": "J9", "pins": ["J9_1", "J9_2"],
                        "current": {"amps": 2.0, "basis": "user"},
                        "driver": {"refdes": "U1", "supply_pins": ["U1_1"],
                                   "return_pins": ["U1_3"]}}],
             "offboard_pins": []},
        ],
        "regulators": [],
    }


def test_a_board_input_behind_a_series_part_carries_its_current_through_it():
    """The parts between a connector's pin and the rail it feeds are DC connections, and
    the input's stated range is its accuracy."""
    pdn = _edge_pdn()
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), filtered_board()).run()
    [j1] = setup.vrms
    assert j1.name == "J1 VIN_F" and (j1.acc_min, j1.acc_max, j1.acc_pct) == (10.0, 14.0, False)
    assert j1.ground is not None and j1.ground.net == "GND"
    assert "FB1" in [l.name for l in setup.links]
    assert not any("Nothing in the file powers" in n for n in notes)


def test_a_motor_is_drawn_at_its_drivers_pins():
    """A motor's connector terminals sit on the motor phases: its current leaves the rail
    at the driver's supply pins and returns at the driver's ground pins."""
    setup, notes = TomachiePdnImporter(_edge_pdn(), filtered_board()).run()
    motor = by_name(setup.loads, "J9")
    assert motor.supply.ref == "U1" and motor.supply.pins == ["1"]
    assert motor.ground.ref == "U1" and motor.ground.pins == ["3"]
    assert motor.current == pytest.approx(2.0)


def test_a_sources_rated_output_is_its_current_limit():
    """A module's rated current out of the supply it makes limits that source."""
    pdn = _edge_pdn()
    pdn["rails"][0]["source"]["max_output_current"] = {"amps": 0.6, "basis": "max"}
    setup, notes = TomachiePdnImporter(pdn, filtered_board()).run()
    assert by_name(setup.vrms, "J1 VIN_F").current_limit_a == pytest.approx(0.6)


def test_a_regulators_output_rating_and_band():
    """This output's own rating on a part with several outputs, else the part's; the
    rail's regulator band is the VRM's accuracy in volts."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(max_output_current={"amps": 1.5, "basis": "max"},
                                output_rails=["+3V3", "+1V8"])
    pdn["rails"][1]["regulator_band"] = {"grade_c": 85, "at_grade": {"min_v": 3.25, "max_v": 3.35}}
    setup, notes = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert u1.current_limit_a is None                 # two outputs, one figure: not applied
    assert any("no rating for this output" in n for n in notes)
    assert (u1.acc_min, u1.acc_max, u1.acc_pct) == (3.25, 3.35, False)
    pdn["rails"][1]["source"]["max_output_current"] = {"amps": 0.5, "basis": "max"}
    setup, _ = run(pdn)
    assert by_name(setup.vrms, "U1 +3V3").current_limit_a == 0.5


def test_a_driven_led_returns_through_its_driver():
    """An LED a driver's output sinks draws at its own pin on the rail and returns at
    the driver's ground pins; a driver with no supply pins listed moves no supply."""
    pdn = _edge_pdn()
    pdn["rails"][0]["loads"] = [{"role": "resistive_path", "refdes": "U1", "pins": ["U1_1"],
                                 "path_parts": ["U1"], "path_end": "LED_K",
                                 "current": {"amps": 0.0045, "basis": "v_minus_vf_over_r"},
                                 "driver": {"refdes": "U1", "supply_pins": [],
                                            "return_pins": ["U1_3"]}}]
    setup, notes = TomachiePdnImporter(pdn, filtered_board()).run()
    led = by_name(setup.loads, "U1")
    assert led.supply.ref == "U1" and led.supply.pins == ["1"]
    assert led.ground.ref == "U1" and led.ground.pins == ["3"]
    assert led.current == pytest.approx(0.0045)


def test_details_and_unknown_fields_are_skipped():
    """A ``details`` object at any level, and any field the importer does not name, is
    skipped, never a reason to refuse the file."""
    pdn = copy.deepcopy(PDN)
    pdn["details"] = {"process": {"via_plating": {"through_um": 20}}}
    pdn["rails"][1]["details"] = {"worst_case": {"total_current_max": {"amps": 2.0}}}
    pdn["rails"][1]["loads"][0]["details"] = {"die": {"package_ohms": {"1": 0.02}}}
    pdn["rails"][1]["loads"][0]["some_future_field"] = 7
    setup, _ = run(pdn)
    assert [l.name for l in setup.loads] == ["U2"] and validate(setup) == []


@pytest.mark.parametrize("text", ["[]", '{"schema": "tomachie-pdn", "schema_version": 1, '
                                        '"rails": ["VIN"]}',
                                  '{"schema": "tomachie-pdn", "schema_version": 1, '
                                  '"rails": [{"name": "VIN", "source": []}]}'])
def test_a_malformed_file_is_refused_not_a_crash(tmp_path, text):
    """A file with fields of the wrong kind says it is not valid (#137)."""
    path = tmp_path / "bad.pdn.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PdnImportError):
        import_tomachie_pdn(path, board())
