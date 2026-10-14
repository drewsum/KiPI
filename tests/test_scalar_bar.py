"""The results scale bar: tick values left of the bar, title clear above, and a color
range fitted to the layers that are shown."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

pv = pytest.importorskip("pyvista")
pytest.importorskip("pyvistaqt")

from kipi.gui.viewer3d import Viewer3D, _copper_clim  # noqa: E402
from kipi.dc.pi.tree import solve_tree  # noqa: E402
from kipi.dc.viz.results import FIELDS, build_result_scene  # noqa: E402
from test_multiphase import board, two_phase  # noqa: E402


@pytest.mark.parametrize("field", ["voltage", "current_density", "power_density",
                                   "via_current"])
def test_scale_bar_layout(field):
    snap = board()
    tree = solve_tree(snap, two_phase())
    scene = build_result_scene(snap, next(iter(tree.groups.values())).result)
    if scene.vias is None:                   # the test board has none: stand one in
        scene.vias = pv.Cylinder()
        scene.vias.cell_data["I_mA"] = range(scene.vias.n_cells)
        scene.ranges["via_current"] = (0.0, float(scene.vias.n_cells))
    # Just the drawing state Viewer3D needs, on an off-screen plotter (no Qt window).
    v = NS(plotter=pv.Plotter(off_screen=True), _result=scene, _result_field=field,
           _result_actors={}, _result_via_actor=None, _scalar_bars=[], _zscale=1.0,
           _hidden_layers=set())
    v._style_bar = lambda title: Viewer3D._style_bar(v, title)
    _, assoc, label, unit = FIELDS[field]
    title = f"{label} ({unit})"
    if assoc == "via":
        Viewer3D._draw_via_result(v, scene, FIELDS[field][0], title)
    else:
        Viewer3D._draw_copper_result(v, scene, FIELDS[field][0], assoc, title)
    bar = v.plotter.scalar_bars[title]
    assert bar.GetTextPosition() == bar.PrecedeScalarBar
    assert bar.GetVerticalTitleSeparation() > 0
    assert v._scalar_bars == [title]
    v.plotter.close()


def _scene_with_outlier_layer():
    """The test board's copper plus a copy on another layer sitting 1 V higher."""
    snap = board()
    tree = solve_tree(snap, two_phase())
    scene = build_result_scene(snap, next(iter(tree.groups.values())).result)
    lid, lr = next(iter(scene.layers.items()))
    mesh = lr.mesh.copy()
    mesh.point_data["V"] = mesh.point_data["V"] + 1.0
    scene.layers[99] = replace(lr, layer_id=99, mesh=mesh)
    return scene, lid


def test_color_range_follows_visible_layers():
    scene, lid = _scene_with_outlier_layer()
    v_lo, v_hi = (float(f(scene.layers[lid].mesh.point_data["V"])) for f in (min, max))
    lo, hi, _ = _copper_clim(scene, "voltage")
    assert (lo, hi) == pytest.approx((v_lo, v_hi + 1.0))
    lo, hi, _ = _copper_clim(scene, "voltage", {99})
    assert (lo, hi) == pytest.approx((v_lo, v_hi))
    # Nothing shown: keep the full range rather than collapse.
    lo, hi, _ = _copper_clim(scene, "voltage", {lid, 99})
    assert (lo, hi) == pytest.approx((v_lo, v_hi + 1.0))


def test_hiding_a_layer_rescales_the_drawn_result():
    scene, lid = _scene_with_outlier_layer()
    v = NS(plotter=pv.Plotter(off_screen=True), _result=scene, _result_field="voltage",
           _result_actors={}, _result_via_actor=None, _scalar_bars=[], _zscale=1.0,
           _hidden_layers=set())
    v._style_bar = lambda title: Viewer3D._style_bar(v, title)
    Viewer3D._draw_copper_result(v, scene, "V", "point", "Voltage (V)")
    v._hidden_layers.add(99)
    Viewer3D._rescale_result(v)
    want = _copper_clim(scene, "voltage", {99})[:2]
    for actor in v._result_actors.values():
        assert actor.GetMapper().GetScalarRange() == pytest.approx(want)
        assert actor.GetMapper().GetLookupTable().GetRange() == pytest.approx(want)
    v.plotter.close()
