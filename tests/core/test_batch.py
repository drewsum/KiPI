import numpy as np
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
    a, b3 = plane(2), plane(3, x=5)
    assert (m.regular_faces == np.vstack([a.regular_faces,
                                          b3.regular_faces + a.n_points])).all()
    assert np.allclose(m.points, np.vstack([a.points, b3.points]))


def test_chunk_can_filter_to_included_nets_without_dropping_stored_geometry():
    b = MeshBatcher()
    b.add("IN", ("cu", 3), plane(2), RED)
    b.add("OUT", ("cu", 3), plane(3, x=5), BLUE)
    chunk, = b.chunks(("cu", 3))

    all_copper = chunk.build()
    included_only = chunk.build({"IN"})

    assert all_copper.n_cells == 8 + 18
    assert included_only.n_cells == 8
    assert (included_only.cell_data["rgb"] == [255, 0, 0]).all()
    assert set(chunk.parts) == {"IN", "OUT"}

    assert chunk.build(set()) is None
    assert set(chunk.parts) == {"IN", "OUT"}


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



@pytest.mark.parametrize("mesh", [plane(3), pv.Plane(i_resolution=2, j_resolution=1),
                                  pv.Sphere().merge(pv.Line())],
                         ids=["triangles", "quads", "with lines"])
def test_meshes_are_read_as_pyvista_reads_them(mesh):
    # #205: triangles are read straight from VTK's arrays, the rest through pyvista.
    from kipi.core.viz.batch import _arrays
    tri = mesh if mesh.is_all_triangles else mesh.triangulate()
    pts, tris = _arrays(mesh)
    assert pts.dtype == np.float32 and tris.dtype == np.int64
    assert np.array_equal(pts, np.asarray(tri.points, dtype=np.float32))
    assert np.array_equal(tris, tri.faces.reshape(-1, 4)[:, 1:])
