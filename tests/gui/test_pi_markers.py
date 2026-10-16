"""PI Models markers in the 3D view (#84): the DC setup's VRMs, loads and series links,
marked on their pads over the copper (not over results)."""

import os
from types import MethodType
from types import SimpleNamespace as NS

import numpy as np
import pytest

from kipi.dc.pi.model import DcLink, PinRef
from kipi.dc.viz.markers import LIFT_MM, pi_markers
from phase_boards import T, board, two_phase
from shapely.geometry import box
from tree_boards import B, pad


def _by_name(markers):
    return {m.name: m for m in markers}


def test_markers_sit_on_the_pads_of_the_shown_nets():
    snap, setup = board(), two_phase(ground=True)
    top, bottom_cu_top = 1.5 + 2 * T + LIFT_MM, T + LIFT_MM
    m = _by_name(pi_markers(snap, setup, {"VOUT"}))
    assert set(m) == {"VRM U1", "Load U2"}                # J1 is only on VIN and GND
    assert m["VRM U1"].kind == "vrm" and m["Load U2"].kind == "load"
    np.testing.assert_allclose(m["VRM U1"].power, [[0.5, 0.5, top], [19.5, 0.5, top]])
    assert len(m["VRM U1"].ground) == 0                  # GND is not shown
    np.testing.assert_allclose(m["Load U2"].anchor, [5.5, 0.5, top])

    m = _by_name(pi_markers(snap, setup, {"VOUT", "VIN", "GND"}))
    assert len(m["VRM U1"].power) == 4                    # both phases' output and input
    np.testing.assert_allclose(m["VRM U1"].ground, [[9.5, 0.5, bottom_cu_top],
                                                    [19.5, 0.5, bottom_cu_top]])
    assert len(m["VRM J1"].power) == 1 and len(m["VRM J1"].ground) == 1
    assert pi_markers(snap, setup, set()) == [] and pi_markers(None, setup, {"VOUT"}) == []


def test_links_and_parts_under_the_board():
    from kipi.core.model.board import Footprint
    snap, setup = board(), two_phase()
    setup.links.append(DcLink("FB1", PinRef("L1", "VOUT", ["2"]), PinRef("Q1", "VIN", ["1"]),
                              0.01))
    snap.footprints.append(Footprint("fq1", "Q1", "", (9.5, 3.5), "bottom", ["q1_1"]))
    m = _by_name(pi_markers(snap, setup, {"VOUT", "VIN"}))
    link = m["Link FB1"]
    assert link.kind == "link" and len(link.power) == 2
    # Q1 is under the board: its pad is marked below the top copper's bottom face.
    q1 = next(p for p in link.power if p[0] == 9.5)
    assert q1[2] == pytest.approx(1.5 + T - LIFT_MM)


def test_capacitors_with_a_model_are_marked_by_reference():
    from kipi.ac.model import CapModel
    snap, setup = board(), two_phase()
    snap.pads.append(pad("c1_1", "VOUT", "C1", "1", box(2, 0, 3, 1)))
    snap.pads.append(pad("c1_2", "GND", "C1", "2", box(2, 0, 3, 1), B))
    snap.pads.append(pad("c2_1", "VOUT", "C2", "1", box(3, 0, 4, 1)))
    setup.caps.append(CapModel("10u", 10e-6, 1e-9, 0.005, ["C1"]))   # C2 has no model
    m = _by_name(pi_markers(snap, setup, {"VOUT", "GND"}))
    assert "C2" not in m and m["C1"].kind == "cap"
    np.testing.assert_allclose(m["C1"].power, [[2.5, 0.5, 1.5 + 2 * T + LIFT_MM]])
    np.testing.assert_allclose(m["C1"].ground, [[2.5, 0.5, T + LIFT_MM]])  # GND by name
    assert "C1" not in _by_name(pi_markers(snap, setup, {"VIN"}))


# --- Drawing -----------------------------------------------------------------------------

pv = pytest.importorskip("pyvista")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kipi.gui.viewer3d import Viewer3D  # noqa: E402


def _view():
    v = NS(plotter=pv.Plotter(off_screen=True), _models={}, _model_actors={}, _board_top=0.0,
           _model_board=0.0, _zscale=1.0, _show_models=True, _show_models_result=False,
           _show_board=True, _show_vias=True, _show_pi=True, _pi_markers=[], _pi_actors={},
           _result=None, _result_field="voltage", _chunk_actors={}, _result_actors={},
           _arrow_actors={}, _result_via_actor=None, _board_actors=[], _hidden_layers=set())
    for name in ("set_pi_markers", "_draw_pi_markers", "_remove_pi_actors", "_apply_visibility", "set_zscale",
                 "set_feature_visible", "_place_models"):
        setattr(v, name, MethodType(getattr(Viewer3D, name), v))
    return v


def _visible(v):
    return {n for n, a in v._pi_actors.items() if a.GetVisibility()}


def test_markers_are_drawn_over_the_copper_only():
    v = _view()
    v.set_pi_markers(pi_markers(board(), two_phase(ground=True), {"VOUT", "GND"}))
    names = {"pi_vrm_power", "pi_vrm_ground", "pi_vrm_labels", "pi_load_power",
             "pi_load_ground", "pi_load_labels"}
    assert set(v._pi_actors) == names == _visible(v)
    assert not any(a.GetPickable() for a in v._pi_actors.values())
    v.set_feature_visible("pi", False)
    assert _visible(v) == set()
    v.set_feature_visible("pi", True)
    v._result = object()                                  # results are shown
    v._apply_visibility()
    assert _visible(v) == set()
    v._result = None
    v._apply_visibility()
    assert _visible(v) == names
    props = v.plotter.renderer.GetViewProps()
    assert all(props.IsItemPresent(a) for a in v._pi_actors.values())
    v.set_pi_markers([])                                  # AC mode
    assert v._pi_actors == {} and props.GetNumberOfItems() == 0


def test_markers_follow_the_z_scale():
    v = _view()
    v.set_pi_markers(pi_markers(board(), two_phase(), {"VOUT"}))
    z = lambda: v._pi_actors["pi_load_power"].GetMapper().GetInput().GetPoint(0)[2]  # noqa: E731
    z1 = z()
    v.set_zscale(4.0)
    assert z() == pytest.approx(4 * z1)


# --- The window ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_window_marks_the_dc_setup_in_dc_mode(app, no_3d, monkeypatch):
    from qtpy.QtWidgets import QCheckBox
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    labels = [cb.text() for cb in w.findChildren(QCheckBox)]
    assert "PI Models" in labels and "3D Models" in labels and "Models" not in labels
    assert not w.chk_pi.isChecked() and w.chk_models.text() == "3D Models"
    w.snapshot = board()
    w.solve_panel.setup = two_phase()
    monkeypatch.setattr(w, "_checked_set", lambda: frozenset({"VOUT"}))
    drawn = []
    w.viewer.set_pi_markers = lambda markers: drawn.append([m.name for m in markers])
    w._sync_pi_markers()
    assert drawn[-1] == ["VRM U1", "Load U2"]
    w.set_mode("ac")
    assert drawn[-1] == []
    w.set_mode("dc")
    assert drawn[-1] == ["VRM U1", "Load U2"]
    w.close()
