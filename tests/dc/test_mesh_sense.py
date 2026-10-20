"""Mesh remote sense (#93): every sense point is held at the target voltage."""

import pytest

from kipi.dc.pi.model import Load, PinRef, PISetup, SensePoint, validate
from kipi.dc.pi.tree import solve_tree

from test_tree import board_two_pin
from tree_boards import R, board, by_name, setup


def _mesh(s, *points):
    u1 = by_name(s.vrms, "U1")
    u1.mesh_sense = True
    u1.sense, u1.ground_sense = points[0].sense, points[0].ground_sense
    u1.sense_points = list(points[1:])
    return s


def _two_points():
    s = _mesh(setup("ldo"), SensePoint(PinRef("U2", "+3V3", ["3"])),
              SensePoint(PinRef("U2", "+3V3", ["1"])))
    s.loads[:] = [Load("END", PinRef("U2", "+3V3", ["1"]), 1.0),
                  Load("MID", PinRef("U2", "+3V3", ["3"]), 0.2)]
    return s


def test_every_sense_point_sits_at_the_set_voltage():
    res = solve_tree(board_two_pin(), _two_points())
    for name in ("END", "MID"):
        assert by_name(res.loads, name).v_supply == pytest.approx(3.3, abs=1e-9)
    u1 = by_name(res.vrms, "U1")
    assert u1.i_out == pytest.approx(1.2, rel=1e-6)
    # No current reaches the output pins, which only measure: they sit at the mid point.
    assert u1.v_out == pytest.approx(3.3, abs=1e-9)


def test_mesh_sense_follows_the_load_line():
    s = _two_points()
    by_name(s.vrms, "U1").load_line_mohm = 10.0
    res = solve_tree(board_two_pin(), s)
    assert by_name(res.loads, "END").v_supply == pytest.approx(3.3 - 0.010 * 1.2, abs=1e-7)


def test_off_by_default_the_extra_points_are_ignored():
    s = _two_points()
    by_name(s.vrms, "U1").mesh_sense = False
    res = solve_tree(board_two_pin(), s)
    # Ordinary remote sense at point 1 (the mid pin): the end pin sits below it.
    assert by_name(res.loads, "MID").v_supply == pytest.approx(3.3, abs=1e-9)
    assert by_name(res.loads, "END").v_supply == pytest.approx(3.3 - 1.0 * R(4), rel=1e-6)


def test_each_point_is_held_above_its_ground_sense():
    s = _mesh(setup("ldo", iq=0.01, ground=True),
              SensePoint(PinRef("U2", "+3V3", ["1"]), PinRef("U2", "GND", ["2"])))
    res = solve_tree(board(), s)
    ld = by_name(res.loads, "U2")
    assert ld.v_effective == pytest.approx(3.3, abs=1e-9)
    assert by_name(res.vrms, "U1").v_ground_sense == pytest.approx(ld.v_ground, rel=1e-9)


def test_validation_and_round_trip(tmp_path):
    s = _mesh(setup("ldo", ground=True), SensePoint(PinRef("U2", "+3V3", ["1"])),
              SensePoint(None), SensePoint(PinRef("U2", "GND", ["2"])))
    errors = validate(s)
    assert any("mesh sense point 2 has no sense pins" in e for e in errors)
    assert any("(sense point 3): sense pins must be on" in e for e in errors)
    s = _two_points()
    path = s.save(tmp_path / "x.kipi-setup.yml")
    assert PISetup.load(path) == s
