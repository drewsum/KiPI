# Copyright (c) 2026 Tomachie LLC.
"""The AC values of a Tomachie power-tree file (spec revision 9)."""

import copy
import math

import pytest

from kipi.ac.model import CapModel
from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.model import PISetup
from test_tomachie_pdn import PDN, run
from test_tomachie_pdn_budget import _cap_board, _caps_pdn
from tree_boards import by_name


def _ac_pdn():
    pdn = _caps_pdn()
    pdn["spec_revision"] = 9
    ac22 = {"capacitance_f": 22e-6, "rated_v": 6.3, "dielectric": "X5R", "tolerance_pct": 20,
            "esr": {"ohm": 0.004, "basis": "estimated", "from": "typical ceramic ESR"}}
    for c in pdn["rails"][1]["decoupling"]:
        c["ac"] = ac22
    pdn["rails"][1]["loads"][0]["local_caps"][0]["ac"] = {
        "capacitance_f": 1e-7, "rated_v": "unknown", "dielectric": "C0G(NP0)",
        "tolerance_pct": "unknown", "esr": "unknown"}
    pdn["rails"][1]["loads"][0]["ac"] = {"i_step": {"amps": 0.5, "basis": "estimated"}}
    pdn["regulators"][0]["ac"] = {"f_sw": {"hz": 1e6, "basis": "datasheet", "page": 5},
                                  "bandwidth": {"hz": 1e5, "basis": "estimated",
                                                "from": "switching frequency / 10"}}
    return pdn


def test_capacitor_models_take_esr_rating_dielectric_and_tolerance():
    setup, notes = TomachiePdnImporter(_ac_pdn(), _cap_board()).run()
    caps = {m.name: m for m in setup.caps}
    m = caps["PN22U"]
    assert (m.esr_ohm, m.rated_v, m.dielectric, m.mfg_tolerance_pct) == (0.004, 6.3, "X5R", 20)
    assert m.aging_pct_per_decade == pytest.approx(3.0)
    # "unknown" stays unknown; the decoder's C0G(NP0) is KiPI's C0G.
    n = caps["PN100N"]
    assert (n.esr_ohm, n.rated_v, n.dielectric, n.mfg_tolerance_pct) == (0.0, None, "C0G", None)
    assert any("ESR from the file for 1 (1 of them estimates)" in x and "1 have no ESR" in x
               for x in notes)


def test_the_users_capacitor_values_are_kept():
    base = PISetup(caps=[CapModel("PN22U", 20e-6, 0.0, 0.003, ["C1"], rated_v=10.0)])
    setup, _ = TomachiePdnImporter(_ac_pdn(), _cap_board(), base).run()
    m = {x.name: x for x in setup.caps}["PN22U"]
    assert (m.esr_ohm, m.rated_v, m.dielectric) == (0.003, 10.0, "X5R")


def test_vrm_takes_its_bandwidth_and_says_when_estimated():
    setup, notes = run(_ac_pdn())
    assert by_name(setup.vrms, "U1 +3V3").bandwidth_khz == pytest.approx(100.0)
    assert any("VRM bandwidth (AC): estimated for U1 (switching frequency / 10)" in n
               for n in notes)


def test_ic_load_gets_a_probe_without_a_target():
    """A step with no rise time, slew or clock: a probe, but no target, and why."""
    setup, notes = run(_ac_pdn())
    p = by_name(setup.probes, "U2")
    assert (p.rail.net, p.ground.net, p.target_ohm, p.target_hz) == ("+3V3", "GND", None, None)
    assert any(n.startswith("Probe targets (AC): none for 1 probe - no rise time, slew or "
                            "fastest clock:") and n.endswith("\nU2") for n in notes)


def _target_pdn(**ac):
    pdn = _ac_pdn()
    pdn["rails"][1]["loads"][0]["ac"] = ac
    return pdn


def test_a_probe_target_from_the_loads_step_and_clock():
    """Z = V_ac / I_step up to 1 / (pi t_rise), t_rise = 1 / (pi f_max) from the clock."""
    from kipi.ac.target import probe_loads, vrm_accuracies
    setup, notes = run(_target_pdn(i_step={"amps": 0.5, "basis": "datasheet", "page": 9},
                                   f_max={"hz": 50e6, "basis": "estimated"}))
    p = by_name(setup.probes, "U2")
    v_ac = probe_loads(setup, p.rail, p.ground)[0].tolerance.v_ac_v - max(
        (a.v for a in vrm_accuracies(setup, p.rail)), default=0.0)
    assert p.target_ohm == pytest.approx(v_ac / 0.5)
    assert p.target_hz == pytest.approx(50e6)
    assert p.target_from.startswith("step 500 mA (datasheet), fastest clock or edge, "
                                    "estimated")
    assert any(n.startswith("Probe targets (AC): set for 1") for n in notes)
    # Kept in the setup file.
    again = by_name(PISetup.from_dict(setup.to_dict()).probes, "U2")
    assert again.target_from == p.target_from


