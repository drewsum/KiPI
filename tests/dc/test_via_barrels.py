"""The DC result's via barrels, built from one unit cylinder (#217): each where and as
big as a pyvista cylinder of its own would be, coloured by its current."""

from types import SimpleNamespace as NS

import numpy as np
import pytest

pv = pytest.importorskip("pyvista")

from kipi.dc.viz.results import _via_barrels  # noqa: E402
from synthetic import make_board  # noqa: E402


def test_barrels_match_one_cylinder_each():
    st = make_board().stackup
    ids = [cu.id for cu in st.copper]
    bars = [NS(layers=ids, position=(1.0, 2.0), drill_mm=0.3, owner_id="v1"),
            NS(layers=ids[:2], position=(5.0, -1.0), drill_mm=0.01, owner_id="v2"),
            NS(layers=[], position=(0.0, 0.0), drill_mm=0.3, owner_id="none")]
    result = NS(mesh=NS(barrels=bars),
                fields=NS(vias=[NS(current=0.5), NS(current=-0.2), NS(current=9.0)]))
    mesh, rng, segments, got_ids = _via_barrels(st, result, 16)
    assert got_ids == ["v1", "v2"] and rng == (0.0, 500.0)
    per = mesh.n_cells // 2
    assert np.array_equal(mesh.cell_data["I_mA"], np.repeat([500.0, -200.0], per))
    for k, bar in enumerate(bars[:2]):
        layers = [st.layer(l) for l in bar.layers]
        top, bot = max(l.z_top_mm for l in layers), min(l.z_bottom_mm for l in layers)
        x, y = bar.position
        ref = pv.Cylinder(center=(x, -y, (top + bot) / 2), direction=(0, 0, 1),
                          radius=max(bar.drill_mm, 0.05) / 2, height=top - bot,
                          resolution=16, capping=True)
        one = mesh.extract_cells(np.arange(k * per, (k + 1) * per))
        assert np.allclose(one.bounds, ref.bounds)
        assert one.area == pytest.approx(ref.area, rel=1e-4)     # caps triangulated
        assert np.allclose(segments[k], [(x, y, top), (x, y, bot)])


def test_no_barrels():
    st = make_board().stackup
    mesh, rng, segments, ids = _via_barrels(st, NS(mesh=NS(barrels=[]), fields=NS(vias=[])), 16)
    assert mesh is None and rng == (0.0, 0.0) and segments.shape == (0, 2, 3) and ids == []
