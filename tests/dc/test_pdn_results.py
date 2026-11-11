# Copyright (c) 2026 Tomachie LLC.
"""``pdn_results.json``: the headless solve's results, as fixed words for the tool that
wrote the pdn.json to read back."""

import copy
import json

import pytest

from kipi import pdn_results
from kipi.core.model.snapshot_io import save
from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.tree import solve_tree
from kipi.headless import run
from test_tomachie_pdn import PDN, filtered_board


def _pdn():
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0].update(type="switching", efficiency=0.9)
    pdn["rails"][0]["loads"][0].update(
        input_net="VIN_F", input_filter=[{"refdes": "FB1", "class": "ferrite",
                                          "net_in": "VIN", "net_out": "VIN_F",
                                          "resistance_ohm": 0.08, "datasheet_figures": []}])
    pdn["rails"][1]["source"].update(output_net="SWOUT", output_path=[
        {"refdes": "FB2", "class": "inductor", "net_in": "SWOUT", "net_out": "+3V3",
         "resistance_ohm": "unknown",
         "datasheet_figures": [{"spec": "dcr", "typ": "27", "unit": "mOhm"},
                               {"spec": "rated_current", "typ": "2", "unit": "A"}]}])
    return pdn


def test_a_dc_case_in_fixed_words():
    setup, _ = TomachiePdnImporter(_pdn(), filtered_board()).run()
    case = pdn_results.dc_case(solve_tree(filtered_board(), setup), setup, "typ")
    assert case["case"] == "typical"
    [u2] = [x for x in case["loads"] if x["refdes"] == "U2"]
    assert (u2["rail"], u2["pins"], u2["result"]) == ("+3V3", ["U2_1"], "pass")
    assert u2["current_a"] == pytest.approx(1.0) and u2["spare_v"] > 0
    assert u2["limit_low"] < u2["v_low"] <= u2["v_high"] < u2["limit_high"]
    # The switcher's in-loop inductor carries what its switch pins supply.
    [fb2] = [x for x in case["series_parts"] if x["refdes"] == "FB2"]
    assert (fb2["result"], fb2["rated_a"]) == ("within", 2.0)
    assert fb2["current_a"] == pytest.approx(1.0, rel=1e-3)
    assert case["summary"]["pass"] == sum(1 for x in case["loads"] if x["result"] == "pass")
    json.dumps(case, allow_nan=False)                 # no NaN reaches the file


def test_the_headless_solve_writes_the_file(tmp_path):
    board = tmp_path / "board.json.gz"
    save(filtered_board(), board)
    pdn = tmp_path / "board.pdn.json"
    pdn.write_text(json.dumps(_pdn()), encoding="utf-8")
    path = run(tmp_path / "out", board, pdn, ac="none")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert (doc["schema"], doc["schema_version"]) == ("pdn_results", 1)
    assert doc["analyzer"]["name"] == "KiPI"
    assert doc["pdn_json"]["sha256"] == pdn_results.sha256(pdn)
    assert [c["case"] for c in doc["dc"]["cases"]] == ["typical", "worst"]
    assert doc["ac"]["rails"] == []
    assert any(n.startswith("FB2: inside U1's regulation loop") for n in doc["import_notes"])


def test_a_rail_that_cannot_be_swept_says_why(tmp_path):
    board = tmp_path / "board.json.gz"
    save(filtered_board(), board)
    pdn = tmp_path / "board.pdn.json"
    pdn.write_text(json.dumps(_pdn()), encoding="utf-8")
    doc = json.loads(run(tmp_path / "out", board, pdn, ac="all").read_text(encoding="utf-8"))
    for rail in doc["ac"]["rails"]:
        assert rail["result"] in ("solved", "not_solved")
        if rail["result"] == "not_solved":
            assert rail["reason"]
