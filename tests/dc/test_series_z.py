# Copyright (c) 2026 Tomachie LLC.
import copy
import sys
from pathlib import Path

import numpy as np
import pytest

from kipi.dc.pi import series_z
from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.model import PISetup, biased_link

sys.path.insert(0, str(Path(__file__).parent))
from test_tomachie_pdn import _load_switch, filtered_board  # noqa: E402
from tree_boards import by_name  # noqa: E402

W = 2 * np.pi
POINTS = [[0.0, 0.05, 0.0], [1e6, 0.06, 6.0], [1e8, 300.0, 100.0], [1e9, 200.0, -50.0]]
AT_1A = [[0.0, 0.06, 0.0], [1e6, 0.07, 2.0], [1e8, 60.0, 20.0], [1e9, 150.0, -40.0]]


def test_table_on_between_below_and_above_its_points():
    z = series_z.table_impedance(POINTS, W * np.array([1e6, 1e7, 5e5, 2e9]))
    assert z[0] == pytest.approx(0.06 + 6j)
    assert z[1].real == pytest.approx((0.06 + 300.0) / 2)
    assert z[1].imag == pytest.approx((6.0 + 100.0) / 2)
    assert z[2] == pytest.approx(0.055 + 3j)
    assert z[3] == pytest.approx(200.0 - 50j)


def test_curves_blend_by_current_and_hold_beyond():
    curves = [{"a": 0.0, "points": POINTS}, {"a": 1.0, "points": AT_1A}]
    w = W * np.array([1e8])
    assert series_z.by_current_impedance(curves, 0.5, w)[0] == pytest.approx(180.0 + 60j)
    assert series_z.by_current_impedance(curves, 3.0, w)[0] == pytest.approx(60.0 + 20j)


def test_circuit_at_dc_and_resonance():
    c = {"rs_ohm": 0.0247, "l_h": 14.1e-6, "rp_ohm": 21982.0, "cp_f": 6.761e-12}
    srf = 1 / (2 * np.pi * np.sqrt(c["l_h"] * c["cp_f"]))
    z = series_z.circuit_impedance(c, W * np.array([1.0, srf]))
    assert z[0].real == pytest.approx(0.0247, rel=1e-3)
    assert abs(z[1]) == pytest.approx(21982.0, rel=0.01)


def _ferrite(ac):
    pdn = _load_switch(datasheet_resistances=[{"spec": "rds_on", "typ": "0.089", "unit": "ohm"}])
    pdn["rails"][0]["loads"][0].update(input_net="VIN_F", input_filter=[{
        "refdes": "FB1", "class": "ferrite", "net_in": "VIN", "net_out": "VIN_F",
        "resistance_ohm": "unknown", "datasheet_figures": [], "ac": ac}])
    return TomachiePdnImporter(copy.deepcopy(pdn), filtered_board()).run()


def _measured():
    return {"basis": "maker_model", "element": "ferrite", "dcr_ohm": 0.05,
            "z_points": {"columns": ["hz", "r_ohm", "x_ohm"], "points": POINTS},
            "measured_at": {"dc_current_a": 0, "temperature_c": 25}}


def test_measured_curve_is_imported_and_used():
    setup, notes = _ferrite(_measured())
    fb1 = by_name(setup.links, "FB1")
    assert fb1.resistance == pytest.approx(0.05)
    assert fb1.impedance(W * np.array([1e6]))[0] == pytest.approx(0.06 + 6j)
    assert any("FB1" in n and "measured curve" in n for n in notes)


def test_curves_by_current_follow_the_solved_current():
    ac = _measured()
    ac["z_dc_bias"] = {"columns": ["dc_current_a", "dcr_ohm", "z_100mhz_ohm", "peak_hz", "peak_ohm"],
                       "points": [[0, 0.05, 316, 1e8, 316], [1, "unknown", 63, 1e9, 156]],
                       "curves": {"columns": ["hz", "r_ohm", "x_ohm"],
                                  "by_current": [{"dc_current_a": 0, "points": POINTS},
                                                 {"dc_current_a": 1, "points": AT_1A}]}}
    setup, _ = _ferrite(ac)
    fb1 = by_name(setup.links, "FB1")
    biased = biased_link(setup, fb1, {"FB1": -1.0})
    assert biased.bias_a == pytest.approx(1.0)
    assert biased.impedance(W * np.array([1e8]))[0] == pytest.approx(60.0 + 20j)


