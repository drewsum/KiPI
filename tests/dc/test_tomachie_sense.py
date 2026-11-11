# Copyright (c) 2026 Tomachie LLC.
"""A regulator's ``source.sense`` in the Tomachie file becomes the VRM's remote sense:
the pads it holds at the set voltage (its own sense pins on the rail, or the divider's
top pads named by ``at``) and, with a simulated ground, its return-sense pads."""

import copy

from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from tree_boards import board, by_name

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
         "offboard_pins": [], "ground": "GND"},
        {"name": "+3V3", "voltage": {"nominal": 3.3, "min": 3.135, "max": 3.465,
                                     "basis": "user"},
         "source": {"role": "regulator", "refdes": "U1", "pins": ["U1_2"],
                    "output_net": "+3V3", "output_path": [],
                    # The feedback comes off a divider whose top resistor pad is U2's pad
                    # on the rail (the test board has no resistor): that pad is held.
                    "sense": {"pins": ["U1_9"], "pin_names": ["FB"], "net": "FB",
                              "at": {"pins": ["U2_1"], "net": "+3V3"},
                              "return": {"pins": ["U2_2"], "net": "GND"}}},
         "loads": [{"role": "ic", "refdes": "U2", "pins": ["U2_1"],
                    "current": {"amps": 1.0, "basis": "typ"}}],
         "offboard_pins": [], "ground": "GND"},
    ],
    "regulators": [{"refdes": "U1", "type": "linear", "efficiency": "unknown",
                    "input_rails": ["VIN"], "output_rails": ["+3V3"]}],
}


def run(pdn):
    return TomachiePdnImporter(copy.deepcopy(pdn), board()).run()


def test_sense_point_and_return_sense_land_on_the_vrm():
    setup, notes = run(PDN)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.sense.ref, u1.sense.net, u1.sense.pins) == ("U2", "+3V3", ["1"])
    assert (u1.ground_sense.ref, u1.ground_sense.net, u1.ground_sense.pins) == ("U2", "GND", ["2"])
    assert any("remote sense at U2[1] (FB) on +3V3, return sense at U2[2]" in n for n in notes)


def test_sense_pins_on_the_rail_itself():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["source"]["sense"] = {"pins": ["U2_1"], "pin_names": ["VSNS"], "net": "+3V3"}
    setup, _ = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert u1.sense.pins == ["1"] and u1.ground_sense is None


def test_sense_off_the_output_net_is_refused_and_said():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["source"]["sense"] = {"pins": ["U1_1"], "pin_names": ["FB"], "net": "VIN"}
    setup, notes = run(pdn)
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert u1.sense is None and u1.ground_sense is None
    assert any("U1_1 on VIN are not on its rail" in n for n in notes)


def test_no_sense_block_changes_nothing():
    pdn = copy.deepcopy(PDN)
    del pdn["rails"][1]["source"]["sense"]
    setup, _ = run(pdn)
    assert by_name(setup.vrms, "U1 +3V3").sense is None
