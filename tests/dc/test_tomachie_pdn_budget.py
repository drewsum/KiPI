# Copyright (c) 2026 Tomachie LLC.
"""A power-tree file's worst-case budget fields: a regulator's output band, a load's
maximum supply and pre-layout copper margin, ESL, a 0 ohm jumper's resistance and a
part that draws what it drives."""

import copy

import pytest
from shapely.geometry import box

from kipi.ac.model import CapModel
from kipi.dc.pi.importers.pdn_figures import dc_resistance
from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.model import PISetup
from kipi.dc.pi.tree import solve_tree
from test_tomachie_pdn import PDN, run
from tree_boards import board, by_name, pad

BAND = {"basis": "divider",
        "at_25c": {"min_v": 3.23, "max_v": 3.37, "page": 6},
        "at_grade": {"min_v": 3.2, "max_v": 3.4, "page": 6},
        "grade_c": 85.0, "divider_tolerance_pct": {"top": 1.0, "bottom": 1.0}}


def with_band(band=BAND):
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["regulator_band"] = copy.deepcopy(band)
    return pdn


def test_regulator_band_is_the_vrms_accuracy_at_the_temperature_grade():
    setup, notes = run(with_band())
    u1 = by_name(setup.vrms, "U1 +3V3")
    assert (u1.acc_min, u1.acc_max, u1.acc_pct, u1.acc_dv) == (3.2, 3.4, False, False)
    assert any(n.startswith("Output accuracy from the datasheets") and "3.2 to 3.4 V at 85 C"
               in n for n in notes)


def test_band_at_25c_when_no_grade_band():
    band = copy.deepcopy(BAND)
    del band["at_grade"]
    u1 = by_name(run(with_band(band))[0].vrms, "U1 +3V3")
    assert (u1.acc_min, u1.acc_max) == (3.23, 3.37)


def test_band_not_known_is_said():
    setup, notes = run(with_band({"basis": "divider",
                                  "not_known_because": ["no reference accuracy"]}))
    assert not by_name(setup.vrms, "U1 +3V3").has_accuracy()
    assert any("output accuracy not known: no reference accuracy" in n for n in notes)


def test_worst_case_runs_the_vrm_at_the_low_end_of_its_band():
    """The worst case solves U1 at 3.2 V, the low end of its band, and U2 is still judged
    against the rail's 3.135 V minimum (set from 3.3 V), not against one moved down."""
    setup, _ = run(with_band())
    typ = by_name(solve_tree(board(), setup).loads, "U2")
    setup.corner = "max"
    res = solve_tree(board(), setup)
    worst = by_name(res.loads, "U2")
    assert typ.nominal == pytest.approx(3.3) and worst.nominal == pytest.approx(3.2)
    assert worst.limit_low == pytest.approx(3.135) and typ.limit_low == pytest.approx(3.135)
    assert worst.v_low < typ.v_low
    assert worst.margin_low == pytest.approx(worst.v_low - 3.135)
    assert any("low end of their DC accuracy: U1 +3V3 3.2 V (set 3.3 V)" in n
               for n in res.warnings)
    assert any("no DC accuracy is given for these VRMs" in n and "J1 VIN" in n
               for n in res.warnings)


def test_a_maximum_equal_to_its_typical_but_for_rounding_solves():
    """A file can state one figure twice through different arithmetic; the worst case
    solves it rather than calling the maximum below the typical."""
    pdn = copy.deepcopy(PDN)
    load = pdn["rails"][1]["loads"][0]
    load["current"] = {"amps": 1.4000000000000001, "basis": "typ"}
    load["current_max"] = {"amps": 1.4, "basis": "estimated"}
    setup, _ = run(pdn)
    setup.corner = "max"
    assert by_name(solve_tree(board(), setup).loads, "U2").current == pytest.approx(1.4)


def test_a_load_with_no_stated_supply_range_is_not_judged():
    """The rail's range is its source's: a load stating none of its own has no window
    (a worst case at the source's minimum would fail it at any copper drop); an IC so
    left is named."""
    pdn = copy.deepcopy(PDN)
    load = pdn["rails"][1]["loads"][0]
    del load["supply_min"]
    pdn["rails"][1]["loads"].append({"role": "pull_resistor", "refdes": "R5",
                                     "pins": ["U2_1"],
                                     "current": {"amps": 0.001, "basis": "typ"}})
    setup, notes = run(pdn)
    for name in ("U2", "R5"):
        assert not by_name(setup.loads, name).has_tolerance()
    note = next(n for n in notes if n.startswith("No supply voltage range is stated"))
    assert note.endswith(": U2")


