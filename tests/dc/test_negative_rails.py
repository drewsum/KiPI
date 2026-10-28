"""Negative rails (#220): a load's current is what it draws from its rail, so on a negative
rail it flows into the copper and the pins rise toward 0 V, and the VRMs upstream draw
power from the rails feeding them whatever their polarity."""

import pytest

from kipi.dc.pi.tree import solve_tree
from tree_boards import R, board, by_name, setup


def _negative(kind="ldo", vin=-12.0, iq=0.0, ground=False):
    s = setup(kind, iq=iq, ground=ground)
    s.vrms[0].voltage, s.vrms[1].voltage = vin, -3.3
    return s


def test_load_on_a_negative_rail_sits_above_it():
    res = solve_tree(board(), _negative())
    ld = by_name(res.loads, "U2")
    assert ld.current == 1.0
    assert ld.v_supply == pytest.approx(-3.3 + 1.0 * R(8), rel=1e-9)
    assert ld.drop_worst == pytest.approx(-R(8), rel=1e-9)       # its size below -3.3 V
    u1 = by_name(res.vrms, "U1")
    assert u1.i_out == pytest.approx(-1.0, rel=1e-9)            # the VRM sinks it
    assert u1.p_out == pytest.approx(3.3, rel=1e-3)


def test_negative_ldo_chain_and_its_quiescent_current():
    res = solve_tree(board(), _negative(iq=0.01))
    u1, j1 = by_name(res.vrms, "U1"), by_name(res.vrms, "J1")
    assert u1.i_in == pytest.approx(-1.01, rel=1e-9)
    assert j1.i_out == pytest.approx(-1.01, rel=1e-9)
    assert u1.v_in == pytest.approx(-12.0 + 1.01 * R(8), rel=1e-9)


@pytest.mark.parametrize("vin", [12.0, -12.0])
def test_switcher_draws_power_from_either_polarity(vin):
    res = solve_tree(board(), _negative("smps", vin=vin))
    u1 = by_name(res.vrms, "U1")
    assert u1.p_in == pytest.approx(u1.p_out / 0.9, rel=1e-9)
    assert u1.p_in > 0
    assert u1.v_in == pytest.approx(vin - u1.i_in * R(8), rel=1e-9)


def test_ground_return_of_a_negative_rail():
    # An LDO passes its current through, so the return flows from J1's ground pin to the
    # load's (past U1's, 1 mm of pad): the load's ground sits below 0 V, which shrinks what
    # it sees further.
    res = solve_tree(board(), _negative(ground=True))
    ld = by_name(res.loads, "U2")
    assert ld.v_ground == pytest.approx(-1.0 * R(19), rel=1e-6)
    assert ld.v_effective == pytest.approx(-3.3 + 1.0 * R(27), rel=1e-6)
    for g in res.groups.values():
        assert g.result.fields.summary["kcl_error"] < 1e-9, g.name