def test_equivalent_circuit_is_imported_with_an_unknown_temperature():
    ac = {"basis": "maker_equivalent_circuit", "element": "inductor", "dcr_ohm": 0.032,
          "z_points": {"points": POINTS},
          "measured_at": {"dc_current_a": 0, "temperature_c": "unknown"},
          "equivalent_circuit": {"topology": "(rs + l) || rp || cp", "rs_ohm": 0.032,
                                 "l_h": 1e-6, "rp_ohm": 936.0, "cp_f": 9.88e-12}}
    setup, _ = _ferrite(ac)
    fb1 = by_name(setup.links, "FB1")
    assert fb1.circuit["rp_ohm"] == 936.0
    assert fb1.impedance(W * np.array([1e3]))[0].real == pytest.approx(0.032, rel=1e-3)


def test_maker_impedance_round_trips_through_the_setup_file():
    setup, _ = _ferrite(_measured())
    back = by_name(PISetup.from_dict(setup.to_dict()).links, "FB1")
    assert back.z_points == by_name(setup.links, "FB1").z_points
    assert not back.is_short()


def test_feedthrough_through_resistance_is_its_dc_resistance():
    pdn = _load_switch(datasheet_resistances=[{"spec": "rds_on", "typ": "0.089", "unit": "ohm"}])
    pdn["rails"][0]["loads"][0].update(input_net="VIN_F", input_filter=[{
        "refdes": "FB1", "class": "capacitor", "net_in": "VIN", "net_out": "VIN_F",
        "resistance_ohm": "unknown", "datasheet_figures": [],
        "shunt_cap": {"refdes": "FB1", "value": "1uF", "to": "GND",
                      "ac": {"maker_model": {"status": "ok", "connection": "feed_thru",
                                             "through_resistance_ohm": 0.0032}}}}])
    setup, _ = TomachiePdnImporter(copy.deepcopy(pdn), filtered_board()).run()
    assert by_name(setup.links, "FB1").resistance == pytest.approx(0.0032)


def test_inductance_follows_the_solved_current_and_holds_beyond():
    pts = [[0.0, 10.83e-6], [0.4, 10.0e-6], [0.8, 8.41e-6]]
    assert series_z.inductance_at(pts, 0.0) == pytest.approx(10.83e-6)
    assert series_z.inductance_at(pts, -0.2) == pytest.approx(10.415e-6)
    assert series_z.inductance_at(pts, 2.0) == pytest.approx(8.41e-6)


def test_inductor_dc_bias_is_imported_beside_ac_and_biased():
    pdn = _load_switch(datasheet_resistances=[{"spec": "rds_on", "typ": "0.089", "unit": "ohm"}])
    pdn["rails"][0]["loads"][0].update(input_net="VIN_F", input_filter=[{
        "refdes": "FB1", "class": "inductor", "net_in": "VIN", "net_out": "VIN_F",
        "resistance_ohm": "unknown", "datasheet_figures": [{"spec": "dcr", "typ": "0.03", "unit": "ohm"}],
        "dc_bias": {"columns": ["a", "h"], "points": [[0, 10.83e-6], [0.8, 8.41e-6]],
                    "basis": "maker_data", "source": "Murata product data"}}])
    setup, notes = TomachiePdnImporter(copy.deepcopy(pdn), filtered_board()).run()
    fb1 = by_name(setup.links, "FB1")
    assert fb1.l_by_current == [[0.0, 10.83e-6], [0.8, 8.41e-6]]
    assert not fb1.is_short()
    biased = biased_link(setup, fb1, {"FB1": -0.4})
    w = W * np.array([1e5])
    assert biased.impedance(w)[0].imag == pytest.approx(w[0] * 9.62e-6, rel=1e-6)
    assert fb1.impedance(w)[0].imag == pytest.approx(w[0] * 10.83e-6, rel=1e-6)
    back = by_name(PISetup.from_dict(setup.to_dict()).links, "FB1")
    assert back.l_by_current == fb1.l_by_current
    assert any("FB1" in n and "inductance" in n for n in notes)
