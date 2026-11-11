# Copyright (c) 2026 Tomachie LLC.
"""A diode in series (#161): a DC connection with a forward voltage, solved from a diode
model of its drop and slope resistance (#191), within a few millivolts of the drop plus
the current times the slope; with its datasheet curve, on the curve itself."""

import sys
from pathlib import Path

import pytest

from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm
from kipi.dc.pi.tree import solve_tree

sys.path.insert(0, str(Path(__file__).parent))
from test_tomachie_pdn import filtered_board  # noqa: E402


def _setup(forward_v=None, forward_v_max=None):
    """12 V on VIN, a diode FB1 from VIN to VIN_F (50 mOhm slope), 1 A drawn on VIN_F."""
    s = PISetup()
    s.vrms = [Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source",
                  ground=PinRef("J1", "GND", ["2"]))]
    s.links = [DcLink("FB1", PinRef("FB1", "VIN", ["1"]), PinRef("FB1", "VIN_F", ["2"]), 0.05,
                      diode_vf=forward_v or 0.0, diode_vf_max=forward_v_max)]
    s.loads = [Load("U1", PinRef("U1", "VIN_F", ["1"]), 1.0,
                    ground=PinRef("U1", "GND", ["3"]))]
    return s


def _load(setup):
    return next(l for l in solve_tree(filtered_board(), setup).loads if l.name == "U1")


def test_the_load_sits_one_forward_drop_lower():
    plain, diode = _load(_setup()), _load(_setup(forward_v=0.45))
    assert diode.v_supply == pytest.approx(plain.v_supply - 0.45, abs=0.01)


def test_the_diode_current_and_loss():
    tree = solve_tree(filtered_board(), _setup(forward_v=0.45))
    link = next(s for g in tree.groups.values() for s in g.result.fields.series
                if s.name == "FB1")
    assert link.current == pytest.approx(1.0, rel=1e-6)
    assert link.power == pytest.approx(1.0 * (0.45 + 1.0 * 0.05), rel=0.02)


def test_worst_case_takes_the_maximum_forward_voltage():
    s = _setup(forward_v=0.45, forward_v_max=0.55)
    typ = _load(s)
    s.corner = "max"
    assert _load(s).v_supply == pytest.approx(typ.v_supply - 0.10, abs=2e-3)


def test_a_diode_round_trips_through_the_setup_file():
    s = PISetup.from_dict(_setup(forward_v=0.45, forward_v_max=0.55).to_dict())
    assert (s.links[0].diode_vf, s.links[0].diode_vf_max) == (0.45, 0.55)


# A Schottky's curve (SK510A-like, 25 C): far from a straight line over its range.
CURVE = [[0.01, 0.3484], [0.1, 0.4264], [0.5, 0.5341], [1.0, 0.6083], [2.0, 0.6923],
         [5.0, 0.7959], [10.0, 0.8936], [20.0, 1.038]]


def _drop(setup):
    tree = solve_tree(filtered_board(), setup)
    link = next(s for g in tree.groups.values() for s in g.result.fields.series
                if s.name == "FB1")
    return link.current, _load(setup).v_supply


def test_a_diode_with_a_curve_is_solved_on_it_at_its_current():
    """A line fitted from 2 A to 20 A (0.684 V + 18.4 mOhm) reads 0.70 V at 1 A; on the
    curve it drops 0.608 V there."""
    plain = _load(_setup())
    s = _setup(forward_v=0.684)
    s.links[0].resistance = 0.0184
    s.links[0].vf_curve = CURVE
    amps, v = _drop(s)
    assert amps == pytest.approx(1.0, rel=1e-6)
    # The plain setup's link is 50 mOhm: 50 mV of the 0.6083 V is already in it.
    assert plain.v_supply - v == pytest.approx(0.6083 - 0.05, abs=1e-4)


def test_worst_case_lifts_the_curve_as_much_as_the_line():
    s = _setup(forward_v=0.684, forward_v_max=0.758)
    s.links[0].resistance = 0.0184
    s.links[0].vf_curve = CURVE
    _, typ = _drop(s)
    s.corner = "max"
    _, worst = _drop(s)
    assert typ - worst == pytest.approx(0.758 - 0.684, abs=1e-4)


def test_curve_volts_between_and_beyond_its_points():
    from kipi.dc.pi.diode_curve import curve_volts
    assert curve_volts(CURVE, 1.0) == pytest.approx(0.6083)
    # Half-way in log current between 0.1 A and 1 A... is not the arithmetic midpoint.
    mid = curve_volts(CURVE, 0.1 * 10 ** 0.5)
    assert 0.4264 < mid < 0.6083
    assert curve_volts(CURVE, 40.0) > 1.038 and curve_volts(CURVE, 1e-4) >= 0.0


def test_a_curve_round_trips_through_the_setup_file():
    s = _setup(forward_v=0.684)
    s.links[0].vf_curve = CURVE
    assert PISetup.from_dict(s.to_dict()).links[0].vf_curve == CURVE


def test_a_curve_gives_the_diode_a_model_for_ac():
    """The DC solve is on the curve itself; AC takes the diode's small-signal values from
    a model fitted to it, and a saved setup keeps both."""
    from kipi.dc.pi.diode_curve import CURVE_SOURCE, diode_ref, fit_curve
    s = _setup(forward_v=0.684)
    s.links[0].vf_curve = CURVE
    diode_ref(s.links[0])
    assert s.links[0].is_diode and s.links[0].spice.file == CURVE_SOURCE
    i_s, n, rs = fit_curve(CURVE)
    assert i_s > 0 and n > 1 and rs > 0
    back = PISetup.from_dict(s.to_dict()).links[0]
    assert back.is_diode and back.vf_curve == CURVE
