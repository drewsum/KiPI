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
    assert "Plotly.newPlot" in text                     # interactive 3D for each group
    assert text.count("Plotly.newPlot") == len(tree.groups)
    assert text.count("data:image/png;base64,") >= 2 * len(tree.groups)
    for l in tree.loads:
        assert l.name in text
    assert ("Electro-thermal" in text) == thermal
    assert ("Copper temperature by layer" in text) == thermal
    import re
    assert not re.search(r"<script[^>]+src=", text)        # plotly.js embedded: works offline
