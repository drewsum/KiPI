"""Load power and DC connection losses as heat in the electro-thermal solve (#62)."""

import copy
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Zone,
)
from kipi.dc.pi.model import PISetup
from kipi.dc.pi.report import pin_rows
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver import CurrentLoad, NetProblem, SeriesLink, SolveSettings, VoltageSource, run
from kipi.dc.thermal.cosim import ThermalResult, heat_sources, load_powers, solve_electrothermal
from kipi.dc.thermal.model import ThermalSettings
from tree_boards import board, setup

F, B = 3, 34
T = 0.035


@pytest.fixture(scope="module")
def solved():
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    return snap, s, solve_tree(snap, s)


def test_load_power_is_pin_current_times_pin_voltage(solved):
    snap, s, tree = solved
    watts, pts = load_powers(tree, snap, s)["U2"]
    pins = [r for r in pin_rows(tree, snap) if r.part == "U2"]
    assert watts == pytest.approx(sum(r.current * r.v for r in pins), rel=1e-12)
    # The ground pins return the current: the load sees the supply minus the ground.
    l = tree.loads[0]
    assert watts == pytest.approx(l.current * (l.v_supply - l.v_ground), rel=1e-9)
    assert 0 < watts < 3.3 and len(pts) == len(pins)


def test_a_load_without_ground_pins_returns_at_zero_volts():
    snap = board()
    s = setup("ldo")
    tree = solve_tree(snap, s)
    watts, _ = load_powers(tree, snap, s)["U2"]
    assert watts == pytest.approx(tree.loads[0].current * tree.loads[0].v_supply, rel=1e-9)


def test_load_power_is_heat_only_when_ticked(solved):
    snap, s, tree = solved
    off = heat_sources(tree, snap, s)[1].sum()
    on_setup = copy.deepcopy(s)
    on_setup.thermal.load_power = True
    on = heat_sources(tree, snap, on_setup)[1].sum()
    assert on - off == pytest.approx(load_powers(tree, snap, s)["U2"][0], rel=1e-9)


def test_electrothermal_with_load_power_runs_hotter():
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    th = ThermalSettings(enabled=True, iterations=1, grid_mm=0.5)
    cool = solve_electrothermal(snap, s, th).thermal
    hot = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1, grid_mm=0.5,
                                                        load_power=True)).thermal
    assert cool.load_heat_w == {}
    assert set(hot.load_heat_w) == {"U2"}
    assert hot.heat_w == pytest.approx(cool.heat_w + hot.load_heat_w["U2"], rel=1e-6)
    assert hot.t_max > cool.t_max


def test_load_power_round_trips_through_the_setup_file():
    s = setup("ldo")
    s.thermal.load_power = True
    assert PISetup.from_dict(s.to_dict()).thermal.load_power
    assert not PISetup.from_dict(setup("ldo").to_dict()).thermal.load_power    # off by default


def test_setups_with_a_stated_load_heat_still_open():
    d = setup("ldo").to_dict()
    d["loads"][0]["dissipation_w"] = 0.25             # saved before #121 removed it
    s = PISetup.from_dict(d)
    assert s.loads[0].name == setup("ldo").loads[0].name
    assert "dissipation_w" not in s.to_dict()["loads"][0]


def test_thermal_results_saved_before_load_heat():
    th = ThermalResult.__new__(ThermalResult)
    th.__setstate__({"settings": ThermalSettings(), "heat_w": 1.0})
    assert th.load_heat_w == {}


def _strip(pads):
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    zones = [Zone(f"z{n}", n, "", 0, {F: [Poly(list(g.exterior.coords)[:-1], [])]})
             for n, g in (("A", box(0, 0, 10, 1)), ("B", box(12, 0, 22, 1)))]
    return BoardSnapshot("heat", "", st, [Net(1, "A"), Net(2, "B")], [], [], pads, [], zones, [])


def _pad(pid, net, ref, num, g):
    return Pad(pid, net, ref, num, (g.centroid.x, g.centroid.y), "smd",
               shapes={F: [Poly(list(g.exterior.coords)[:-1], [])]})


def test_dc_connection_losses_heat_its_pins():
    """A DC connection's I²R lands on its pins, with or without the load power option."""
    snap = _strip([_pad("src", "A", "J1", "1", box(0, 0, 1, 1)),
                   _pad("r1a", "A", "R1", "1", box(9, 0, 10, 1)),
                   _pad("r1b", "B", "R1", "2", box(12, 0, 13, 1)),
                   _pad("ld", "B", "U1", "1", box(21, 0, 22, 1))])
    res = run(snap, NetProblem("A + B", [VoltageSource("VRM", ["src"], 1.0)],
                               [CurrentLoad("U1", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3),
                               series=[SeriesLink("R1", ["r1a"], ["r1b"], 0.02)],
                               nets=["A", "B"]))
    tree = SimpleNamespace(groups={"A + B": SimpleNamespace(result=res)}, vrms=[])
    for load_power in (False, True):
        s = SimpleNamespace(vrms=[], loads=[], thermal=ThermalSettings(load_power=load_power))
        xy, w, _, _ = heat_sources(tree, snap, s)
        at_r1 = np.isin(xy[:, 0], [9.5, 12.5]) & (xy[:, 1] == 0.5)
        assert w[at_r1].sum() == pytest.approx(2.0 ** 2 * 0.02, rel=1e-9)


def test_load_power_checkbox(qapp_offscreen):
    from kipi.dc.gui.pi_panel import PIPanel
    snap = board()
    s = setup("ldo", ground=True)
    panel = PIPanel()
    panel.set_board(snap, s, "test")
    box_ = panel.chk_load_power
    assert not box_.isChecked() and not box_.isEnabled()      # off, and thermal is off
    panel.chk_thermal.setChecked(True)
    assert box_.isEnabled()
    box_.setChecked(True)
    assert s.thermal.load_power
    s2 = setup("ldo")
    s2.thermal.load_power = True
    panel.set_board(snap, s2, "test")                           # a saved setup's choice shows
    assert panel.chk_load_power.isChecked()


@pytest.fixture(scope="module")
def qapp_offscreen():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
