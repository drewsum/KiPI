"""Solving only some net groups (excluded nets)."""

import pytest

from kipi.dc.pi.model import Load, PinRef, PISetup, active_setup, validate
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver.fem import SolveError
from test_tree import R, board, by_name, setup


def test_excluded_output_becomes_its_vrms_input_load():
    s = setup("ldo", iq=0.01, ground=True)
    s.excluded_nets = ["+3V3"]
    res = solve_tree(board(), s)
    assert set(res.groups) == {"VIN", "GND"}
    assert [v.name for v in res.vrms] == ["J1"]
    assert by_name(res.vrms, "J1").i_out == pytest.approx(1.01, rel=1e-9)
    ld = by_name(res.loads, "U1 (input)")
    assert ld.current == pytest.approx(1.01, rel=1e-12)
    assert ld.v_supply == pytest.approx(12.0 - 1.01 * R(8), rel=1e-9)
    assert ld.v_ground == pytest.approx(1.01 * R(9), rel=1e-9)   # returned at U1's ground
    assert any("Not solved" in w and "+3V3" in w for w in res.warnings)


def test_excluded_output_of_a_switcher_uses_nominal_voltages():
    s = setup("smps", eff=0.8, iq=0.002)
    s.excluded_nets = ["+3V3"]
    res = solve_tree(board(), s)
    expected = 3.3 * 1.0 / (0.8 * 12.0) + 0.002
    assert by_name(res.loads, "U1 (input)").current == pytest.approx(expected, rel=1e-12)


def test_excluded_input_makes_the_vrm_an_ideal_source():
    s = setup("ldo", iq=0.01, ground=True)
    s.excluded_nets = ["VIN"]
    res = solve_tree(board(), s)
    assert set(res.groups) == {"+3V3", "GND"}
    u1 = by_name(res.vrms, "U1")
    assert u1.kind == "source" and u1.i_in is None
    ld = by_name(res.loads, "U2")
    assert ld.v_supply == pytest.approx(3.3 - 1.0 * R(8), rel=1e-9)
    assert ld.v_ground == pytest.approx(1.0 * R(10), rel=1e-9)  # U1's ground is now 0 V


def test_excluded_ground_is_not_simulated():
    s = setup("ldo", iq=0.01, ground=True)
    s.excluded_nets = ["GND"]
    res = solve_tree(board(), s)
    assert "GND" not in res.groups
    assert by_name(res.loads, "U2").v_ground is None


def test_excluding_a_group_hides_its_setup_problems():
    s = setup()
    s.loads.append(Load("U9", PinRef("U9", "+5V"), 0.5))      # no VRM drives +5V yet
    assert any("+5V" in e for e in validate(s))
    s.excluded_nets = ["+5V"]
    assert validate(active_setup(s)[0]) == []
    solve_tree(board(), s)


def test_nothing_selected():
    s = setup()
    s.excluded_nets = ["VIN", "+3V3"]
    with pytest.raises(SolveError, match="Nothing to solve"):
        solve_tree(board(), s)


def test_active_setup_leaves_the_setup_alone():
    s = setup("ldo", ground=True)
    s.excluded_nets = ["VIN"]
    before = s.to_dict()
    active_setup(s)
    assert s.to_dict() == before
    unfiltered = setup()
    assert active_setup(unfiltered)[0] is unfiltered


def test_excluded_nets_round_trip(tmp_path):
    s = setup()
    s.excluded_nets = ["+3V3"]
    assert PISetup.load(s.save(tmp_path / "s.json")).excluded_nets == ["+3V3"]