def test_on_a_board_input_rail_the_datasheet_range_is_the_loads():
    """A board input's stated range is the source's, the voltage the worst case solves it
    at: the load is judged against its datasheet's 3.0 V, not the input's 3.135 V."""
    pdn = copy.deepcopy(PDN)
    rail = pdn["rails"][1]
    rail["source"] = {"role": "board_input", "refdes": "J2", "pins": ["U1_2"],
                      "output_net": "+3V3", "output_path": []}
    u2 = by_name(run(pdn)[0].loads, "U2")
    assert (u2.tol_pct, u2.tol_min, u2.tol_max) == (False, 3.0, 3.465)


def test_an_outputs_own_rating_comes_before_the_parts():
    """A part with several outputs rates each: the rail source's max_output_current is
    the limit of the output making it, before the regulator's single figure."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0]["max_output_current"] = {"amps": 0.5, "basis": "max"}
    u1 = by_name(run(pdn)[0].vrms, "U1 +3V3")
    assert u1.current_limit_a == 0.5
    pdn["rails"][1]["source"]["max_output_current"] = {"amps": 4.0, "basis": "typ",
                                                       "chosen_by": "tool"}
    u1 = by_name(run(pdn)[0].vrms, "U1 +3V3")
    assert u1.current_limit_a == 4.0
    # Several outputs, none rated for this one: the part's figure is not applied.
    del pdn["rails"][1]["source"]["max_output_current"]
    pdn["regulators"][0]["output_rails"] = ["+3V3", "+1V8"]
    setup, notes = run(pdn)
    assert by_name(setup.vrms, "U1 +3V3").current_limit_a is None
    assert any("no rating for this output of a part with several" in n for n in notes)


def test_supply_max_tightens_the_rails_maximum():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["supply_max"] = {"volts": 3.4, "basis": "max",
                                                 "chosen_by": "tool"}
    u2 = by_name(run(pdn)[0].loads, "U2")
    # The rail's minimum (3.135 V) is tighter than the part's (3.0 V); its maximum is not.
    assert (u2.tol_pct, u2.tol_min, u2.tol_max) == (False, 3.135, 3.4)


def test_copper_margin_is_kept_and_out_of_range_loads_are_named():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["copper_margin"] = {
        "v": -0.05, "worst_supply_v": 2.95, "supply_min_v": 3.0, "series_drop_v": 0.0,
        "low_end_basis": "band", "high_room_v": -0.02,
        "returns": "supply copper and ground return together"}
    setup, notes = run(pdn)
    assert by_name(setup.loads, "U2").copper_margin_v == -0.05
    out = next(n for n in notes if n.startswith("Out of their supply range before any copper"))
    assert "U2 (50 mV below its minimum)" in out and "20 mV above its maximum" in out
    res = solve_tree(board(), setup)
    assert by_name(res.loads, "U2").copper_margin == -0.05
    assert PISetup.from_dict(setup.to_dict()).loads[0].copper_margin_v == -0.05


def test_copper_margin_not_known_is_none():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["copper_margin"] = {"not_known_because": ["no minimum"]}
    assert by_name(run(pdn)[0].loads, "U2").copper_margin_v is None


def _cap_board():
    snap = board()
    snap.pads += [pad("c1_1", "+3V3", "C1", "1", box(15, 0, 16, 1)),
                  pad("c2_1", "+3V3", "C2", "1", box(17, 0, 18, 1)),
                  pad("c3_1", "+3V3", "C3", "1", box(19, 0, 20, 1))]
    return snap


def _caps_pdn():
    pdn = copy.deepcopy(PDN)
    esl = {"h": 4e-10, "basis": "estimated"}
    pdn["rails"][1]["decoupling"] = [
        {"refdes": "C1", "value": "22uF/6.3V", "part_number": "PN22U", "esl": esl},
        {"refdes": "C2", "value": "22uF/6.3V", "part_number": "PN22U", "esl": esl},
        {"refdes": "C9", "value": "1uF", "part_number": "PN1U", "esl": esl}]   # not on board
    pdn["rails"][1]["loads"][0]["local_caps"] = [
        {"refdes": "C3", "value": "100nF", "part_number": "PN100N", "esl": "unknown"}]
    return pdn


def test_capacitors_become_one_model_per_part_number():
    setup, notes = TomachiePdnImporter(_caps_pdn(), _cap_board()).run()
    caps = {m.name: m for m in setup.caps}
    assert sorted(caps) == ["PN100N", "PN22U"]
    assert (caps["PN22U"].capacitance_f, caps["PN22U"].esl_h, caps["PN22U"].refs) == (
        pytest.approx(22e-6), 4e-10, ["C1", "C2"])
    assert (caps["PN100N"].capacitance_f, caps["PN100N"].esl_h) == (pytest.approx(1e-7), 0.0)
    assert any("Capacitor models (AC): 2 added and 0 filled in" in n
               and "2 have no ESR" in n for n in notes)


def test_capacitor_models_keep_what_the_user_set():
    """A model of the same name keeps its values and only gets what it lacks; a capacitor
    the user put in another model stays there."""
    base = PISetup(caps=[CapModel("PN22U", 20e-6, 0.0, 0.003, ["C1"]),
                         CapModel("Mine", 1e-7, 3e-10, 0.01, ["C3"])])
    setup, _ = TomachiePdnImporter(_caps_pdn(), _cap_board(), base).run()
    caps = {m.name: m for m in setup.caps}
    assert (caps["PN22U"].capacitance_f, caps["PN22U"].esl_h, caps["PN22U"].esr_ohm) == (
        20e-6, 4e-10, 0.003)
    assert caps["PN22U"].refs == ["C1", "C2"]
    assert caps["Mine"].refs == ["C3"] and caps["PN100N"].refs == []


def test_a_series_parts_esl_is_its_connections():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["source"]["output_path"] = []
    pdn["rails"][1]["series_from_parent"] = [
        {"refdes": "U1", "class": "resistor", "value": "0R", "net_in": "VIN",
         "net_out": "+3V3", "resistance_ohm": 0.01, "datasheet_figures": [],
         "esl": {"h": 6e-10, "basis": "estimated"}}]
    setup, _ = run(pdn)
    assert by_name(setup.links, "U1").esl_h == 6e-10


def test_a_0_ohm_jumpers_maximum_is_its_jumper_resistance():
    figs = [{"spec": "jumper_resistance", "max": "20", "unit": "mohm"},
            {"spec": "resistance", "max": "100M", "unit": "ohm"}]
    assert dc_resistance({"resistance_ohm": 0.0, "datasheet_figures": figs})[:2] == (
        0.0, pytest.approx(0.02))
    # Without a jumper figure it is a short; another "resistance" figure is never taken.
    assert dc_resistance({"resistance_ohm": 0.0,
                          "datasheet_figures": figs[1:]})[:2] == (0.0, None)


def test_a_dc_resistance_named_after_its_part_is_taken():
    figs = [{"spec": "BLM18PG121SH1_dc_resistance", "max": "0.05", "unit": "ohm"},
            {"spec": "BLM18PG121SH1_impedance_100MHz", "typ": "120", "unit": "ohm"},
            {"spec": "insulation_resistance", "typ": "10", "unit": "Mohm"}]
    assert dc_resistance({"resistance_ohm": "unknown", "datasheet_figures": figs})[:2] == (
        None, pytest.approx(0.05))


def test_a_series_datasheets_figures_for_other_parts_are_left_out():
    figs = [{"spec": "BLM18PG121SH1_dc_resistance", "max": "0.05", "unit": "ohm"},
            {"spec": "BLM18PG221SN1_dc_resistance", "max": "0.10", "unit": "ohm"}]
    el = {"resistance_ohm": "unknown", "part_number": "BLM18PG121SH1D",
          "datasheet_figures": figs}
    assert dc_resistance(el) == (None, pytest.approx(0.05), [])
    # Equal figures under two names are one figure, not "several".
    same = [{"spec": "dc_resistance", "max": "50", "unit": "mohm"},
            {"spec": "dcr", "max": "50", "unit": "mohm"}]
    assert dc_resistance({"resistance_ohm": "unknown", "datasheet_figures": same})[2] == []


def test_a_part_that_draws_what_it_drives_solves_its_own_current():
    """A level shifter's datasheet current is static: the least it draws. It is solved,
    never 0 A; what it drives on top is not known, so its rail gets a headroom."""
    pdn = copy.deepcopy(PDN)
    load = pdn["rails"][1]["loads"][0]
    load.update(current={"amps": 6e-6, "basis": "max"}, load_dependent=True,
                load_dependent_reason="static figure; switching current follows signal "
                                      "activity and loads")
    del load["current_max"]
    setup, notes = run(pdn)
    u2 = by_name(setup.loads, "U2")
    assert (u2.current, u2.current_known) == (6e-6, False)
    assert any(n.startswith("Draw what they drive") and "U2 (6 µA)" in n for n in notes)
    assert not any(n.startswith("Current not known") for n in notes)
    tree = solve_tree(board(), setup)
    assert by_name(tree.loads, "U2").status == ""
    assert [h.unknown for h in tree.headroom] == [["U2"]]
