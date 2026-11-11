# Copyright (c) 2026 Tomachie LLC.
"""A power mux or a second feed in a Tomachie power-tree file: one path per input."""

import copy
import json

import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Stackup, Zone,
)
from kipi.core.model.snapshot_io import save
from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.model import PISetup, validate
from kipi.dc.pi.tree import solve_tree
from kipi.headless import run
from test_tomachie_pdn import PDN
from tree_boards import B, F, T, by_name, pad, poly


def _board(second="U1"):
    """VIN: J1.1 -> U1.1 (y 0..1); VIN2: J3.1 -> U1.4, or D1.1 -> D1.2 on OUT (y 2..3);
    OUT: U1.2 -> U2.1."""
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [pad("j1_1", "VIN", "J1", "1", box(0, 0, 1, 1)),
            pad("u1_1", "VIN", "U1", "1", box(9, 0, 10, 1)),
            pad("j3_1", "VIN2", "J3", "1", box(0, 2, 1, 3)),
            pad("u1_2", "OUT", "U1", "2", box(12, 0, 13, 1)),
            pad("u2_1", "OUT", "U2", "1", box(21, 0, 22, 1)),
            pad("j1_2", "GND", "J1", "2", box(0, 0, 1, 1), B),
            pad("j3_2", "GND", "J3", "2", box(0, 2, 1, 3), B),
            pad("u1_3", "GND", "U1", "3", box(10, 0, 11, 1), B),
            pad("u2_2", "GND", "U2", "2", box(21, 0, 22, 1), B)]
    out = box(12, 0, 22, 1)
    zones = [Zone("a", "VIN", "", 0, {F: [poly(box(0, 0, 10, 1))]}),
             Zone("a2", "VIN2", "", 0, {F: [poly(box(0, 2, 10, 3))]}),
             Zone("c", "GND", "", 0, {B: [poly(box(0, 0, 22, 3))]})]
    nets = [Net(1, "VIN"), Net(2, "VIN2"), Net(3, "OUT"), Net(4, "GND")]
    if second == "U1":
        pads.append(pad("u1_4", "VIN2", "U1", "4", box(9, 2, 10, 3)))
    elif second == "D1":
        pads += [pad("d1_1", "VIN2", "D1", "1", box(9, 2, 10, 3)),
                 pad("d1_2", "OUT", "D1", "2", box(12, 2, 13, 3))]
        out = box(12, 0, 22, 3)
    else:
        # U2.1 on N, fed by D2 from OUT and D1 from VIN2.
        pads[4] = pad("u2_1", "N", "U2", "1", box(21, 0, 22, 1))
        pads += [pad("d2_1", "OUT", "D2", "1", box(13, 0, 14, 1)),
                 pad("d2_2", "N", "D2", "2", box(15, 0, 16, 1)),
                 pad("d1_1", "VIN2", "D1", "1", box(9, 2, 10, 3)),
                 pad("d1_2", "N", "D1", "2", box(15, 2, 16, 3))]
        out = box(12, 0, 14, 1)
        zones.append(Zone("n", "N", "", 0, {F: [poly(box(15, 0, 22, 3))]}))
        nets.append(Net(5, "N"))
    zones.append(Zone("b", "OUT", "", 0, {F: [poly(out)]}))
    return BoardSnapshot("mux", "", st, nets, [], [], pads, [], zones, [])


def _pdn(second="U1"):
    pdn = copy.deepcopy(PDN)
    vin2 = copy.deepcopy(pdn["rails"][0])
    vin2.update(name="VIN2", voltage={"nominal": 10.0, "min": "not_specified",
                                      "max": "not_specified", "basis": "net_name"})
    vin2["source"].update(refdes="J3", pins=["J3_1"], output_net="VIN2")
    vin2["loads"][0]["pins"] = ["U1_4"]
    out = pdn["rails"][1]
    out.update(name="OUT", voltage={"nominal": 12.0, "min": "not_specified",
                                    "max": "not_specified", "basis": "net_name"})
    out["source"].update(output_net="OUT", output_path=[])
    pdn["rails"] = [pdn["rails"][0], vin2, out]
    pdn["regulators"] = [{"refdes": "U1", "type": "load_switch", "type_basis": "part_answer",
                          "input_rails": ["VIN", "VIN2"], "output_rails": ["OUT"],
                          "datasheet_resistances": [{"spec": "rds_on", "typ": "0.05",
                                                     "unit": "ohm"}]}]
    def diode(ref, net_in, net_out):
        return {"refdes": ref, "class": "diode", "net_in": net_in, "net_out": net_out,
                "resistance_ohm": "unknown",
                "datasheet_figures": [{"spec": "vf", "typ": "0.3", "unit": "V", "if": "1 A"}]}
    if second != "U1":
        vin2["loads"] = []
        pdn["regulators"][0]["input_rails"] = ["VIN"]
    if second == "D1":
        out["other_feeds"] = [{"rail": "VIN2", "series": [diode("D1", "VIN2", "OUT")]}]
    elif second == "ored":
        out["loads"][0].update(input_net="N", input_filter=[diode("D2", "OUT", "N")],
                               other_feeds=[{"rail": "VIN2", "series": [diode("D1", "VIN2", "N")]}])
    return pdn


