"""Slotted PTH holes turn with their pad: the drill size is given in the pad's own frame."""

import math

import pytest
from shapely.affinity import rotate
from shapely.geometry import Point, box

from kipi.core.geometry.copper import drill_holes, iter_polygons, net_layer_copper, slot_axis
from kipi.core.kicad_io.snapshot import from_dict, to_dict
from kipi.core.model.board import Pad
from synthetic import F, make_board
from test_tree import poly


def _slot_pad(angle_deg, pad_geom=None, drill=(2.0, 0.8)):
    """A +3V3 PTH pad at (5, 5) whose 2.0 x 0.8 mm slot runs along its own x axis."""
    if pad_geom is None:
        # 2.9 x 1.7 mm pad, long along the pad's x, turned like KiCad turns it (counter-
        # clockwise on screen, which with y down is clockwise in these coordinates).
        pad_geom = rotate(box(3.55, 4.15, 6.45, 5.85), -(angle_deg or 0.0), origin=(5, 5))
    return Pad("s1", "+3V3", "J9", "1", (5.0, 5.0), "pth", drill_mm=drill,
               shapes={F: [poly(pad_geom)]}, angle_deg=angle_deg)


def _board_with(pad):
    snap = make_board()
    snap.pads.append(pad)
    snap.invalidate_index()
    return snap


def _extent(holes):
    """Size of the hole at the pad (the board's own via is elsewhere)."""
    x0, y0, x1, y1 = holes.intersection(box(3, 3, 7, 7)).bounds
    return x1 - x0, y1 - y0


@pytest.mark.parametrize("angle, size", [(0.0, (2.0, 0.8)), (90.0, (0.8, 2.0)),
                                         (180.0, (2.0, 0.8)), (-90.0, (0.8, 2.0))])
def test_slot_follows_the_pad_angle(angle, size):
    hole = drill_holes(_board_with(_slot_pad(angle)), "+3V3", F, quad_segs=16)
    assert _extent(hole) == pytest.approx(size, abs=0.01)


def test_slot_axis_at_an_oblique_angle():
    # KiCad's +30 degrees points the pad's x axis up and to the right on screen: +x, -y.
    ux, uy = slot_axis(_slot_pad(30.0))
    assert (ux, uy) == pytest.approx((math.cos(math.radians(30)), -math.sin(math.radians(30))))
    # A slot long along the pad's y turns the same way.
    ux, uy = slot_axis(_slot_pad(30.0, drill=(0.8, 2.0)))
    assert (ux, uy) == pytest.approx((math.sin(math.radians(30)), math.cos(math.radians(30))))


def test_a_turned_slot_stays_inside_its_pad():
    # The footprint is turned 90 degrees: the pad is 1.7 wide and 2.9 tall. A slot cut
    # along x (the old behavior) would be 2.0 wide and sever the pad.
    copper = net_layer_copper(_board_with(_slot_pad(90.0)), "+3V3", F)
    ring = [g for g in iter_polygons(copper) if g.intersects(box(4, 4, 6, 6))]
    assert len(ring) == 1 and len(ring[0].interiors) == 1


def test_old_snapshots_take_the_slot_axis_from_the_pad_shape():
    # No angle recorded: the pad is long along y, so the slot is too.
    pad = _slot_pad(None, pad_geom=box(4.15, 3.55, 5.85, 6.45))
    assert _extent(drill_holes(_board_with(pad), "+3V3", F, quad_segs=16)) == \
        pytest.approx((0.8, 2.0), abs=0.01)
    # A round pad gives no hint: the drill's own axes are used.
    pad = _slot_pad(None, pad_geom=Point(5, 5).buffer(1.6))
    assert slot_axis(pad) == (1.0, 0.0)


def test_snapshots_keep_the_pad_angle():
    snap = _board_with(_slot_pad(90.0))
    back = from_dict(to_dict(snap))
    assert next(p for p in back.pads if p.id == "s1").angle_deg == 90.0
    d = to_dict(snap)
    for p in d["pads"]:
        del p["angle_deg"]                       # a snapshot from before angles
    assert all(p.angle_deg is None for p in from_dict(d).pads)
