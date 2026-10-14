"""An optional total output current limit judges each VRM PASS or FAIL."""

import pickle

import pytest

from kipi.dc.pi.model import PISetup, validate
from kipi.dc.pi.tree import VrmReport, solve_tree
from test_multiphase import board, by_name, two_phase


def test_limit_judges_the_total_of_all_phases():
    s = two_phase()                                  # U1: two phases, 1 A each
    s.vrms[1].current_limit_a = 2.5
    s.vrms[0].current_limit_a = 1.5                  # J1 feeds U1's 2 A input
    tree = solve_tree(board(), s)
    u1, j1 = by_name(tree.vrms, "U1"), by_name(tree.vrms, "J1")
    assert u1.i_out == pytest.approx(2.0) and u1.current_limit_a == 2.5 and u1.passed is True
    assert j1.i_out > 1.5 and j1.passed is False
    s.vrms[1].current_limit_a = 1.5                  # each phase is under it, the total is not
    assert by_name(solve_tree(board(), s).vrms, "U1").passed is False


def test_no_limit_by_default():
    tree = solve_tree(board(), two_phase())
    assert all(v.current_limit_a is None and v.passed is None for v in tree.vrms)


def test_limit_round_trips_and_must_be_positive(tmp_path):
    s = two_phase()
    s.vrms[1].current_limit_a = 3.0
    back = PISetup.load(s.save(tmp_path / "s.json"))
    assert [v.current_limit_a for v in back.vrms] == [None, 3.0]
    d = s.to_dict()
    del d["vrms"][1]["current_limit_a"]              # setups saved before the limit
    assert PISetup.from_dict(d).vrms[1].current_limit_a is None
    s.vrms[1].current_limit_a = 0.0
    assert any("current limit" in e for e in validate(s))


def test_results_saved_before_the_limit_still_load():
    r = VrmReport("U1", "ldo", "VOUT", 1.0, 1.0, None, 2.0, None, None, 2.0, None)
    blob = pickle.dumps(r).replace(b"current_limit_a", b"current_limit_z")
    old = pickle.loads(blob.replace(b"passed", b"passez"))
    assert old.current_limit_a is None and old.passed is None


def test_csv_and_html_show_the_verdict(tmp_path):
    import csv
    from kipi.dc.pi.report import export_csv
    snap, s = board(), two_phase()
    s.vrms[1].current_limit_a = 1.5
    tree = solve_tree(snap, s)
    export_csv(tree, snap, tmp_path, stem="t")
    with open(tmp_path / "t_vrms.csv", encoding="utf-8") as fh:
        rows = {r["vrm"]: r for r in csv.DictReader(fh)}
    assert rows["U1"]["result"] == "FAIL" and float(rows["U1"]["i_limit_A"]) == 1.5
    assert rows["J1"]["result"] == ""

    pytest.importorskip("plotly")
    pytest.importorskip("matplotlib")
    from kipi.dc.report.html import write_report
    from kipi.dc.viz.results import build_result_scene
    scenes = {n: build_result_scene(snap, g.result) for n, g in tree.groups.items()}
    text = write_report(tmp_path / "r.html", tree, scenes, snap, s).read_text(encoding="utf-8")
    assert "I limit (A)" in text and '<span class="fail">FAIL</span>' in text
