import pytest

pv = pytest.importorskip("pyvista")

from kipi.core.viz.batch import MeshBatcher

RED, BLUE = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)


def plane(n=2, x=0.0):
    """A small triangulated square with 2*n*n triangles."""
    return pv.Plane(center=(x, 0, 0), i_resolution=n, j_resolution=n).triangulate()


def test_merges_nets_into_one_chunk_per_category():
    b = MeshBatcher()
    b.add("A", ("cu", 3), plane(2), RED)
    b.add("B", ("cu", 3), plane(3, x=5), BLUE)
    b.add("A", ("cu", 4), plane(2), RED)
    dirty = b.take_dirty()
    assert len(dirty) == 2 and len(b.chunks(("cu", 3))) == 1
    chunk = b.chunks(("cu", 3))[0]
    m = chunk.build()
    assert m.n_cells == 8 + 18
    rgb = m.cell_data["rgb"]
    assert (rgb[:8] == [255, 0, 0]).all() and (rgb[8:] == [0, 0, 255]).all()
    assert m.n_points == plane(2).n_points + plane(3).n_points
    # Triangle indices were offset into the merged point array.
    assert m.faces.reshape(-1, 4)[:, 1:].max() == m.n_points - 1


def test_chunks_split_at_triangle_budget():
    b = MeshBatcher(max_triangles=20)
    for i in range(5):
        b.add(f"N{i}", ("cu", 3), plane(2), RED)   # 8 triangles each
    sizes = [c.n_tris for c in b.chunks(("cu", 3))]
    assert sizes == [16, 16, 8]


def test_remove_rebuilds_only_touched_chunks():
    b = MeshBatcher(max_triangles=20)
    for i in range(4):
        b.add(f"N{i}", ("cu", 3), plane(2), RED)
    b.add("N0", ("via", -1), plane(1), BLUE)
    b.take_dirty()
    b.remove("N0")
    dirty = b.take_dirty()
    assert {c.category for c in dirty} == {("cu", 3), ("via", -1)}
    assert len(dirty) == 2                     # not the second copper chunk
    via_chunk = next(c for c in dirty if c.category == ("via", -1))
    assert via_chunk.empty and b.chunks(("via", -1)) == []
    assert "N0" not in b and sorted(b.nets()) == ["N1", "N2", "N3"]


def test_re_adding_replaces_and_recolor_marks_dirty():
    b = MeshBatcher()
    b.add("A", ("cu", 3), plane(2), RED)
    b.add("A", ("cu", 3), plane(3), RED)
    assert b.chunks(("cu", 3))[0].n_tris == 18
    b.take_dirty()
    b.set_color("A", ("cu", 3), BLUE)
    (chunk,) = b.take_dirty()
    assert (chunk.build().cell_data["rgb"] == [0, 0, 255]).all()
    b.set_color("A", ("cu", 3), BLUE)
    assert b.take_dirty() == []
