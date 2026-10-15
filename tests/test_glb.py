"""Reading the triangles of the .glb files kicad-cli writes for part models."""

import math

import numpy as np
import pytest

from glb_writer import TRI, glb, kicad_like, prim
from kipi.core.viz.glb import node_matrix, read_glb


def test_parts_are_keyed_by_their_named_node():
    parts = read_glb(kicad_like(["C1", "C2"]))
    assert set(parts) == {"C1", "C2"}
    # Each part's node moves its triangle along X; Y is the height above the board.
    assert parts["C2"].points[:, 0].min() == pytest.approx(0.001)
    assert parts["C1"].points[:, 1].min() == pytest.approx(0.0016)
    assert parts["C1"].tris.tolist() == [[0, 1, 2]]


def test_names_pick_the_part_nodes_among_named_ones():
    data = kicad_like(["C1"])
    assert set(read_glb(data, names=["C1", "C7"])) == {"C1"}
    assert set(read_glb(data, names=["R1"])) == {""}         # under no wanted node


def test_unnamed_parts_fall_under_the_inner_names_or_none():
    parts = read_glb(kicad_like(["C1", "C2"], named=False), names=["C1", "C2"])
    assert set(parts) == {""}
    assert len(parts[""].tris) == 2


def test_node_transforms_compose():
    q = [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)]        # 90 deg about Z
    nodes = [{"children": [1], "translation": [10, 0, 0], "name": "U1"},
             {"children": [2], "rotation": q, "scale": [2, 2, 2]},
             {"mesh": 0, "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 5, 1]}]
    part = read_glb(glb(nodes, [[prim(TRI)]]))["U1"]
    # (1, 0, 0) -> +5 in Z -> scaled by 2 and turned onto +Y -> moved 10 along X.
    assert part.points[1] == pytest.approx([10, 2, 10])
    assert node_matrix({})[:3, :3] == pytest.approx(np.eye(3))


def test_strips_and_fans_become_triangles():
    quad = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)]
    strip = read_glb(glb([{"mesh": 0, "name": "A"}], [[prim(quad, [0, 1, 2, 3], mode=5)]]))
    assert strip["A"].tris.tolist() == [[0, 1, 2], [2, 1, 3]]   # every other one flipped
    fan = read_glb(glb([{"mesh": 0, "name": "A"}], [[prim(quad, [0, 1, 3, 2], mode=6)]]))
    assert fan["A"].tris.tolist() == [[0, 1, 3], [0, 3, 2]]
    lines = read_glb(glb([{"mesh": 0, "name": "A"}], [[prim(quad, [0, 1], mode=1)]]))
    assert lines == {}


def test_colors_are_converted_from_linear_to_srgb():
    nodes = [{"mesh": 0, "name": "A"}]
    meshes = [[prim(TRI, material=0), prim(TRI, material=1), prim(TRI)]]
    part = read_glb(glb(nodes, meshes, [(0.0, 1.0, 0.2141, 1.0), (0.5, 0.5, 0.5, 1.0)]))["A"]
    assert part.rgb[0].tolist() == [0, 255, 128]       # linear 0.214 is sRGB 0.5
    assert part.rgb[1].tolist() == [188, 188, 188]
    assert part.rgb[2].tolist() == [204, 204, 204]     # no material: light gray
    assert len(part.points) == 9 and part.tris.max() == 8


def test_normals_follow_the_node_rotation_and_interleaved_data_reads():
    q = [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)]        # 90 deg about Z
    nodes = [{"mesh": 0, "name": "A", "rotation": q}]
    up = [(1, 0, 0)] * 3
    for interleave in (False, True):
        part = read_glb(glb(nodes, [[prim(TRI, normals=up, interleave=interleave)]]))["A"]
        assert part.normals == pytest.approx(np.tile([0, 1, 0], (3, 1)), abs=1e-6)
        assert part.points[1] == pytest.approx([0, 1, 0], abs=1e-6)
    # A part with a mesh lacking normals has none at all.
    mixed = read_glb(glb(nodes, [[prim(TRI, normals=up), prim(TRI)]]))["A"]
    assert mixed.normals is None


def test_out_of_range_indices_are_dropped():
    part = read_glb(glb([{"mesh": 0, "name": "A"}], [[prim(TRI, [0, 1, 2, 0, 1, 9])]]))["A"]
    assert part.tris.tolist() == [[0, 1, 2]]


def test_not_a_glb_file_is_refused():
    with pytest.raises(ValueError):
        read_glb(b"PK\x03\x04 not a glb")
