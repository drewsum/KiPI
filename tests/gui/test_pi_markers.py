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


class _Timer:
    """Stands in for the blink QTimer: the test steps it by hand."""
    active = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False


def _view():
    v = NS(plotter=pv.Plotter(off_screen=True), _models={}, _model_actors={}, _board_top=0.0,
           _model_board=0.0, _zscale=1.0, _show_models=True, _show_models_result=False,
           _show_board=True, _show_vias=True, _show_pi=True, _pi_markers=[], _pi_actors={},
           _pi_selected=None, _blink_left=0, _blink_timer=_Timer(),
           _result=None, _result_field="voltage", _chunk_actors={}, _result_actors={},
           _arrow_actors={}, _result_via_actor=None, _board_actors=[], _hidden_layers=set())
    for name in ("set_pi_markers", "_draw_pi_markers", "_remove_pi_actors", "_apply_visibility", "set_zscale",
                 "set_feature_visible", "_place_models", "_selected_marker", "set_pi_selected",
                 "pi_marker_at", "_to_screen", "focus_on", "_start_blink", "_blink_step",
                 "_paint_selected"):
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


def test_ac_mode_draws_its_probes_in_their_own_color():
    from kipi.ac.model import Probe
    from kipi.ac.viz.markers import ac_markers
    from kipi.gui.viewer3d import _PI_COLORS
    v = _view()
    setup = two_phase(ground=True)
    setup.probes.append(Probe("U2 core", PinRef("U2", "VOUT", ["1"]),
                              PinRef("U2", "GND", ["2"])))
    v.set_pi_markers(ac_markers(board(), setup, {"VOUT", "GND"}))
    assert set(v._pi_actors) == {"pi_vrm_power", "pi_vrm_ground", "pi_vrm_labels",
                                "pi_probe_power", "pi_probe_ground",
                                "pi_probe_labels"} == _visible(v)
    dots = v._pi_actors["pi_probe_power"]
    np.testing.assert_allclose(dots.GetProperty().GetColor(), _PI_COLORS["probe"][0])
    assert _PI_COLORS["probe"] not in [c for k, c in _PI_COLORS.items() if k != "probe"]
    v.set_feature_visible("pi", False)
    assert _visible(v) == set()


def test_markers_follow_the_z_scale():
    v = _view()
    v.set_pi_markers(pi_markers(board(), two_phase(), {"VOUT"}))
    z = lambda: v._pi_actors["pi_load_power"].GetMapper().GetInput().GetPoint(0)[2]  # noqa: E731
    z1 = z()
    v.set_zscale(4.0)
    assert z() == pytest.approx(4 * z1)


def test_the_part_selected_in_the_setup_is_marked():
    """#96: larger amber dots on its pads and its name, over its own marker."""
    v = _view()
    v.set_pi_markers(pi_markers(board(), two_phase(ground=True), {"VOUT", "GND"}))
    v.set_pi_selected(("vrm", "U1"))
    dots = v._pi_actors["pi_selected_dots"]
    vrm = next(m for m in v._pi_markers if m.item == "U1")
    assert dots.GetMapper().GetInput().GetNumberOfPoints() == len(vrm.power) + len(vrm.ground)
    assert "pi_selected_label" in v._pi_actors
    names = list(v._pi_actors)
    assert names[-2:] == ["pi_selected_dots", "pi_selected_label"]      # drawn last
    v.set_feature_visible("pi", False)                    # shown with the markers
    assert not dots.GetVisibility()
    v.set_pi_selected(("load", "nothing such"))
    assert "pi_selected_dots" not in v._pi_actors
    assert not v._blink_timer.active                      # nothing selected to blink
    v.set_pi_selected(None)
    assert "pi_selected_label" not in v._pi_actors


def test_the_selected_part_blinks_then_stays_amber():
    from kipi.gui.viewer3d import _PI_BLINK_COLORS, _PI_BLINKS, _PI_SELECTED_COLORS
    v = _view()
    v.set_pi_markers(pi_markers(board(), two_phase(ground=True), {"VOUT", "GND"}))
    v.set_pi_selected(("vrm", "U1"))
    assert v._blink_timer.active

    def colors():
        dots = v._pi_actors["pi_selected_dots"].GetProperty().GetColor()
        label = (v._pi_actors["pi_selected_label"].GetMapper().GetLabelTextProperty()
                 .GetBackgroundColor())
        return tuple(round(c, 6) for c in (*dots, *label))

    blink = tuple(round(c, 6) for c in (*_PI_BLINK_COLORS[0], *_PI_BLINK_COLORS[1]))
    amber = tuple(round(c, 6) for c in (*_PI_SELECTED_COLORS[0], *_PI_SELECTED_COLORS[1]))

    seen = []
    for _ in range(2 * _PI_BLINKS):
        v._blink_step()
        seen.append(colors())
    assert seen == [blink, amber] * _PI_BLINKS            # ends amber
    assert not v._blink_timer.active
    v.set_pi_selected(("load", "U2"))                     # another part blinks afresh
    assert v._blink_timer.active and v._blink_left == 2 * _PI_BLINKS


