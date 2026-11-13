# Portions Copyright (c) 2026 Tomachie LLC.
"""Component heat (load power, #62, and VRM conversion loss, #253) and DC connection
losses as heat in the electro-thermal solve."""

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


def test_component_heat_off_heats_with_the_copper_and_vias_alone(solved):
    snap, s, tree = solved
    assert not s.thermal.component_heat                       # off by default
    u1 = next(v for v in tree.vrms if v.name == "U1")
    assert u1.p_loss > 0
    _, w, _, by_kind = heat_sources(tree, snap, s)
    assert "VRM conversion" not in by_kind and "loads" not in by_kind
    assert set(by_kind) <= {"copper", "vias"}
    assert w.sum() == pytest.approx(sum(by_kind.values()), rel=1e-12)


def test_component_heat_on_adds_the_loads_vrm_conversion_and_dc_connections(solved):
    snap, s, tree = solved
    off = heat_sources(tree, snap, s)[1].sum()
    on_setup = copy.deepcopy(s)
    on_setup.thermal.component_heat = True
    _, w, _, by_kind = heat_sources(tree, snap, on_setup)
    load_w = load_powers(tree, snap, s)["U2"][0]
    u1 = next(v for v in tree.vrms if v.name == "U1")
    assert by_kind["loads"] == pytest.approx(load_w, rel=1e-9)
    assert by_kind["VRM conversion"] == pytest.approx(u1.p_loss, rel=1e-9)
    links = sum(sr.power for g in tree.groups.values() for sr in g.result.fields.series)
    assert by_kind.get("DC connections", 0.0) == pytest.approx(links, rel=1e-9)
    assert w.sum() - off == pytest.approx(load_w + u1.p_loss + links, rel=1e-9)


def test_electrothermal_with_component_heat_runs_hotter():
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    th = ThermalSettings(enabled=True, iterations=1, grid_mm=0.5)
    cool_tree = solve_electrothermal(snap, s, th)
    cool = cool_tree.thermal
    hot = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1, grid_mm=0.5,
                                                        component_heat=True)).thermal
    vrm_w = hot.heat_by_kind["VRM conversion"]
    link_w = hot.heat_by_kind.get("DC connections", 0.0)
    assert cool.load_heat_w == {} and set(cool.heat_by_kind) <= {"copper", "vias"}
    assert set(hot.load_heat_w) == {"U2"}
    assert hot.heat_w == pytest.approx(cool.heat_w + hot.load_heat_w["U2"] + vrm_w + link_w,
                                       rel=1e-6)
    assert hot.t_max > cool.t_max
    # The results say which component heat went in, or what was left out.
    assert cool.vrm_loss_w == pytest.approx(
        next(v for v in cool_tree.vrms if v.name == "U1").p_loss, rel=1e-9)
    assert cool.component_heat_text().startswith(" (component heat off: ")
    assert (f"VRM conversion loss ({cool.vrm_loss_w:.3g} W)" in cool.component_heat_text()
            and "load power not included)" in cool.component_heat_text())
    assert hot.component_heat_text() == (
        f" (of which {hot.load_heat_w['U2']:.3g} W from loads, {vrm_w:.3g} W from VRM "
        f"conversion, {link_w:.3g} W from DC connections)")


def test_component_heat_round_trips_through_the_setup_file():
    s = setup("ldo")
    s.thermal.component_heat = True
    d = s.to_dict()
    assert d["thermal"]["component_heat"] and "load_power" not in d["thermal"]
    assert PISetup.from_dict(d).thermal.component_heat
    assert not PISetup.from_dict(setup("ldo").to_dict()).thermal.component_heat  # off by default


@pytest.mark.parametrize("on", [False, True])
def test_setups_saved_with_load_power_read_it_as_component_heat(on):
    d = setup("ldo").to_dict()
    del d["thermal"]["component_heat"]
    d["thermal"]["load_power"] = on                   # saved before #253
    assert PISetup.from_dict(d).thermal.component_heat is on


@pytest.mark.parametrize("on", [False, True])
def test_results_saved_with_load_power_read_it_as_component_heat(on):
    st = ThermalSettings.__new__(ThermalSettings)
    state = vars(ThermalSettings()).copy()
    del state["component_heat"]
    st.__setstate__({**state, "load_power": on})
    assert st.component_heat is on and not hasattr(st, "load_power")
    # Before #253 the VRMs heated the board either way; such a result says so.
    th = ThermalResult.__new__(ThermalResult)
    th.__setstate__({"settings": st, "heat_w": 1.0, "heat_by_kind": {"VRM conversion": 0.5}})
    assert "0.5 W from VRM conversion" in th.component_heat_text()


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
    """A DC connection's I²R lands on its pins with component heat, and not without (#253)."""
    snap = _strip([_pad("src", "A", "J1", "1", box(0, 0, 1, 1)),
                   _pad("r1a", "A", "R1", "1", box(9, 0, 10, 1)),
                   _pad("r1b", "B", "R1", "2", box(12, 0, 13, 1)),
                   _pad("ld", "B", "U1", "1", box(21, 0, 22, 1))])
    res = run(snap, NetProblem("A + B", [VoltageSource("VRM", ["src"], 1.0)],
                               [CurrentLoad("U1", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3),
                               series=[SeriesLink("R1", ["r1a"], ["r1b"], 0.02)],
                               nets=["A", "B"]))
    tree = SimpleNamespace(groups={"A + B": SimpleNamespace(result=res)}, vrms=[])
    for on in (False, True):
        s = SimpleNamespace(vrms=[], loads=[], thermal=ThermalSettings(component_heat=on))
        xy, w, _, _ = heat_sources(tree, snap, s)
        at_r1 = np.isin(xy[:, 0], [9.5, 12.5]) & (xy[:, 1] == 0.5)
        assert w[at_r1].sum() == pytest.approx(2.0 ** 2 * 0.02 if on else 0.0, rel=1e-9)


def test_component_heat_checkbox(qapp_offscreen):
    from kipi.dc.gui.pi_panel import PIPanel
    snap = board()
    s = setup("ldo", ground=True)
    panel = PIPanel()
    panel.set_board(snap, s)
    box_ = panel.chk_component_heat
    assert box_.text() == "Component heat"
    assert not box_.isChecked() and not box_.isEnabled()      # off, and thermal is off
    panel.chk_thermal.setChecked(True)
    assert box_.isEnabled()
    box_.setChecked(True)
    assert s.thermal.component_heat
    s2 = setup("ldo")
    s2.thermal.component_heat = True
    panel.set_board(snap, s2)                           # a saved setup's choice shows
    assert panel.chk_component_heat.isChecked()


@pytest.fixture(scope="module")
def qapp_offscreen():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
