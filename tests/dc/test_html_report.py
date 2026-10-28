"""HTML report generation."""

import pytest

pytest.importorskip("plotly")
pytest.importorskip("matplotlib")

from kipi.dc.pi.tree import solve_tree
from kipi.dc.report.html import write_report
from kipi.dc.thermal.cosim import solve_electrothermal
from kipi.dc.thermal.model import ThermalSettings
from kipi.dc.viz.results import build_result_scene
from tree_boards import board, setup


@pytest.mark.parametrize("thermal", [False, True])
def test_report_contents(tmp_path, thermal):
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    if thermal:
        tree = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=2, grid_mm=0.5))
    else:
        tree = solve_tree(snap, s)
    temp = tree.thermal.sampler() if tree.thermal else None
    scenes = {n: build_result_scene(snap, g.result, temperature=temp) for n, g in tree.groups.items()}
    path = write_report(tmp_path / "r.html", tree, scenes, snap, s)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>")
    # an interactive 3D view for each group, drawn only when opened (#222)
    assert text.count("class='plot-data'") == len(tree.groups)
    assert "Plotly.newPlot" in text
    assert text.count("data:image/png;base64,") >= 2 * len(tree.groups)
    for l in tree.loads:
        assert l.name in text
    assert "id='pins'" in text and text.count("<details>") >= len(tree.groups)   # #71
    assert "Load line (mΩ)" in text and "V target" in text      # #94
    assert ("Electro-thermal" in text) == thermal
    assert ("Copper temperature by layer" in text) == thermal
    import re
    assert not re.search(r"<script[^>]+src=", text)        # plotly.js embedded: works offline
    # #122: the copper is lit evenly, with no facet shading to patch it with dark triangles
    assert text.count('"lighting":{"ambient":1.0,"diffuse":0.0') >= len(tree.groups)
    # #128: an editable notes box, last in the report, kept and saved by the page itself
    assert "contenteditable='true'" in text and "user-notes-save" in text
    assert text.rindex("<h2") == text.index("<h2 id='user-notes'")       # the last section
    assert "<style data-kipi>" in text      # a saved copy keeps the report's own styles
    # #222: the tables come after every plot, before the notes; images expand to full
    # screen, drawn at twice the size they are shown at
    last_plot = max(text.rindex("class='plot-data'"), text.rindex("data:image/png"))
    for table in ("id='loads'", "id='vrms'", "id='pins'", "id='losses'", "id='vias'",
                  "id='layers'"):
        assert last_plot < text.index(table) < text.index("id='user-notes'"), table
    assert ("id='thermal-tables'" in text) == thermal
    assert "lightbox" in text and "plot-expand" in text
    # each group's 3D view is collapsed until clicked open
    assert text.count("<details class='view3d'><summary>3D view: ") == len(tree.groups)


def test_report_mesh_keeps_copper(tmp_path):
    """The 3D view's thinned mesh uses only original vertices, so it keeps the copper's
    outline and holes and draws nothing off it (#102)."""
    import numpy as np
    import pyvista as pv
    from shapely.geometry import Point, Polygon

    from kipi.core.mesh.mesher import mesh_polygon
    from kipi.dc.report.html import _decimate

    poly = Polygon([(0, 0), (40, 0), (40, 25), (25, 25), (25, 35), (0, 35)])
    for k in range(20):
        poly = poly.difference(Point(3 + 1.7 * k, 5 + 1.3 * k).buffer(0.6, 6))
    pts, tris = mesh_polygon(poly, [], 0.05, 28)
    pts3 = np.column_stack([pts, np.zeros(len(pts))])
    sheet = pv.PolyData(pts3, np.column_stack([np.full(len(tris), 3), tris]).ravel())
    sheet.point_data["V"] = pts[:, 0]
    out = _decimate(sheet, 0.1)
    assert out.n_cells < sheet.n_cells / 3
    p = np.asarray(out.points)
    idx = {tuple(q) for q in np.round(pts3, 9)}
    assert all(tuple(q) in idx for q in np.round(p, 9))         # no vertex moved
    assert np.allclose(out.point_data["V"], p[:, 0])            # voltages stay with them
    t = out.faces.reshape(-1, 4)[:, 1:]
    area = 0.5 * np.abs(np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])[:, 2]).sum()
    assert area == pytest.approx(poly.area, rel=1e-6)           # nothing across holes


@pytest.mark.parametrize("drop, label", [(20e-6, "Drop from 3.3 V (µV)"), (0.15, "V")])
def test_narrow_voltage_maps_show_the_drop(monkeypatch, drop, label):
    """A rail that sags by µV would label its colorbar against an offset ("1e-5+3.2999"),
    so it is drawn as the drop from the target voltage; a plainly labelled map is left (#145)."""
    import numpy as np
    from types import SimpleNamespace
    from matplotlib.tri import Triangulation

    from kipi.dc.report import html
    from kipi.dc.report.html import _has_offset, _layer_map

    monkeypatch.setattr(html, "_png", lambda fig, dpi=110: fig)
    g = np.mgrid[0:20:21j, 0:10:11j].reshape(2, -1).T
    lr = SimpleNamespace(pts2=g, tris=Triangulation(g[:, 0], g[:, 1]).triangles)
    fig = _layer_map(lr, 3.3 - drop * g[:, 0] / 20, False, "F.Cu: voltage", "turbo", "V",
                     v_ref=3.3)
    ax, bar = fig.axes
    assert bar.get_ylabel() == label
    assert not _has_offset(fig, SimpleNamespace(ax=bar))
    assert ("3.299980 to 3.300000 V" in ax.get_title()) == (label != "V")


def test_each_group_has_layer_tickboxes(tmp_path):
    """A tickbox per copper layer over each group's 3D view hides or shows the layer in
    the view and its maps (#181)."""
    import re
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    tree = solve_tree(snap, s)
    scenes = {n: build_result_scene(snap, g.result) for n, g in tree.groups.items()}
    text = write_report(tmp_path / "r.html", tree, scenes, snap, s).read_text(encoding="utf-8")
    assert text.count("class='layer-toggles'") == len(tree.groups)
    names = {c.id: c.name for c in snap.stackup.copper}
    for gi, scene in enumerate(scenes.values()):
        bar = re.search(rf"<div class='layer-toggles' data-plot='plot{gi}'>(.*?)</div>",
                        text).group(1)
        assert re.findall(r"data-layer='([^']+)'", bar)[:len(scene.layers)] == [
            names[lid] for lid in scene.layers]
        for lid in scene.layers:      # its voltage and current-density maps
            assert text.count(f"<figure data-plot='plot{gi}' data-layer='{names[lid]}'>") == 2
    assert "Plotly.restyle" in text              # the tickboxes' script is in the page
