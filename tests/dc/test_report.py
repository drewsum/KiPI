"""Result tables, exports and saved results."""

import csv

import pytest

from kipi.dc.pi.report import (
    export_csv, export_vtk, load_results, loss_rows, save_results, via_rows,
)
from kipi.dc.pi.tree import solve_tree
from kipi.dc.viz.results import build_result_scene
from tree_boards import board, setup


@pytest.fixture(scope="module")
def solved():
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    tree = solve_tree(snap, s)
    scenes = {n: build_result_scene(snap, g.result) for n, g in tree.groups.items()}
    return snap, s, tree, scenes


def test_loss_rows_add_up_to_each_groups_loss(solved):
    snap, _, tree, _ = solved
    rows = {r.group: r for r in loss_rows(tree, snap)}
    for name, g in tree.groups.items():
        assert rows[name].total == pytest.approx(g.result.fields.summary["power_loss"], rel=1e-9)
    # The VIN strip is all on F.Cu.
    assert set(rows["VIN"].by_layer) == {"F.Cu"}


def test_via_rows_ranked_with_limit(tmp_path):
    from kipi.core.model.board import Via
    snap = board()
    # Feed the +3V3 load from B.Cu through a via: add B.Cu copper and a via.
    from shapely.geometry import box
    from tree_boards import F, B, poly
    from kipi.core.model.board import Zone
    snap.zones.append(Zone("vb", "+3V3", "", 0, {B: [poly(box(12, 2, 22, 3))]}))
    snap.vias.append(Via("v1", "+3V3", (17.0, 0.5), 0.3, 0.6, [F, B]))
    snap.invalidate_index()
    tree = solve_tree(snap, setup("ldo"))
    rows = via_rows(tree, snap, limit_a=1e-3)
    assert rows and rows == sorted(rows, key=lambda r: -r.current)
    assert rows[0].passed == (rows[0].current <= 1e-3)


def test_csv_export(solved, tmp_path):
    snap, _, tree, _ = solved
    paths = export_csv(tree, snap, tmp_path, stem="t", via_limit_a=0.5)
    names = {p.name for p in paths}
    assert {"t_loads.csv", "t_vrms.csv", "t_vias.csv", "t_losses.csv",
            "t_dc_connections.csv", "t_load_pin_groups.csv"} <= names
    with open(tmp_path / "t_loads.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["load"] == "U2" and rows[0]["result"] in ("PASS", "FAIL")
    assert rows[0]["mean_is"] == "drop"
    assert float(rows[0]["mean_mV"]) == pytest.approx(tree.loads[0].drop * 1e3, rel=1e-6)


def test_vtk_export_and_results_round_trip(solved, tmp_path):
    import pyvista as pv
    snap, s, tree, scenes = solved
    (vtm,) = [p for p in export_vtk(tree, scenes, snap, tmp_path, "t") if "3V3" in p.name]
    mb = pv.read(str(vtm))
    assert "F.Cu" in mb.keys() and "V" in mb["F.Cu"].point_data
    path = save_results(tmp_path / "r.kipi-results", tree, scenes, snap, s)
    back = load_results(path)
    assert back["board"] == snap.board_name and back["setup"] == s
    assert [l.drop for l in back["tree"].loads] == [l.drop for l in tree.loads]
    sc = back["scenes"]["+3V3"]
    assert sc.probe(3, 17.0, 0.5)["V"] == pytest.approx(scenes["+3V3"].probe(3, 17.0, 0.5)["V"])