def _looking_down(v):
    v.plotter.view_xy()
    v.plotter.reset_camera(bounds=(0, 25, -10, 5, 0, 2))
    v.plotter.render()


def test_a_click_on_a_name_or_dot_finds_its_marker():
    v = _view()
    v.set_pi_markers(pi_markers(board(), two_phase(ground=True), {"VOUT", "GND"}))
    _looking_down(v)
    load = next(m for m in v._pi_markers if m.kind == "load")
    anchor = [*load.anchor[:2], load.anchor[2] + 1.0]     # the name sits above the dots
    (x, y), = v._to_screen(np.array([anchor]))[0]
    assert v.pi_marker_at((x + 5, y + 3)) is load
    (px, py) = v._to_screen(load.power)[0][0]
    assert v.pi_marker_at((px + 2, py)) is not None
    assert v.pi_marker_at((x + 2000, y)) is None
    v.set_feature_visible("pi", False)                    # hidden markers are not clicked
    assert v.pi_marker_at((x, y)) is None


def test_focus_brings_the_view_to_the_part():
    v = _view()
    _looking_down(v)
    cam = v.plotter.renderer.GetActiveCamera()
    before = np.linalg.norm(np.array(cam.GetPosition()) - cam.GetFocalPoint())
    v.focus_on(np.array([[10.0, 2.0, 1.6], [12.0, 2.0, 1.6]]))
    np.testing.assert_allclose(cam.GetFocalPoint(), [11.0, -2.0, 1.6])
    after = np.linalg.norm(np.array(cam.GetPosition()) - cam.GetFocalPoint())
    assert after < before                                 # nearer: a part, not the board
    np.testing.assert_allclose(cam.GetDirectionOfProjection(), [0, 0, -1], atol=1e-9)


# --- The window ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_window_marks_each_modes_parts(app, no_3d, monkeypatch):
    """DC mode marks the loads; AC mode marks the probes in their place (#84)."""
    from qtpy.QtWidgets import QCheckBox
    from kipi.ac.model import Probe
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    labels = [cb.text() for cb in w.findChildren(QCheckBox)]
    assert "PI Models" in labels and "3D Models" in labels and "Models" not in labels
    assert not w.chk_pi.isChecked() and w.chk_models.text() == "3D Models"
    w.snapshot = board()
    w.solve_panel.setup = two_phase()
    w.solve_panel.setup.probes.append(Probe("U2 core", PinRef("U2", "VOUT", ["1"]),
                                            PinRef("U2", "GND", ["2"])))
    monkeypatch.setattr(w, "_checked_set", lambda: frozenset({"VOUT"}))
    drawn = []
    w.viewer.set_pi_markers = lambda markers: drawn.append([m.name for m in markers])
    w._sync_pi_markers()
    assert drawn[-1] == ["VRM U1", "Load U2"]
    w.set_mode("ac")
    assert drawn[-1] == ["VRM U1", "Probe U2 core"]
    w.set_mode("dc")
    assert drawn[-1] == ["VRM U1", "Load U2"]
    w.snapshot = None                                     # no board: nothing to mark
    w._sync_pi_markers()
    assert drawn[-1] == []
    w.close()


def test_picking_a_part_in_the_setup_marks_it_and_goes_there(app, no_3d, monkeypatch):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    w.snapshot = board()
    w.solve_panel.setup = two_phase()
    w.solve_panel.vrm_list.reload()
    w.solve_panel.load_list.reload()
    monkeypatch.setattr(w, "_checked_set", lambda: frozenset({"VOUT"}))
    w._sync_pi_markers()
    marked, focused = [], []
    monkeypatch.setattr(w.viewer, "set_pi_selected", marked.append)
    monkeypatch.setattr(w.viewer, "focus_on", focused.append)
    w.chk_snap.blockSignals(True)                 # not saved to the user's settings
    w.chk_snap.setChecked(True)
    lst = w.solve_panel.load_list
    w.solve_panel.tabs.setCurrentWidget(lst)
    assert marked[-1] == ("load", "U2") and not focused       # shown, not flown to
    assert not w.chk_pi.isChecked() and not w.chk_snap.isEnabled()
    lst.lst.itemClicked.emit(lst.lst.item(0))                 # the user clicks it
    assert not w.chk_pi.isChecked() and not focused           # PI Models stays off
    w.chk_pi.setChecked(True)
    assert w.chk_snap.isEnabled()
    lst.lst.itemClicked.emit(lst.lst.item(0))
    assert len(focused) == 1
    np.testing.assert_allclose(focused[0][:, :2].mean(axis=0), [5.5, 0.5])
    w.chk_snap.setChecked(False)                              # marked, but the view stays
    lst.lst.itemClicked.emit(lst.lst.item(0))
    assert marked[-1] == ("load", "U2") and len(focused) == 1
    w.solve_panel.tabs.setCurrentIndex(3)                     # the Solve tab: none
    assert marked[-1] is None
    w.close()


