"""Pack many small per-net meshes into a few large ones for fast rendering.

VTK's cost per frame is dominated by the number of actors (draw calls), not by
triangles: 3,500 actors holding 2.5 M triangles render at ~80 ms per frame, the
same triangles in a dozen actors at under 1 ms. The batcher groups meshes by
category (e.g. copper on one layer) into chunks of bounded size; each chunk
becomes one actor. Adding or removing a net rebuilds only the chunks it touches.

Per-net color survives merging as a per-cell ``rgb`` array.
"""

from __future__ import annotations

from typing import Dict, Hashable, Iterable, List, Optional, Set, Tuple

import numpy as np
import pyvista as pv
from vtkmodules.util.numpy_support import numpy_to_vtkIdTypeArray, vtk_to_numpy
from vtkmodules.vtkCommonDataModel import vtkCellArray

Category = Hashable       # e.g. ("cu", layer_id), ("pad", layer_id), ("via", -1)
RGB = Tuple[float, float, float]

DEFAULT_MAX_TRIANGLES = 250_000


class _Part:
    __slots__ = ("points", "tris", "color")

    def __init__(self, points: np.ndarray, tris: np.ndarray, color: RGB):
        self.points = points
        self.tris = tris
        self.color = color


def _arrays(mesh: pv.PolyData) -> Tuple[np.ndarray, np.ndarray]:
    """Points (float32) and triangles of ``mesh``. A mesh of triangles only, as the
    view's meshes are, is read straight from VTK's arrays: pyvista's ``is_all_triangles``
    and ``faces`` cost over a millisecond a mesh, most of a large board's merge time
    (#205)."""
    if not (mesh.GetNumberOfVerts() or mesh.GetNumberOfLines() or mesh.GetNumberOfStrips()):
        polys = mesh.GetPolys()
        if polys.IsHomogeneous() == 3:      # every cell a triangle
            conn = vtk_to_numpy(polys.GetConnectivityArray())
            data = mesh.GetPoints()
            pts = (np.zeros((0, 3), np.float32) if data is None
                   else vtk_to_numpy(data.GetData()).astype(np.float32))
            return pts, conn.reshape(-1, 3).astype(np.int64, copy=False)
    if not mesh.is_all_triangles:
        mesh = mesh.triangulate()
    pts = np.asarray(mesh.points, dtype=np.float32)
    tris = mesh.faces.reshape(-1, 4)[:, 1:].astype(np.int64, copy=False)
    return pts, tris


class Chunk:
    """One merged mesh: the parts of several nets in a single category."""

    def __init__(self, category: Category):
        self.category = category
        self.parts: Dict[str, _Part] = {}   # net -> part
        self.n_tris = 0
        self.mesh: Optional[pv.PolyData] = None

    def __repr__(self):
        return f"Chunk({self.category!r}, nets={len(self.parts)}, tris={self.n_tris})"

    @property
    def empty(self) -> bool:
        return not self.parts

    def build(self, visible_nets: Optional[Set[str]] = None) -> Optional[pv.PolyData]:
        """Concatenate visible net parts into one mesh with per-cell ``rgb`` data."""
        parts = [p for net, p in self.parts.items()
                 if visible_nets is None or net in visible_nets]
        if not parts:
            self.mesh = None
            return None
        offsets = np.cumsum([0] + [len(p.points) for p in parts[:-1]])
        pts = np.concatenate([p.points for p in parts])
        conn = np.concatenate([(p.tris + o).ravel() for p, o in zip(parts, offsets)])
        # VTK's cell arrays are given directly: pyvista's legacy ``faces`` (3, a, b, c, ..)
        # costs as much again to build and convert, in the UI thread (#209).
        cells = vtkCellArray()
        cells.SetData(numpy_to_vtkIdTypeArray(np.arange(0, len(conn) + 1, 3, dtype=np.int64),
                                              deep=True),
                      numpy_to_vtkIdTypeArray(conn.astype(np.int64, copy=False), deep=True))
        mesh = pv.PolyData()
        mesh.points = pts
        mesh.SetPolys(cells)
        colors = np.round(np.array([p.color for p in parts]) * 255).astype(np.uint8)
        mesh.cell_data["rgb"] = np.repeat(colors, [len(p.tris) for p in parts], axis=0)
        self.mesh = mesh
        return mesh


class MeshBatcher:
    """Track which net lives in which chunk and which chunks need rebuilding."""

    def __init__(self, max_triangles: int = DEFAULT_MAX_TRIANGLES):
        self.max_triangles = max_triangles
        self._chunks: Dict[Category, List[Chunk]] = {}
        self._net_chunks: Dict[str, Set[Chunk]] = {}
        self._dirty: Set[Chunk] = set()

    # ---------------------------------------------------------------- queries
    def nets(self) -> List[str]:
        return list(self._net_chunks)

    def chunks(self, category: Optional[Category] = None) -> List[Chunk]:
        if category is not None:
            return list(self._chunks.get(category, []))
        return [c for cs in self._chunks.values() for c in cs]

    def __contains__(self, net: str) -> bool:
        return net in self._net_chunks

    # ---------------------------------------------------------------- edits
    def add(self, net: str, category: Category, mesh: pv.PolyData, color: RGB) -> Chunk:
        """Add one net's mesh in ``category``, replacing any earlier one there."""
        pts, tris = _arrays(mesh)
        for c in self._net_chunks.get(net, ()):
            if c.category == category:
                self._drop_part(c, net)
                break
        chunk = self._open_chunk(category, len(tris))
        chunk.parts[net] = _Part(pts, tris, color)
        chunk.n_tris += len(tris)
        self._net_chunks.setdefault(net, set()).add(chunk)
        self._dirty.add(chunk)
        return chunk

    def remove(self, net: str):
        for c in list(self._net_chunks.pop(net, ())):
            self._drop_part(c, net, forget=False)

    def set_color(self, net: str, category: Category, color: RGB):
        for c in self._net_chunks.get(net, ()):
            if c.category == category and net in c.parts:
                part = c.parts[net]
                if part.color != color:
                    part.color = color
                    self._dirty.add(c)

    def clear(self):
        self._dirty.update(self.chunks())
        for c in self.chunks():
            c.parts.clear()
            c.n_tris = 0
        self._chunks.clear()
        self._net_chunks.clear()

    def take_dirty(self) -> List[Chunk]:
        """Chunks changed since the last call. Empty ones should lose their actor."""
        dirty, self._dirty = list(self._dirty), set()
        return dirty

    def mark_all_dirty(self):
        """Rebuild each chunk, for example after changing its visible net subset."""
        self._dirty.update(self.chunks())

    # ---------------------------------------------------------------- internals
    def _open_chunk(self, category: Category, n_tris: int) -> Chunk:
        chunks = self._chunks.setdefault(category, [])
        for c in reversed(chunks):
            if c.n_tris + n_tris <= self.max_triangles or c.empty:
                return c
        c = Chunk(category)
        chunks.append(c)
        return c

    def _drop_part(self, chunk: Chunk, net: str, forget: bool = True):
        part = chunk.parts.pop(net, None)
        if part is None:
            return
        chunk.n_tris -= len(part.tris)
        self._dirty.add(chunk)
        if forget:
            s = self._net_chunks.get(net)
            if s is not None:
                s.discard(chunk)
        if chunk.empty:
            chunks = self._chunks.get(chunk.category, [])
            if chunk in chunks:
                chunks.remove(chunk)


def iter_categories(chunks: Iterable[Chunk]):
    return sorted({c.category for c in chunks}, key=str)