def test_each_mux_input_is_a_link_and_one_is_closed_at_a_time():
    setup, notes = TomachiePdnImporter(_pdn(), _board()).run()
    a = by_name(setup.links, "U1 (VIN)")
    [b] = setup.mux_links
    assert (a.side_a.net, a.side_b.net, a.mux, a.mux_input) == ("VIN", "OUT", "U1", 0)
    assert (b.name, b.side_a.net, b.side_a.pins, b.side_b.net, b.mux, b.mux_input) == \
        ("U1 (VIN2)", "VIN2", ["4"], "OUT", "U1", 1)
    assert b.resistance == pytest.approx(0.05)
    assert validate(setup) == []
    assert any("U1 (OUT): power mux" in n and "VIN, VIN2" in n for n in notes)
    [(mux, k, group)] = setup.mux_paths()
    assert (mux, k, [x.name for x in group]) == ("U1", 1, ["U1 (VIN2)"])
    other = setup.with_mux_input(mux, k)
    assert [x.name for x in other.links if x.mux] == ["U1 (VIN2)"]
    assert [x.name for x in other.mux_links] == ["U1 (VIN)"]
    assert [x.name for x in setup.links if x.mux] == ["U1 (VIN)"]
    again = PISetup.from_dict(setup.to_dict())
    assert [(x.name, x.mux, x.mux_input) for x in again.mux_links] == [("U1 (VIN2)", "U1", 1)]


def test_each_path_solves_from_its_own_source():
    setup, _ = TomachiePdnImporter(_pdn(), _board()).run()
    u2 = by_name(solve_tree(_board(), setup).loads, "U2")
    assert 11.9 < u2.v_effective < 12.0
    u2b = by_name(solve_tree(_board(), setup.with_mux_input("U1", 1)).loads, "U2")
    assert 9.9 < u2b.v_effective < 10.0


def test_a_second_feed_through_a_diode_is_a_path_of_its_own():
    setup, notes = TomachiePdnImporter(_pdn("D1"), _board("D1")).run()
    assert [x.name for x in setup.links] == ["U1"]
    [d1] = setup.mux_links
    assert (d1.side_a.net, d1.side_b.net, d1.mux, d1.mux_input) == ("VIN2", "OUT", "OUT feeds", 1)
    assert d1.diode_vf == pytest.approx(0.3)
    assert any(n.startswith("Rail OUT: also fed from VIN2 through D1") for n in notes)
    u2 = by_name(solve_tree(_board("D1"), setup).loads, "U2")
    assert 11.9 < u2.v_effective < 12.0
    path = setup.with_mux_input("OUT feeds", 1)
    assert [v.name for v in path.vrms] == ["J3 VIN2"]
    u2b = by_name(solve_tree(_board("D1"), path).loads, "U2")
    assert 9.55 < u2b.v_effective < 9.75


def test_the_headless_solve_runs_each_path(tmp_path):
    board = tmp_path / "board.json.gz"
    save(_board(), board)
    pdn = tmp_path / "board.pdn.json"
    pdn.write_text(json.dumps(_pdn()), encoding="utf-8")
    doc = json.loads(run(tmp_path / "out", board, pdn, ac="none").read_text(encoding="utf-8"))
    assert [(c["case"], c.get("path")) for c in doc["dc"]["cases"]] == [
        ("typical", None), ("worst", None),
        ("typical", "through U1 (VIN2)"), ("worst", "through U1 (VIN2)")]
    [u2] = [x for x in doc["dc"]["cases"][2]["loads"] if x["refdes"] == "U2"]
    assert 9.9 < u2["v_low"] < 10.0


def test_a_loads_diode_ored_node_is_a_path_per_feed():
    setup, notes = TomachiePdnImporter(_pdn("ored"), _board("ored")).run()
    d2 = by_name(setup.links, "D2")
    [d1] = setup.mux_links
    assert (d2.mux, d2.mux_input, d1.name, d1.mux, d1.mux_input) == ("U2 feeds", 0, "D1", "U2 feeds", 1)
    assert any(n.startswith("U2: also fed from VIN2 through D1") for n in notes)
    u2 = by_name(solve_tree(_board("ored"), setup).loads, "U2")
    assert 11.6 < u2.v_effective < 11.75
    path = setup.with_mux_input("U2 feeds", 1)
    assert [v.name for v in path.vrms] == ["J1 VIN", "J3 VIN2"]
    assert [k.name for k in path.links] == ["U1", "D1"]
    u2b = by_name(solve_tree(_board("ored"), path).loads, "U2")
    assert 9.6 < u2b.v_effective < 9.75
