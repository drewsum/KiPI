"""The Display panel's opacity slider draws the copper layers and pads see-through."""

from types import SimpleNamespace as NS

import pytest

pv = pytest.importorskip("pyvista")
pytest.importorskip("pyvistaqt")

from kipi.gui.viewer3d import _CATEGORY_LOOK, Viewer3D, _chunk_actor  # noqa: E402
from kipi.dc.pi.tree import solve_tree  # noqa: E402
from kipi.dc.viz.results import FIELDS, build_result_scene  # noqa: E402
from phase_boards import board, two_phase  # noqa: E402


def _view(field):
    snap = board()
    tree = solve_tree(snap, two_phase())
    scene = build_result_scene(snap, next(iter(tree.groups.values())).result)
    if scene.vias is None:                   # the test board has none: stand one in
        scene.vias = pv.Cylinder()
        scene.vias.cell_data["I_mA"] = range(scene.vias.n_cells)
        scene.ranges["via_current"] = (0.0, float(scene.vias.n_cells))
    v = NS(plotter=pv.Plotter(off_screen=True), _result=scene, _result_field=field,
           _result_actors={}, _result_via_actor=None, _scalar_bars=[], _zscale=1.0,
           _hidden_layers=set(), _layer_opacity=1.0, _depth_peeling=False,
           _chunk_actors={})
    v._style_bar = lambda title: Viewer3D._style_bar(v, title)
    v._apply_result_opacity = lambda actor: Viewer3D._apply_result_opacity(v, actor)
    v._set_depth_peeling = lambda on: Viewer3D._set_depth_peeling(v, on)
    return v, scene


class _Chunk:
    def __init__(self, category):
        self.category = category


def _opacities(actors):
    return [a.GetProperty().GetOpacity() for a in actors]


def test_new_chunks_take_the_layer_opacity():
    assert _chunk_actor(("cu", 0), 0.5).GetProperty().GetOpacity() == pytest.approx(0.5)
    pad = _CATEGORY_LOOK["pad"][0] * 0.5
    assert _chunk_actor(("pad", 0), 0.5).GetProperty().GetOpacity() == pytest.approx(pad)
    assert _chunk_actor(("via", -1), 0.5).GetProperty().GetOpacity() == 1.0


@pytest.mark.parametrize("field, base", [("voltage", 1.0), ("via_current", 0.35)])
def test_slider_fades_results_and_chunks(field, base):
    v, scene = _view(field)
    def draw():
        if field == "via_current":
            Viewer3D._draw_via_result(v, scene, FIELDS[field][0], "I")
        else:
            Viewer3D._draw_copper_result(v, scene, "V", "point", "Voltage (V)")
    draw()
    v._chunk_actors = {_Chunk(("cu", 0)): _chunk_actor(("cu", 0)),
                       _Chunk(("via", -1)): _chunk_actor(("via", -1))}
    Viewer3D.set_layer_opacity(v, 0.4)
    assert _opacities(v._result_actors.values()) == pytest.approx(
        [base * 0.4] * len(v._result_actors))
    cu, via = v._chunk_actors.values()
    assert cu.GetProperty().GetOpacity() == pytest.approx(0.4)
    assert via.GetProperty().GetOpacity() == 1.0
    assert v._depth_peeling and v.plotter.renderer.GetUseDepthPeeling()
    Viewer3D.set_layer_opacity(v, 1.0)
    assert cu.GetProperty().GetOpacity() == 1.0 and not v._depth_peeling
    assert not v.plotter.renderer.GetUseDepthPeeling()
    # Results drawn after the change keep it.
    Viewer3D.set_layer_opacity(v, 0.25)
    v.plotter.clear_actors()
    v._result_actors.clear()
    v._scalar_bars.clear()
    draw()
    assert _opacities(v._result_actors.values())[0] == pytest.approx(base * 0.25)
    v.plotter.close()


def test_depth_peeling_skips_pyvistas_probe_window(monkeypatch):
    # pyvista's check renders in a hidden window of its own, which access-violates on
    # some drivers (Windows CI without a GPU); KiPI must never call it.
    from pyvista.plotting.utilities import gl_checks

    def probe(*a, **k):
        raise AssertionError("depth peeling probe called")
    monkeypatch.setattr(gl_checks, "check_depth_peeling", probe)
    import pyvista.plotting.renderer as renderer_mod
    monkeypatch.setattr(renderer_mod, "check_depth_peeling", probe, raising=False)
    v, _scene = _view("voltage")
    Viewer3D.set_layer_opacity(v, 0.5)
    assert v._depth_peeling
    v.plotter.close()


def test_depth_peeling_failure_is_ignored():
    def boom(*a, **k):
        raise RuntimeError("no depth peeling here")
    v = NS(plotter=NS(renderers=[NS(SetUseDepthPeeling=boom)]), _depth_peeling=False)
    Viewer3D._set_depth_peeling(v, True)
    assert not v._depth_peeling


def test_slider_updates_the_label(no_3d):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    got = []
    w.viewer.set_layer_opacity = got.append
    w.sld_opacity.setValue(40)
    assert w.lbl_opacity.text() == "40%" and got == [pytest.approx(0.4)]
    w.close()
