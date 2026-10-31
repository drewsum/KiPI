"""Board edge boundaries (#221): stretches of the edge held at a fixed temperature."""

import json
import pickle

import numpy as np
import pytest

from kipi.dc.pi.model import PISetup
from kipi.dc.thermal.edges import edge_rings, nearest_ring, side_at, span
from kipi.dc.thermal.model import BoardThermalModel, EdgeBoundary, ThermalSettings
from test_thermal import plate

LEFT = [[0.0, 0.0], [0.0, 40.0]]          # the plate's left side


def model(*edges, ambient=25.0):
    return BoardThermalModel(plate(), ThermalSettings(enabled=True, ambient_c=ambient,
                                                      grid_mm=1.0, edges=list(edges)))


def uniform(m, q=1e-4):
    heat = m.empty_heat()
    heat[:, m.inside] = q * m.d * m.d / 2
    return heat


def test_without_boundaries_nothing_changes():
    m = model()
    assert m.edge_cells == [] and not m.g_fix.any()
    t = m.solve(uniform(m))
    assert np.nanmax(np.abs(t - (25.0 + 1e-4 / (2 * m.settings.h_w_mm2_k())))) < 1e-6


def test_the_held_edge_sits_at_its_temperature_and_takes_the_heat():
    m = model(EdgeBoundary("frame", 25.0, 0.0, LEFT))
    heat = uniform(m)
    t = m.solve(heat)
    held = m.edge_cells[0]
    assert held[:, 0].all() and not held[:, 1:].any()       # the left column of cells
    assert np.abs(t[:, held] - 25.0).max() < 1e-4
    # Hotter away from the frame; cooler everywhere than with convection alone.
    row = t[0, 20]
    assert np.all(np.diff(row) > 0)
    assert np.nanmax(t) < np.nanmax(model().solve(heat))
    # Energy balance: the heat put in leaves by convection or into the frame.
    (name, temp, w), = m.edge_heat(t)
    assert (name, temp) == ("frame", 25.0)
    conv = np.nansum((t - 25.0) * m.g_conv)
    assert w > 0 and conv + w == pytest.approx(heat.sum(), rel=1e-6)


def test_a_hot_edge_heats_the_board():
    m = model(EdgeBoundary("hot", 60.0, 0.0, LEFT))
    t = m.solve(m.empty_heat())
    assert np.nanmax(t) == pytest.approx(60.0, abs=1e-4)
    assert 25.0 < np.nanmin(t) < 60.0
    (_, _, w), = m.edge_heat(t)
    assert w < 0                                            # it gives heat
    assert -w == pytest.approx(np.nansum((t - 25.0) * m.g_conv), rel=1e-6)


def test_the_strip_holds_cells_its_width_in_from_the_edge():
    m = model(EdgeBoundary("wedge lock", 25.0, 5.0, LEFT))
    held = m.edge_cells[0]
    assert held[:, :5].all() and not held[:, 6:].any()


def test_a_later_boundary_wins_where_two_overlap():
    m = model(EdgeBoundary("a", 30.0, 0.0, LEFT), EdgeBoundary("b", 40.0, 0.0, LEFT))
    assert not m.edge_cells[0].any() and m.edge_cells[1][:, 0].all()
    assert np.nanmax(m.solve(m.empty_heat())) == pytest.approx(40.0, abs=1e-4)


def test_a_boundary_off_the_board_warns_and_holds_nothing():
    from kipi.core.progress import ProgressReporter
    warned = []

    class P(ProgressReporter):
        def warn(self, msg):
            warned.append(msg)

    BoardThermalModel(plate(), ThermalSettings(enabled=True, grid_mm=1.0, edges=[
        EdgeBoundary("away", 25.0, 0.0, [[100.0, 100.0], [120.0, 100.0]])]), P())
    assert any("'away' is not on the board" in w for w in warned)


def test_boundaries_save_with_the_setup():
    s = PISetup()
    s.thermal.edges = [EdgeBoundary("frame", 55.0, 3.0, LEFT)]
    back = PISetup.from_dict(json.loads(json.dumps(s.to_dict())))
    assert back.thermal.edges == s.thermal.edges
    assert PISetup.from_dict({}).thermal.edges == []


def test_settings_pickled_before_boundaries_have_none():
    s = ThermalSettings()
    state = dict(s.__dict__)
    del state["edges"]
    old = ThermalSettings.__new__(ThermalSettings)
    old.__setstate__(state)
    assert old.edges == []
    assert pickle.loads(pickle.dumps(ThermalSettings(edges=[EdgeBoundary()]))).edges


def test_the_cosim_reports_the_heat_each_edge_takes():
    from kipi.dc.thermal.cosim import solve_electrothermal
    from tree_boards import board, setup
    snap, s = board(), setup()
    from kipi.dc.thermal.model import board_geometry
    x0, y0, _, y1 = board_geometry(snap).bounds            # no outline: around the copper
    edge = EdgeBoundary("frame", 25.0, 0.0, [[x0, y0], [x0, y1]])
    th = ThermalSettings(enabled=True, iterations=1, grid_mm=0.5, edges=[edge])
    tree = solve_electrothermal(snap, s, th)
    (name, temp, w), = tree.thermal.edge_w
    assert name == "frame" and 0 < w < tree.thermal.heat_w
    assert tree.thermal.edges_text().startswith("; edges held: frame at 25 °C takes")


# ------------------------------------------------------------------ picking the edge
def test_clicking_a_side_takes_the_whole_straight_side():
    ring, = edge_rings(plate())
    s = ring.project(__import__("shapely").Point(0, 13))
    pts = np.array(side_at(ring, s))
    assert np.allclose(pts[:, 0], 0) and {pts[0, 1], pts[-1, 1]} == {0.0, 40.0}


def test_a_drag_takes_the_shorter_way_round():
    import shapely
    ring, = edge_rings(plate())
    a = ring.project(shapely.Point(0, 10))
    b = ring.project(shapely.Point(10, 0))
    pts = span(ring, a, b)
    length = shapely.LineString(pts).length
    assert length == pytest.approx(20.0)
    assert [0.0, 0.0] in pts                               # round the corner
    assert nearest_ring([ring], (-2, 20)) == (0, pytest.approx(2.0))