def test_a_probe_target_from_a_slew_and_per_pin_steps():
    pins = by_name(run(_ac_pdn())[0].probes, "U2").rail.pins
    setup, _ = run(_target_pdn(
        i_step={"amps": 0.9, "basis": "datasheet"},
        i_step_by_pin=[{"pin": f"U2_{pins[0]}", "amps": 0.2, "basis": "datasheet"},
                       {"pin": "U2_999", "amps": 0.7, "basis": "datasheet"}],
        slew={"amps_per_s": 2e7, "basis": "datasheet"}))
    p = by_name(setup.probes, "U2")
    assert p.target_from.startswith("step 200 mA (datasheet), slew, datasheet")
    assert p.target_hz == pytest.approx(1 / (3.141592653589793 * 0.2 / 2e7))


def test_a_module_with_its_own_regulators_needs_no_step():
    setup, notes = run(_target_pdn(i_step="unknown", i_step_because="module"))
    p = by_name(setup.probes, "U2")
    said = ("Module carries its own regulators. Module doesn't publish an i_step as it has "
            "its own regulators, designer only needs to supply steady input.")
    assert (p.target_ohm, p.target_from) == (None, said)
    assert f"Probe targets (AC): not needed for U2 - {said}" in notes


def test_the_makers_own_target_wins_and_no_step_says_why():
    setup, _ = run(_target_pdn(target={"ohm": 0.02, "hz": 2e7, "basis": "datasheet",
                                       "page": 41},
                               i_step={"amps": 0.5, "basis": "datasheet"},
                               f_max={"hz": 50e6, "basis": "estimated"}))
    p = by_name(setup.probes, "U2")
    assert (p.target_ohm, p.target_hz, p.target_from) == (0.02, 2e7,
                                                          "the maker's target, page 41")
    _, notes = run(_target_pdn(i_step="unknown", i_step_because="no_dynamic_figure"))
    assert any("none for 1 probe - no current step: its datasheet prints no dynamic "
               "current:\nU2" in n for n in notes)


def test_a_file_without_ac_values_imports_as_before():
    setup, notes = run(PDN)
    assert setup.probes == [] and by_name(setup.vrms, "U1 +3V3").bandwidth_khz is None
    assert not any("(AC): estimated" in n or "Probes (AC)" in n for n in notes)


def test_an_existing_probe_is_kept():
    from kipi.ac.model import Probe
    from kipi.dc.pi.model import PinRef
    mine = Probe("U2", PinRef("U2", "+3V3", ["1"]), PinRef("U2", "GND", ["2"]), 0.05, 1e6)
    pdn = copy.deepcopy(_ac_pdn())
    setup, _ = TomachiePdnImporter(pdn, _cap_board(), PISetup(probes=[mine])).run()
    assert [p.target_ohm for p in setup.probes] == [0.05]


def test_capacitors_without_a_part_number_group_by_value_and_esl():
    pdn = _ac_pdn()
    for c in pdn["rails"][1]["decoupling"]:
        c.pop("part_number")
    setup, _ = TomachiePdnImporter(pdn, _cap_board()).run()
    m = {x.name: x for x in setup.caps}["22uF/6.3V, 0.4 nH (no part number)"]
    assert (m.refs, m.esr_ohm, m.capacitance_f) == (["C1", "C2"], 0.004, pytest.approx(22e-6))


def test_without_ac_values_a_capacitor_needs_its_part_number():
    pdn = copy.deepcopy(_caps_pdn())
    pdn["rails"][1]["decoupling"][0].pop("part_number")
    setup, _ = TomachiePdnImporter(pdn, _cap_board()).run()
    assert {x.name: x.refs for x in setup.caps}["PN22U"] == ["C2"]


def test_the_parts_own_curves_give_its_esl_and_dc_bias():
    pdn = _ac_pdn()
    for c in pdn["rails"][1]["decoupling"]:
        c["ac"] = dict(c["ac"], esl={"h": 2.5e-10, "basis": "graph", "page": 3},
                       kept_at_rated={"pct": 35.0, "basis": "graph", "page": 3})
    setup, _ = TomachiePdnImporter(pdn, _cap_board()).run()
    m = {x.name: x for x in setup.caps}["PN22U"]
    assert (m.esl_h, m.kept_at_rated_pct) == (2.5e-10, 35.0)
    # A value the user entered stays.
    base = PISetup(caps=[CapModel("PN22U", 22e-6, 3e-10, 0.003, ["C1"], kept_at_rated_pct=50.0)])
    setup, _ = TomachiePdnImporter(pdn, _cap_board(), base).run()
    m = {x.name: x for x in setup.caps}["PN22U"]
    assert (m.esl_h, m.kept_at_rated_pct) == (3e-10, 50.0)


