"""The Board map tab (#67): the solve gathers the map (parts first, then the mesh) and hands
it over as it fills; the tab draws the mesh by layer, the capacitors by model and the probes."""

import os

import numpy as np
import pytest

pytest.importorskip("qtpy")
pytest.importorskip("matplotlib")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from matplotlib.backend_bases import MouseEvent  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from ac_boards import plane_pair, plane_setup  # noqa: E402
from kipi.ac.board_map import ACMap  # noqa: E402
from kipi.ac.gui.board_map import ALL_LAYERS, EMPTY, BoardMap  # noqa: E402
from kipi.ac.model import CapModel  # noqa: E402
from kipi.ac.report import load_results, save_results  # noqa: E402
from kipi.ac.solver import solve_ac  # noqa: E402
from kipi.core.progress import ProgressReporter  # noqa: E402


class _Partials(ProgressReporter):
    def __init__(self):
        super().__init__()
        self.maps = []

    def partial(self, result):
        self.maps.append((result, result.mesh is not None))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def solved():
    snap = plane_pair(caps=[("C1", "100n", (30, 30)), ("C2", "100n", (40, 10)),
                            ("C3", "10u", (10, 40))])
    setup = plane_setup(snap, f_start_hz=1e6, f_stop_hz=1e7, points_per_decade=5,
                        field_points_per_decade=2)
    setup.caps = [CapModel("100n", 100e-9, 0.4e-9, 0.02, ["C1", "C2"])]   # C3 has none
    progress = _Partials()
    return snap, setup, solve_ac(snap, setup, "VRM1", "GND", progress), progress.maps


def test_solve_hands_over_the_map_as_it_fills(solved):
    _snap, _setup, result, maps = solved
    m = result.board_map
    assert [has_mesh for _, has_mesh in maps] == [False, True]   # parts, then the mesh
    assert all(got is m for got, _ in maps)
    assert {k: [p.ref for p in v] for k, v in m.models.items()} == {"100n": ["C1", "C2"]}
    assert [p.ref for p in m.missing] == ["C3"]
    assert [p.ref for p in m.probes] == [p.name for p in result.probes]
    assert m.outline and m.extent() is not None
    mesh = m.mesh
    assert len(mesh.tris) == result.stats.n_tris
    assert mesh.tri_ground.any() and not mesh.tri_ground.all()   # rail and ground copper
    assert [n for _, n in m.mesh_layers()]                       # named, top first


def test_saved_results_keep_the_map(solved, tmp_path):
    snap, setup, result, _ = solved
    path = save_results(tmp_path / "r.kipi-ac-results", {"VRM1": result}, "b", setup)
    m = load_results(path)["results"]["VRM1"].board_map
    assert m.models.keys() == result.board_map.models.keys()
    assert np.array_equal(m.mesh.tris, result.board_map.mesh.tris)


def test_the_tab_draws_layers_and_hover(app, solved):
    _snap, _setup, result, _ = solved
    w = BoardMap()
    w.resize(900, 600)
    w.set_map(result.board_map)
    w.canvas.draw()
    layers = [w.cmb_layer.itemData(i) for i in range(w.cmb_layer.count())]
    assert layers[0] == ALL_LAYERS and len(layers) == 1 + len(result.board_map.mesh_layers())
    def n_tris():
        return sum(len(c.get_paths()) for c in w.ax.collections
                   if type(c).__name__ == "PolyCollection")

    n_all = n_tris()
    assert n_all == result.stats.n_tris
    w.cmb_layer.setCurrentIndex(1)                     # one layer: fewer triangles
    assert 0 < n_tris() < n_all

    c1 = result.board_map.models["100n"][0]
    x, y = w.ax.transData.transform((c1.x, c1.y))
    w._hover(MouseEvent("motion_notify_event", w.canvas, x, y))
    assert w._annot.get_visible() and "C1" in w._annot.get_text()
    assert "100n" in w._annot.get_text()

    w.set_map(None)
    assert w.board_map is None and w.lbl.text() == EMPTY


def test_meshing_map_is_drawn_without_a_mesh(app):
    w = BoardMap()
    w.set_map(ACMap(rail="VRM1", rail_nets=["+3V3"], ground="GND"))
    assert "meshing" in w.ax.get_title()
    assert not w.cmb_layer.isEnabled()


def test_main_window_tab(app, no_3d, solved):
    from kipi.gui.main_window import MainWindow
    _snap, _setup, result, _ = solved
    w = MainWindow()
    tab = w.views.indexOf(w.board_map)
    assert not w.views.isTabVisible(tab)               # DC mode
    w.set_mode("ac")
    assert w.views.isTabVisible(tab)
    w._show_ac_result(result)
    assert w.board_map.board_map is result.board_map
    assert w.views.currentWidget() is w.plot           # results open on the impedance plot
    w._show_ac_result(None)
    assert w.board_map.board_map is None
    w.close()
