# Copyright (c) 2026 Tomachie LLC.
"""Figures the engineer states for a headless run are merged into the pdn.json in its
own shape, basis user, and a name the file does not hold is reported."""

import copy

from kipi.dc.pi.importers.pdn_overrides import apply_overrides
from kipi.dc.pi.importers.tomachie_ac import load_rise, load_step, vrm_bandwidth_khz

PDN = {
    "rails": [{"name": "P1V2", "loads": [{"refdes": "U1", "ac": {"i_step": "unknown",
                                                                  "i_step_because": "no_datasheet"}}]}],
    "regulators": [{"refdes": "IC30", "ac": {"f_sw": "unknown", "bandwidth": "unknown"}},
                   {"refdes": "IC22"}],
}

STATED = "stated by the engineer (overrides file)"


def test_stated_figures_land_in_the_files_shape():
    pdn = copy.deepcopy(PDN)
    applied, unmatched = apply_overrides(pdn, {
        "regulators": {"IC30": {"f_sw_hz": 800e3, "bandwidth_hz": 80e3}, "IC22": {"bandwidth_hz": 50e3}},
        "loads": {"P1V2:U1": {"i_step_a": 0.7, "t_rise_s": 1e-6,
                              "supply_min_v": 1.14, "supply_max_v": 1.26}}})
    assert unmatched == []
    assert len(applied) == 7
    ic30 = pdn["regulators"][0]
    assert ic30["ac"]["f_sw"] == {"hz": 800e3, "basis": "user", "from": STATED}
    assert vrm_bandwidth_khz(ic30) == (80.0, "user")
    assert vrm_bandwidth_khz(pdn["regulators"][1]) == (50.0, "user")
    u1 = pdn["rails"][0]["loads"][0]
    assert load_step(u1) == (0.7, "user")
    assert "i_step_because" not in u1["ac"]
    assert load_rise(u1, 0.7) == (1e-6, "rise time, user")
    assert u1["supply_min"] == {"volts": 1.14, "basis": "user", "from": STATED}
    assert u1["supply_max"]["volts"] == 1.26


def test_a_name_the_file_does_not_hold_is_reported():
    pdn = copy.deepcopy(PDN)
    applied, unmatched = apply_overrides(pdn, {"regulators": {"IC99": {"bandwidth_hz": 1}},
                                               "loads": {"P3V3:U1": {"i_step_a": 1}}})
    assert applied == []
    assert unmatched == ["regulator IC99", "load P3V3:U1"]


def test_zero_or_missing_values_change_nothing():
    pdn = copy.deepcopy(PDN)
    applied, unmatched = apply_overrides(pdn, {"regulators": {"IC30": {"bandwidth_hz": 0, "f_sw_hz": None}}})
    assert (applied, unmatched) == ([], [])
    assert pdn == PDN