def test_capacitors_on_defaults_are_named_with_why():
    pdn = _ac_pdn()
    pdn["ac_defaults"] = [{"because": "no_part_number", "refdes": ["C1", "C2"]}]
    _, notes = run(pdn)
    note = next(n for n in notes if n.startswith("Capacitor AC values"))
    # The count and the reason first, the capacitors on the next line.
    first, refs = note.split("\n")
    assert "defaults are used for 2 capacitors" in first
    assert ("the schematic does not use a part number so a datasheet could not be "
            "looked up") in first
    assert refs == "C1, C2"


def test_a_value_read_off_a_graph_is_rounded_to_four_figures():
    from kipi.dc.pi.importers.tomachie_ac import ac_value
    assert ac_value({"h": 1.075330938e-9, "basis": "graph"}, "h") == (1.075e-9, "graph")
    assert ac_value({"ohm": 0.0058331, "basis": "estimated"}, "ohm") == (0.0058331,
                                                                         "estimated")


def test_capacitor_models_say_where_their_values_came_from():
    pdn = _ac_pdn()
    for c in pdn["rails"][1]["decoupling"]:
        c["ac"] = dict(c["ac"], defaults_because="no_part_number")
    setup, _ = TomachiePdnImporter(pdn, _cap_board()).run()
    m = {x.name: x for x in setup.caps}["PN22U"]
    assert m.values_from == {"C": "schematic", "ESL": "estimated", "ESR": "estimated"}
    assert m.values_from_text() == "estimate - no part number"
    m.values_from["ESL"] = "graph"
    assert m.values_from_text() == "ESR estimate, ESL graph - no part number"
    assert m.values_from_detail() == ("capacitance schematic, ESL graph, ESR estimate, "
                                      "estimates because: no part number")
    # The maker's measured S-parameter file; a basis KiPI does not know reads as written.
    m.values_from.update(ESL="maker_model", ESR="maker_model")
    assert m.values_from_text() == "maker's measured file"
    m.values_from["ESR"] = "some_new_basis"
    assert m.values_from_text() == "ESR some new basis, ESL maker's measured file"
    # Kept in the setup file.
    again = PISetup.from_dict(setup.to_dict())
    m2 = {x.name: x for x in again.caps}["PN22U"]
    assert (m2.values_from, m2.defaults_because) == (m.values_from, "no_part_number")
    # A model without all three values says so.
    assert {x.name: x for x in setup.caps}["PN100N"].values_from_text() == "values needed"


def test_an_esl_the_maker_does_not_publish_is_marked_as_a_substitute():
    """A molded capacitor's ESL from a case table: the model says it is not the maker's,
    and the notes name the parts and the source the file gives."""
    pdn = _ac_pdn()
    for c in pdn["rails"][1]["decoupling"]:
        c["esl"] = {"h": 3e-9, "basis": "estimated", "because": "not_published_by_maker",
                    "from": "molded case 7343: NIC Components NTC-T datasheet ESL by case"}
        c["ac"] = dict(c["ac"], esr={"ohm": 0.015, "basis": "datasheet", "page": 3})
    setup, notes = TomachiePdnImporter(pdn, _cap_board()).run()
    m = {x.name: x for x in setup.caps}["PN22U"]
    assert (m.esl_h, m.defaults_because) == (pytest.approx(3e-9), "not_published_by_maker")
    assert m.values_from_text() == "ESR datasheet, ESL estimate - not published by its maker"
    assert "estimates because: not published by its maker" in m.values_from_detail()
    note = next(n for n in notes if n.startswith("Capacitor ESL (AC)"))
    assert "PN22U (" in note and "3 nH, molded case 7343: NIC Components NTC-T" in note
    # A case estimate without that mark is an estimate and nothing more.
    _, plain = run(_ac_pdn())
    assert not any(n.startswith("Capacitor ESL (AC)") for n in plain)


