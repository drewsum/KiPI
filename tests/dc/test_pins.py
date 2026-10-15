"""Every solved pin's voltage and current in the reports (#71)."""

import csv
from types import SimpleNamespace

import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Zone,
)
from kipi.dc.pi.report import PIN_CURRENT_SHARED, PIN_CURRENT_SOLVED, export_csv, pin_rows
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver import CurrentLoad, NetProblem, SeriesLink, SolveSettings, VoltageSource, run
from kipi.dc.solver.post import SeriesResult, TerminalResult
from tree_boards import board, setup

F, B = 3, 34
T = 0.035


@pytest.fixture(scope="module")
def solved():
    snap = board()
    tree = solve_tree(snap, setup("ldo", iq=0.01, ground=True))
    return snap, tree, pin_rows(tree, snap)


def test_every_terminal_pin_is_listed(solved):
    snap, tree, rows = solved
    for name, g in tree.groups.items():
        got = [(r.terminal, r.part, r.pin) for r in rows if r.group == name]
        by_id = {p.id: p for p in snap.pads}
        want = [(t.name, by_id[pid].footprint, by_id[pid].number)
                for t in g.result.fields.terminals for pid in t.pad_ids]
        assert sorted(got) == sorted(want) and got
        assert all(r.kind == g.kind for r in rows if r.group == name)


def test_pin_currents_add_up(solved):
    _snap, tree, rows = solved
    for name, g in tree.groups.items():
        mine = [r for r in rows if r.group == name]
        for t in g.result.fields.terminals:
            pins = [r for r in mine if r.terminal == t.name]
            drawn = sum(r.current for r in pins)
            assert drawn == pytest.approx(-t.current if t.kind == "source" else t.current,
                                          abs=1e-12)
            assert {r.current_is for r in pins} == {PIN_CURRENT_SOLVED if t.kind == "source"
                                                    else PIN_CURRENT_SHARED}
        assert sum(r.current for r in mine) == pytest.approx(0.0, abs=1e-9)   # KCL


def test_load_pins_match_the_load_table(solved):
    _snap, tree, rows = solved
    for l in tree.loads:
        vs = [r.v for r in rows if r.terminal == l.name and r.group == l.group]
        assert min(vs) == pytest.approx(l.v_supply_min)
        assert sum(vs) / len(vs) == pytest.approx(l.v_supply)


def _strip(pads):
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    nets = sorted({p.net for p in pads})
    zones = [Zone(f"z{n}", n, "", 0, {F: [Poly(list(g.exterior.coords)[:-1], [])]})
             for n, g in (("A", box(0, 0, 10, 1)), ("B", box(12, 0, 22, 1))) if n in nets]
    return BoardSnapshot("pins", "", st, [Net(i + 1, n) for i, n in enumerate(nets)], [], [],
                         pads, [], zones, [])


def _pad(pid, net, ref, num, g):
    return Pad(pid, net, ref, num, (g.centroid.x, g.centroid.y), "smd",
               shapes={F: [Poly(list(g.exterior.coords)[:-1], [])]})


def _tree(res, name="A"):
    return SimpleNamespace(groups={name: SimpleNamespace(name=name, kind="rail", result=res)})


def test_source_pins_share_by_the_copper():
    """Two VRM output pins either end of a strip, the load near one: that pin delivers more."""
    snap = _strip([_pad("s1", "A", "U1", "1", box(0, 0, 1, 1)),
                   _pad("s2", "A", "U1", "2", box(9, 0, 10, 1)),
                   _pad("ld", "A", "U2", "1", box(2, 0, 3, 1))])
    res = run(snap, NetProblem("A", [VoltageSource("U1 (output)", ["s1", "s2"], 1.0)],
                               [CurrentLoad("U2", ["ld"], 1.0)], SolveSettings(max_edge_mm=0.3)))
    rows = {r.pin: r for r in pin_rows(_tree(res), snap) if r.part == "U1"}
    # 1 mm of strip to pin 1, 6 mm to pin 2 (pads are equipotential).
    assert rows["1"].current == pytest.approx(-6 / 7, rel=1e-6)
    assert rows["2"].current == pytest.approx(-1 / 7, rel=1e-6)
    assert rows["1"].current_is == PIN_CURRENT_SOLVED


def test_dc_connection_pins():
    snap = _strip([_pad("src", "A", "J1", "1", box(0, 0, 1, 1)),
                   _pad("r1a", "A", "R1", "1", box(9, 0, 10, 1)),
                   _pad("r1b", "B", "R1", "2", box(12, 0, 13, 1)),
                   _pad("ld", "B", "U1", "1", box(21, 0, 22, 1))])
    res = run(snap, NetProblem("A + B", [VoltageSource("VRM", ["src"], 1.0)],
                               [CurrentLoad("U1", ["ld"], 2.0)], SolveSettings(max_edge_mm=0.3),
                               series=[SeriesLink("R1", ["r1a"], ["r1b"], 0.02)],
                               nets=["A", "B"]))
    rows = {(r.part, r.pin): r for r in pin_rows(_tree(res, "A + B"), snap)}
    a, b = rows[("R1", "1")], rows[("R1", "2")]
    assert a.role == b.role == "DC connection"
    assert a.current == pytest.approx(2.0) and b.current == pytest.approx(-2.0)
    assert a.v - b.v == pytest.approx(2.0 * 0.02, rel=1e-9)


def test_results_saved_before_pins_were_kept():
    t = TerminalResult.__new__(TerminalResult)
    t.__setstate__({"name": "U2", "kind": "load", "current": 1.0, "v_pads": [3.3],
                    "v_min": 3.3, "v_max": 3.3})
    s = SeriesResult.__new__(SeriesResult)
    s.__setstate__({"name": "R1", "resistance": 0.0, "current": 1.0, "v_a": 1.0, "v_b": 1.0,
                    "power": 0.0})
    fields = SimpleNamespace(terminals=[t], series=[s], v_ref=3.3)
    tree = _tree(SimpleNamespace(fields=fields))
    assert pin_rows(tree, board()) == []


def test_pins_csv(solved, tmp_path):
    snap, tree, rows = solved
    export_csv(tree, snap, tmp_path, stem="t")
    with open(tmp_path / "t_pins.csv", encoding="utf-8") as fh:
        got = list(csv.DictReader(fh))
    assert len(got) == len(rows)
    r = got[0]
    assert r["role"] == "source" and r["current_is"] == PIN_CURRENT_SOLVED
    assert float(r["v_V"]) == pytest.approx(rows[0].v, rel=1e-8)
    assert r["deviation_is"] in ("drop", "boost")