def test_clicking_a_name_in_3d_selects_the_part(app, no_3d, monkeypatch):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    w.snapshot = board()
    w.solve_panel.setup = two_phase()
    for il in (w.solve_panel.vrm_list, w.solve_panel.load_list):
        il.reload()
    monkeypatch.setattr(w, "_checked_set", lambda: frozenset({"VOUT"}))
    w._sync_pi_markers()
    load = next(m for m in w.viewer.pi_markers if m.kind == "load")
    monkeypatch.setattr(w.viewer, "pi_marker_at", lambda xy: load)
    picked = []
    monkeypatch.setattr(w, "_on_pick", lambda *a: picked.append(a))
    w._on_click((5.5, 0.5, 1.6), (10, 10))
    assert w.solve_panel.tabs.currentWidget() is w.solve_panel.load_list
    assert w.solve_panel.load_list.current().name == "U2"
    assert picked == []                                       # not a copper pick
    w.close()


def _ac_window(monkeypatch):
    from kipi.ac.model import Probe
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    w.snapshot = board()
    setup = two_phase()
    setup.probes.append(Probe("U2 core", PinRef("U2", "VOUT", ["1"]),
                              PinRef("U2", "GND", ["2"])))
    w.solve_panel.setup = w.ac_panel.setup = setup         # the panels share one setup
    monkeypatch.setattr(w, "_checked_set", lambda: frozenset({"VOUT"}))
    w.solve_panel.load_list.reload()
    w.set_mode("ac")
    for il in w.ac_panel._lists():
        il.reload()
    return w


def test_picking_a_probe_in_the_ac_setup_marks_it_and_goes_there(app, no_3d, monkeypatch):
    w = _ac_window(monkeypatch)
    marked, focused = [], []
    monkeypatch.setattr(w.viewer, "set_pi_selected", marked.append)
    monkeypatch.setattr(w.viewer, "focus_on", focused.append)
    lst = w.ac_panel.probe_list
    w.ac_panel.tabs.setCurrentWidget(lst)
    assert marked[-1] == ("probe", "U2 core") and not focused  # shown, not flown to
    assert not w.chk_pi.isChecked()
    lst.lst.itemClicked.emit(lst.lst.item(0))                  # the user clicks it
    assert not w.chk_pi.isChecked() and not focused            # PI Models stays off
    w.chk_snap.blockSignals(True)                  # not saved to the user's settings
    w.chk_snap.setChecked(True)
    w.chk_pi.setChecked(True)
    lst.lst.itemClicked.emit(lst.lst.item(0))
    assert len(focused) == 1
    np.testing.assert_allclose(focused[0][:, :2].mean(axis=0), [5.5, 0.5])
    w.ac_panel.tabs.setCurrentWidget(w.ac_panel.cap_list)      # capacitors: none
    assert marked[-1] is None
    w.ac_panel.tabs.setCurrentWidget(w.ac_panel.vrm_list)
    assert marked[-1] == ("vrm", "J1")
    # The DC panel's lists say nothing while AC mode shows, and the mode switch brings
    # back its own selection.
    w.solve_panel.tabs.setCurrentWidget(w.solve_panel.load_list)
    assert marked[-1] == ("vrm", "J1")
    w.set_mode("dc")
    assert marked[-1][0] == "load"
    w.set_mode("ac")
    assert marked[-1] == ("vrm", "J1")
    w.close()


def test_clicking_a_marker_in_ac_mode_selects_it_in_the_ac_setup(app, no_3d, monkeypatch):
    from kipi.ac.viz.markers import ac_markers
    w = _ac_window(monkeypatch)
    probe = next(m for m in ac_markers(w.snapshot, w.ac_panel.setup, {"VOUT"})
                 if m.kind == "probe")
    monkeypatch.setattr(w.viewer, "pi_marker_at", lambda xy: probe)
    w._on_click(None, (10, 10))
    assert w.ac_panel.tabs.currentWidget() is w.ac_panel.probe_list
    assert w.ac_panel.probe_list.current().name == "U2 core"
    assert w.solve_panel.tabs.currentWidget() is not w.solve_panel.load_list
    w.close()
