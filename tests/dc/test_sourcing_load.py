"""A load that sources current onto its rail (#241): its current flows into the copper,
the pins rise above the target voltage and the VRM supplies that much less."""

import pytest

from kipi.dc.pi.model import PISetup
from kipi.dc.pi.tree import solve_tree
from tree_boards import R, board, by_name, setup


def _sourcing(ground=False, voltage=None):
    s = setup("ldo", ground=ground)
    s.loads[0].sources = True
    if voltage is not None:
        s.vrms[0].voltage, s.vrms[1].voltage = -12.0, voltage
    return s


def test_sourcing_load_lifts_its_rail():
    res = solve_tree(board(), _sourcing())
    ld = by_name(res.loads, "U2")
    assert ld.current == -1.0
    assert ld.v_supply == pytest.approx(3.3 + 1.0 * R(8), rel=1e-9)
    assert by_name(res.vrms, "U1").i_out == pytest.approx(-1.0, rel=1e-9)


def test_sourcing_load_on_a_negative_rail_pulls_it_further_down():
    res = solve_tree(board(), _sourcing(voltage=-3.3))
    ld = by_name(res.loads, "U2")
    assert ld.v_supply == pytest.approx(-3.3 - 1.0 * R(8), rel=1e-9)
    assert by_name(res.vrms, "U1").i_out == pytest.approx(1.0, rel=1e-9)


def test_sourcing_load_returns_through_its_ground_pins():
    res = solve_tree(board(), _sourcing(ground=True))
    ld = by_name(res.loads, "U2")
    # The current it puts on the rail comes out of ground: its ground pins sit below 0 V.
    assert ld.v_ground == pytest.approx(-1.0 * R(19), rel=1e-6)
    for g in res.groups.values():
        assert g.result.fields.summary["kcl_error"] < 1e-9, g.name


def test_sources_is_saved_and_defaults_off():
    s = _sourcing()
    assert PISetup.from_dict(s.to_dict()).loads[0].sources
    d = setup("ldo").to_dict()
    del d["loads"][0]["sources"]
    assert not PISetup.from_dict(d).loads[0].sources
