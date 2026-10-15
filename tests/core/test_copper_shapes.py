import math

import pytest

pytest.importorskip("kipy")

from kipy.board_types import BoardCircle, BoardRectangle, BoardSegment, Net  # noqa: E402
from kipy.geometry import Vector2  # noqa: E402
from kipy.proto.board.board_types_pb2 import BoardLayer  # noqa: E402

from kipi.core.kicad_io.copper_shapes import copper_shape_zones, shape_geometry  # noqa: E402
from kipi.core.progress import NULL  # noqa: E402

NM = 1_000_000


def _segment(net="", width_mm=1.0, layer=BoardLayer.BL_F_Cu):
    s = BoardSegment()
    s.start = Vector2.from_xy(0, 0)
    s.end = Vector2.from_xy(10 * NM, 0)
    s.attributes.stroke.width = int(width_mm * NM)
    s.layer = layer
    if net:
        s.net = Net(name=net)
    return s


def test_segment_is_a_stroke_with_round_ends():
    g = shape_geometry(_segment())
    assert g.area == pytest.approx(10.0 + math.pi * 0.25, rel=1e-2)


def test_filled_rectangle_without_stroke_is_its_area():
    r = BoardRectangle()
    r.top_left = Vector2.from_xy(0, 0)
    r.bottom_right = Vector2.from_xy(4 * NM, 2 * NM)
    r.attributes.stroke.width = 0
    r.attributes.fill.filled = True
    assert shape_geometry(r).area == pytest.approx(8.0)


def test_unfilled_circle_is_a_ring():
    c = BoardCircle()
    c.center = Vector2.from_xy(0, 0)
    c.radius_point = Vector2.from_xy(5 * NM, 0)
    c.attributes.stroke.width = 1 * NM
    c.attributes.fill.filled = False
    ring = math.pi * (5.5 ** 2 - 4.5 ** 2)
    assert shape_geometry(c).area == pytest.approx(ring, rel=1e-2)


def test_zones_only_for_copper_shapes_with_a_net():
    shapes = [_segment(net="+3V3"), _segment(net=""),
              _segment(net="+3V3", layer=BoardLayer.BL_F_SilkS)]
    zones = copper_shape_zones(shapes, [BoardLayer.BL_F_Cu], NULL)
    assert [z.net for z in zones] == ["+3V3"]
    assert list(zones[0].filled) == [BoardLayer.BL_F_Cu]
