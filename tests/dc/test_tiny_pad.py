"""A terminal pad smaller than the mesh still holds a node."""

from shapely.geometry import box

from kipi.dc.pi.model import PinRef
from kipi.dc.pi.tree import solve_tree
from tree_boards import B, board, by_name, pad, setup


def test_a_pad_smaller_than_the_mesh_takes_the_nearest_node():
    snap = board()
    snap.pads.append(pad("u2_3", "GND", "U2", "3", box(21.49, 0.49, 21.515, 0.515), B))
    s = setup(ground=True)
    s.loads[0].ground = PinRef("U2", "GND", ["3"])
    s.settings.max_edge_mm = 1.5
    res = solve_tree(snap, s)
    assert by_name(res.loads, "U2").passed is True