def test_an_equivalent_parts_file_and_a_maker_figure_not_trusted():
    """An equivalent part's measured file is named as that part's, never this one's; a
    maker's figure that is used but doubted is marked and said why, its value kept."""
    pdn = _ac_pdn()
    doubt = "the maker's figure gives ESL 1.76 nH; the equivalent's measured file 0.67 nH"
    for c in pdn["rails"][1]["decoupling"]:
        c["ac"] = dict(c["ac"], esr={"ohm": 0.004, "basis": "equivalent_maker_model"},
                       esl={"h": 1.76e-9, "basis": "graph"}, doubt=doubt,
                       maker_model={"status": "equivalent",
                                    "equivalent": "TDK C1608X7R1E104K080AA"})
    setup, notes = TomachiePdnImporter(pdn, _cap_board()).run()
    m = {x.name: x for x in setup.caps}["PN22U"]
    assert (m.esl_h, m.esr_ohm) == (1.76e-9, 0.004)
    assert m.values_from_text() == ("ESR measured file of an equivalent part (TDK "
                                    "C1608X7R1E104K080AA), ESL graph - questionable")
    assert m.values_from_detail().endswith(f"questionable: {doubt}")
    note = next(n for n in notes if "questionable" in n)
    first, line = note.split("\n")
    assert first.endswith("questionable for 1 capacitor model:")
    assert line == f"PN22U (2 parts): {doubt}"
    again = {x.name: x for x in PISetup.from_dict(setup.to_dict()).caps}["PN22U"]
    assert (again.equivalent, again.doubt) == ("TDK C1608X7R1E104K080AA", doubt)


def test_a_rail_with_selectable_levels_takes_its_default_and_keeps_the_others():
    """A rail a pin or firmware sets to one of several levels (an SD card's 1.8 / 3.3 V
    I/O supply): solved at its power-up default, else its lowest level; the others are
    kept for the VRM editor and said in the notes."""
    pdn = _ac_pdn()
    rail = pdn["rails"][1]
    rail["voltage"] = {"nominal": "not_specified", "basis": "not_stated", "levels": [1.8, 3.3]}
    setup, notes = run(pdn)
    v = next(x for x in setup.vrms if x.output.net == rail["name"])
    assert (v.voltage, v.levels) == (1.8, [1.8, 3.3])
    assert any("solved at 1.8 V (the lowest of its levels); it also runs at 3.3 V" in n
               for n in notes)
    rail["voltage"]["default_level"] = {"volts": 3.3, "basis": "pull_up", "from": "R97"}
    setup, notes = run(pdn)
    v = next(x for x in setup.vrms if x.output.net == rail["name"])
    assert v.voltage == 3.3
    assert any("solved at 3.3 V (its power-up default); it also runs at 1.8 V" in n
               for n in notes)
    again = next(x for x in PISetup.from_dict(setup.to_dict()).vrms if x.name == v.name)
    assert again.levels == [1.8, 3.3]


def _buck_pdn():
    pdn = _ac_pdn()
    pdn["rails"][0]["voltage"].update(min=10.8, max=13.2)
    pdn["regulators"][0].update(type="switching", efficiency=0.9,
                                output_current={"amps": 1.0, "basis": "rollup"},
                                input_voltage_min={"volts": 4.5, "basis": "min"},
                                input_voltage_max={"volts": 17.0, "basis": "max"})
    return pdn


def test_a_switching_converters_input_gets_a_stability_and_a_ripple_probe():
    from kipi.ac.target import target_at
    setup, notes = TomachiePdnImporter(_buck_pdn(), _cap_board()).run()
    s = by_name(setup.probes, "U1 +3V3 input (stability)")
    r = by_name(setup.probes, "U1 +3V3 input (ripple)")
    assert (s.rail.net, s.ground.net, r.rail.net) == ("VIN", "GND", "VIN")
    p_in = 3.3 * 1.0 / 0.9
    assert s.target_ohm == pytest.approx(10.8 ** 2 / p_in / 2)
    assert s.target_hz == pytest.approx(1e5)
    d = 3.3 / (0.9 * 10.8)
    i1 = 2 / math.pi * math.sin(math.pi * d)
    z1 = (17.0 - 13.2) / i1
    assert r.target_custom and r.target_from_hz == pytest.approx(1e6)
    assert r.target_points[0] == (pytest.approx(1e6), pytest.approx(z1))
    t = target_at([5e5, 1e6, 1e7], points=r.target_mask(), from_hz=r.target_from_hz)
    assert math.isnan(t[0]) and t[1] == pytest.approx(z1) and t[2] == pytest.approx(10 * z1)
    assert any("U1 +3V3 input (ripple)" in n for n in notes if n.startswith("Probe targets"))


def test_a_linear_regulators_input_gets_no_probe():
    setup, _ = run(_ac_pdn())
    assert not [p for p in setup.probes if "input" in p.name]


def test_a_converter_slower_than_its_board_input_gets_no_stability_target():
    pdn = _buck_pdn()
    pdn["regulators"][0]["ac"]["bandwidth"] = {"hz": 2e4, "basis": "estimated", "from": "f_sw / 10"}
    setup, notes = TomachiePdnImporter(pdn, _cap_board()).run()
    s = by_name(setup.probes, "U1 +3V3 input (stability)")
    assert s.target_ohm is None
    assert any("comes from a board input (J1 VIN), taken to hold the rail up to 50 kHz" in n
               and n.endswith("U1 +3V3 input (stability)") for n in notes)
