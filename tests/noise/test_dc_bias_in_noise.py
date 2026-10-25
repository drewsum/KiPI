"""The board model takes the parts' bias from the last DC solve, as AC mode does: the DC
voltage across each capacitor, and the current through each diode link."""

from types import SimpleNamespace as NS

import pytest

from kipi.ac.model import CapModel
from kipi.dc.pi.model import DcLink, PinRef, constant_drop_ref
from kipi.dc.solver.post import SeriesResult
from kipi.noise.checks import operating_point, seed_phases
from kipi.noise.extract.common import board_ports
from kipi.noise.model import Aggressor
from noise_boards import buck_board, buck_setup


def _setup(snap):
    setup = buck_setup(board_model=True)
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1 buck", "U1", fsw_hz=1e6)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    setup.aggressors.append(a)
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"], rated_v=25.0,
                           kept_at_rated_pct=40.0),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    return setup


def test_capacitors_are_derated_at_the_dc_solves_voltage(monkeypatch):
    snap = buck_board()
    setup = _setup(snap)
    agg = setup.aggressors[0]
    op = operating_point(setup, agg)
    nominal = board_ports(snap, setup, agg, op, {})
    c1 = next(p for p in nominal.ports if p.name == "C1").element
    assert c1.capacitance_f == pytest.approx(setup.caps[0].capacitance_at(12.0))
    # A DC solve that found 11 V across C1: the board model derates it there.
    monkeypatch.setattr("kipi.ac.bias.dc_volts", lambda snap, dc, caps: {"C1": 11.0})
    tree = NS(groups={"x": NS(result=NS(fields=NS(series=[])))})
    solved = board_ports(snap, setup, agg, op, {}, dc_result=tree)
    c1 = next(p for p in solved.ports if p.name == "C1").element
    assert c1.capacitance_f == pytest.approx(setup.caps[0].capacitance_at(11.0))
    assert any("DC solve's voltage" in n for n in solved.notes)
    assert not any("DC solve's voltage" in n for n in nominal.notes)


def test_a_diode_link_is_biased_by_the_dc_solve(monkeypatch):
    monkeypatch.setattr("kipi.ac.bias.dc_volts", lambda snap, dc, caps: {})
    snap = buck_board()
    setup = _setup(snap)
    # A diode in series on the output rail, from VOUT to itself (a stand-in: both sides
    # inside the rails), biased by the load beyond it or by the DC solve.
    setup.links.append(DcLink("D1", PinRef("C2", "VOUT", ["1"]), PinRef("U2", "VOUT", ["1"]),
                              0.05, spice=constant_drop_ref(0.4, 0.05)))
    agg = setup.aggressors[0]
    op = operating_point(setup, agg)
    est = board_ports(snap, setup, agg, op, {})
    d = next(p for p in est.ports if p.name == "D1").element
    # The estimate: a VRM drives side B, so it takes the diode to block (0 A).
    assert d.bias_a == 0.0 and any("estimated" in n for n in est.notes)
    tree = NS(groups={"VOUT": NS(result=NS(fields=NS(
        series=[SeriesResult("D1", 0.05, 4.2, 1.8, 1.4, 0.0)])))})
    got = board_ports(snap, setup, agg, op, {}, dc_result=tree)
    d = next(p for p in got.ports if p.name == "D1").element
    assert d.bias_a == pytest.approx(4.2) and any("from the DC solve" in n for n in got.notes)
