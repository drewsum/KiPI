"""The power tree block diagram (#58)."""

import pytest

from kipi.ac.model import CapModel
from kipi.dc.pi.diagram import build_diagram
from kipi.dc.pi.model import DcLink, Load, PinRef
from tree_boards import board, setup


def _tree():
    s = setup("ldo", ground=True)
    # A ferrite from +3V3 to a filtered rail, declared "backwards" (side A downstream).
    s.links.append(DcLink("FB1", PinRef("FB1", "+3V3_F", ["2"]), PinRef("FB1", "+3V3", ["1"]),
                          0.05))
    s.loads.append(Load("U3", PinRef("U3", "+3V3_F", ["1"]), 0.2))
    s.caps.append(CapModel("10u", 10e-6, 1e-9, 0.005, ["U2"]))   # U2's pads: +3V3 and GND
    return s


def test_blocks_and_vrm_types():
    d = build_diagram(_tree(), board())
    kinds = {bid: b.kind for bid, b in d.blocks.items()}
    assert kinds == {"vrm:J1": "vrm", "vrm:U1": "vrm", "rail:VIN": "rail", "rail:+3V3": "rail",
                     "rail:+3V3_F": "rail", "link:FB1": "link", "load:U2": "load",
                     "load:U3": "load", "cap:10u:+3V3": "cap"}       # no ground rail
    assert d.blocks["vrm:J1"].vrm_kind == "source"
    assert d.blocks["vrm:U1"].vrm_kind == "ldo"
    assert d.blocks["rail:+3V3_F"].lines == ["3.3 V"]          # the group's VRM voltage


def test_power_flows_left_to_right():
    d = build_diagram(_tree(), board())
    arrows = {(a.src, a.dst) for a in d.arrows}
    assert {("vrm:J1", "rail:VIN"), ("rail:VIN", "vrm:U1"), ("vrm:U1", "rail:+3V3"),
            ("rail:+3V3", "link:FB1"), ("link:FB1", "rail:+3V3_F"),
            ("rail:+3V3_F", "load:U3")} <= arrows
    col = {bid: b.column for bid, b in d.blocks.items()}
    for a in d.arrows:
        assert col[a.src] < col[a.dst], (a.src, a.dst)
    assert col["vrm:J1"] == 0
    assert [a.dashed for a in d.arrows if a.dst.startswith("cap:")] == [True]


def test_rows_are_unique_per_column():
    d = build_diagram(_tree(), board())
    for c in d.columns():
        assert [b.row for b in c] == list(range(len(c)))


def test_capacitors_need_the_board():
    s = _tree()
    d = build_diagram(s)
    assert not any(b.kind == "cap" for b in d.blocks.values())
    assert d.notes and "Load the board" in d.notes[0]
    s.caps[0].refs.append("C99")                         # not on this board
    assert any("C99" in n for n in build_diagram(s, board()).notes)


def test_empty_setup():
    from kipi.dc.pi.model import PISetup
    d = build_diagram(PISetup())
    assert d.blocks == {} and d.arrows == [] and d.columns() == []


def test_window_draws_and_saves(tmp_path, qapp_offscreen):
    from kipi.gui.power_tree import PowerTreeWindow
    w = PowerTreeWindow()
    w.show_tree(_tree(), board())
    assert len(w.diagram.blocks) == 9
    out = tmp_path / "tree.png"
    w.save_png(str(out))
    assert out.stat().st_size > 0
    w.close()


@pytest.fixture(scope="module")
def qapp_offscreen():
    import os
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# Placement (#58): grids for big sets of loads, each branch beside its feeder.

def _many_loads(n=47):
    s = _tree()
    for i in range(n):
        s.loads.append(Load(f"L{i}", PinRef(f"L{i}", "+3V3", ["1"]), 0.01))
    return s


def _size(b, compact):
    return (100.0, 30.0 if compact else 50.0)


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def test_many_loads_pack_into_a_grid_that_fits_the_window():
    from kipi.dc.pi.diagram import place
    d = build_diagram(_many_loads(), board())
    pl = place(d, _size, aspect=1.6)
    (cid, c), = pl.clusters.items()
    assert c.kind == "load" and c.rail == "rail:+3V3" and len(c.members) == 48   # U2 too
    assert 1 < c.cols < 48
    # Far shorter than one load per row, and close to the window's shape.
    assert pl.height < 48 * (30 + 8) / 2          # one per row would be ~1800
    assert 0.8 < pl.width / (pl.height * 1.6) < 1.25
    fx, fy, fw, fh = pl.cluster_rects[cid]
    for m in c.members:
        x, y, w, h = pl.rects[m]
        assert fx <= x and x + w <= fx + fw and fy <= y and y + h <= fy + fh


def test_placed_blocks_do_not_overlap():
    from kipi.dc.pi.diagram import place
    d = build_diagram(_many_loads(), board())
    pl = place(d, _size)
    boxes = [r for k, r in pl.rects.items() if k not in pl.cluster_of]
    boxes += list(pl.cluster_rects.values())
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not _overlap(a, b)
    # Each block sits right of the one feeding it.
    for a in d.arrows:
        src = pl.rects[a.src]
        dst = pl.cluster_rects.get(pl.cluster_of.get(a.dst), pl.rects[a.dst])
        assert src[0] + src[2] <= dst[0]


def test_few_loads_stay_single_blocks():
    from kipi.dc.pi.diagram import place
    pl = place(build_diagram(_tree(), board()), _size)
    assert pl.clusters == {}


def test_net_ties_between_grounds_are_left_out():
    s = _tree()
    s.links.append(DcLink("NT1", PinRef("NT1", "GND", ["1"]), PinRef("NT1", "AGND", ["2"]),
                          0.0))
    assert "link:NT1" not in build_diagram(s).blocks
