"""Build PyVista meshes from board geometry. This module has no Qt dependency.

Copper is drawn as slabs at the true stackup Z. Vias are tubes. The viewer
applies any Z exaggeration as a plotter scale, so the meshes here stay in
real millimeters.

KiCad's y axis points down. The meshes flip it (y -> -y) so the board reads
correctly when viewed from +Z.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
import shapely
from shapely.geometry.base import BaseGeometry

from kipi.core.geometry.copper import drill_holes, iter_polygons, poly_to_shapely
from kipi.core.model.board import BoardSnapshot, CopperLayer

# KiCad-like default layer colors, top to bottom (outer layers fixed).
_TOP = (0.78, 0.20, 0.20)
_BOTTOM = (0.30, 0.45, 0.85)
_INNER = [(0.85, 0.75, 0.25), (0.35, 0.75, 0.40), (0.85, 0.45, 0.80), (0.30, 0.75, 0.80),
          (0.95, 0.55, 0.25), (0.60, 0.50, 0.90)]


def layer_color(layer: CopperLayer, n_layers: int) -> Tuple[float, float, float]:
    if layer.index == 0:
        return _TOP
    if layer.index == n_layers - 1:
        return _BOTTOM
    return _INNER[(layer.index - 1) % len(_INNER)]


def triangulate(geom: BaseGeometry) -> Tuple[np.ndarray, np.ndarray]:
    """Constrained Delaunay triangulation of a (multi)polygon.

    Returns ``(points[N,2], triangles[M,3])`` with shared vertices merged.
    """
    pts_list, tri_list, offset = [], [], 0
    for poly in iter_polygons(geom):
        tris = shapely.get_parts(shapely.constrained_delaunay_triangles(poly))
        if len(tris) == 0:
            continue
        coords = shapely.get_coordinates(tris).reshape(-1, 4, 2)[:, :3, :].reshape(-1, 2)
        uniq, inverse = np.unique(np.round(coords, 7), axis=0, return_inverse=True)
        pts_list.append(uniq)
        tri_list.append(inverse.reshape(-1, 3) + offset)
        offset += len(uniq)
    if not pts_list:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    return np.vstack(pts_list), np.vstack(tri_list)


def sheet(geom: BaseGeometry, z: float) -> Optional[pv.PolyData]:
    pts2, tris = triangulate(geom)
    if len(tris) == 0:
        return None
    pts = np.column_stack([pts2[:, 0], -pts2[:, 1], np.full(len(pts2), z)])
    faces = np.column_stack([np.full(len(tris), 3), tris]).ravel()
    return pv.PolyData(pts, faces)


def slab_from_triangles(pts2: np.ndarray, tris: np.ndarray, z_bottom: float,
                        thickness: float) -> Tuple[pv.PolyData, np.ndarray]:
    """Closed slab from a 2D triangulation in KiCad coordinates (y is flipped here).

    Bottom and top faces reuse the triangles; side walls are quads on the boundary
    edges (edges used by exactly one triangle). Winding is made consistent so all
    normals point outward, which keeps shading and volume correct.

    Returns the mesh and, for each output face, the index of the source triangle, so
    per-triangle data can be carried onto the slab. Points are the 2D points twice
    (bottom copy then top copy), so per-point data maps with ``np.tile(data, 2)``.
    """
    # Mesh coordinates flip y, so make triangles counter-clockwise in (x, -y).
    xy = np.column_stack([pts2[:, 0], -pts2[:, 1]])
    a, b, c = xy[tris[:, 0]], xy[tris[:, 1]], xy[tris[:, 2]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
    tris = tris.copy()
    cw = cross < 0
    tris[cw] = tris[cw][:, ::-1]
    n = len(xy)
    m = len(tris)
    pts = np.vstack([np.column_stack([xy, np.full(n, z_bottom)]),
                     np.column_stack([xy, np.full(n, z_bottom + thickness)])])
    bottom = tris[:, ::-1]                       # faces down
    top = tris + n                               # faces up
    # Directed edges of CCW triangles; boundary edges have no reversed twin.
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    e_tri = np.tile(np.arange(m), 3)
    key = e[:, 0].astype(np.int64) * n + e[:, 1]
    rkey = e[:, 1].astype(np.int64) * n + e[:, 0]
    is_boundary = ~np.isin(key, rkey)
    boundary = e[is_boundary]
    wall_tri = e_tri[is_boundary]
    i, j = boundary[:, 0], boundary[:, 1]
    # Outward wall for a CCW boundary edge i->j: (i, j, j+n) and (i, j+n, i+n).
    walls = np.concatenate([np.column_stack([i, j, j + n]), np.column_stack([i, j + n, i + n])])
    allf = np.concatenate([bottom, top, walls])
    faces = np.column_stack([np.full(len(allf), 3), allf]).ravel()
    source = np.concatenate([np.arange(m), np.arange(m), wall_tri, wall_tri])
    return pv.PolyData(pts, faces), source


def slab(geom: BaseGeometry, z_bottom: float, thickness: float) -> Optional[pv.PolyData]:
    """Closed copper slab for a shapely (multi)polygon."""
    pts2, tris = triangulate(geom)
    if len(tris) == 0:
        return None
    if thickness <= 0:
        return sheet(geom, z_bottom)
    return slab_from_triangles(pts2, tris, z_bottom, thickness)[0]


@dataclass
class LayerMesh:
    net: str
    layer_id: int
    mesh: pv.PolyData
    color: Tuple[float, float, float]
    area_mm2: float


@dataclass
class NetScene:
    """Meshes for one net, so the viewer can add or remove nets independently."""
    net: str
    copper: List[LayerMesh] = field(default_factory=list)
    vias: Optional[pv.PolyData] = None
    pads: Dict[int, pv.PolyData] = field(default_factory=dict)   # layer id -> pad tops


@dataclass
class SceneData:
    """Board body plus any number of nets."""
    nets: Dict[str, NetScene] = field(default_factory=dict)
    board: Optional[pv.PolyData] = None
    outline_lines: Optional[pv.PolyData] = None


def via_tubes(snap: BoardSnapshot, nets: List[str], n_sides: int = 12) -> Optional[pv.PolyData]:
    """Via barrels of the given nets as one mesh.

    A unit cylinder is scaled and translated per via with numpy, which is much
    faster than building and merging one cylinder per via.
    """
    st = snap.stackup
    rows = []   # x, y, z_bottom, z_top, radius
    for v in (v for n in nets for v in snap.items_on_net(n)[1]):
        layers = [st.layer(l) for l in v.layers if st.layer(l) is not None]
        if not layers:
            continue
        z_top = max(l.z_top_mm for l in layers)
        z_bot = min(l.z_bottom_mm for l in layers)
        rows.append((v.position[0], -v.position[1], z_bot, max(z_top, z_bot + 1e-3),
                     max(v.drill_mm, 0.05) / 2.0))
    if not rows:
        return None
    a = np.asarray(rows)
    unit = pv.Cylinder(center=(0, 0, 0.5), direction=(0, 0, 1), radius=1.0, height=1.0,
                       resolution=n_sides, capping=True).triangulate()
    up = np.asarray(unit.points)                                   # (P, 3)
    uf = unit.faces.reshape(-1, 4)[:, 1:]                          # (F, 3) triangles
    n, P = len(a), len(up)
    pts = np.empty((n, P, 3))
    pts[:, :, 0] = a[:, None, 0] + up[None, :, 0] * a[:, None, 4]
    pts[:, :, 1] = a[:, None, 1] + up[None, :, 1] * a[:, None, 4]
    pts[:, :, 2] = a[:, None, 2] + up[None, :, 2] * (a[:, None, 3] - a[:, None, 2])
    tris = uf[None, :, :] + (np.arange(n) * P)[:, None, None]      # (n, F, 3)
    faces = np.column_stack([np.full(n * len(uf), 3), tris.reshape(-1, 3)]).ravel()
    return pv.PolyData(pts.reshape(-1, 3), faces)


def pad_tops(snap: BoardSnapshot, nets: List[str]) -> Dict[int, pv.PolyData]:
    """Pad shapes as sheets sitting just above their copper layer (for highlighting)."""
    st = snap.stackup
    out: Dict[int, pv.PolyData] = {}
    for cu in st.copper:
        polys = []
        for p in (p for n in nets for p in snap.items_on_net(n)[2]):
            for poly in p.shapes.get(cu.id, []):
                g = poly_to_shapely(poly)
                if g is not None and not g.is_empty:
                    polys.append(g)
        if not polys:
            continue
        geom = shapely.union_all(polys)
        holes = [h for h in (drill_holes(snap, n, cu.id) for n in nets) if h is not None]
        if holes:
            geom = geom.difference(shapely.union_all(holes))
        lift = 0.002
        z = cu.z_top_mm + lift if cu.index == 0 else (
            cu.z_bottom_mm - lift if cu.index == len(st.copper) - 1 else cu.z_top_mm + lift)
        m = sheet(geom, z)
        if m is not None:
            out[cu.id] = m
    return out


def board_body(snap: BoardSnapshot) -> Tuple[Optional[pv.PolyData], Optional[pv.PolyData]]:
    """Translucent laminate body and its outline edges."""
    if not snap.outline:
        return None, None
    geoms = [poly_to_shapely(p) for p in snap.outline]
    geom = shapely.union_all([g for g in geoms if g is not None])
    t = snap.stackup.board_thickness_mm
    body = slab(geom, 0.0, t)
    lines = []
    for poly in iter_polygons(geom):
        for ring in [poly.exterior, *poly.interiors]:
            xy = np.asarray(ring.coords)
            for z in (0.0, t):
                pts = np.column_stack([xy[:, 0], -xy[:, 1], np.full(len(xy), z)])
                lines.append(pv.lines_from_points(pts))
    return body, (pv.merge(lines) if lines else None)


def build_net_scene(snap: BoardSnapshot, net: str, layers: Dict[int, BaseGeometry],
                    progress=None) -> NetScene:
    """Build the meshes for one net from its per-layer copper geometry."""
    st = snap.stackup
    n = len(st.copper)
    ns = NetScene(net=net)
    for lid, geom in layers.items():
        cu = st.layer(lid)
        if cu is None:
            continue
        m = slab(geom, cu.z_bottom_mm, cu.thickness_mm)
        if m is None:
            continue
        ns.copper.append(LayerMesh(net=net, layer_id=lid, mesh=m, color=layer_color(cu, n),
                                   area_mm2=geom.area))
        if progress is not None:
            progress.debug(f"  mesh {net} / {cu.name}: {m.n_cells} cells")
            progress.check_cancel()
    ns.vias = via_tubes(snap, [net])
    ns.pads = pad_tops(snap, [net])
    return ns


def build_scene(snap: BoardSnapshot, copper: Dict[str, Dict[int, BaseGeometry]],
                progress=None) -> SceneData:
    """Build the board body and every net in ``copper`` (``net -> layer -> geometry``)."""
    scene = SceneData()
    for net, layers in copper.items():
        scene.nets[net] = build_net_scene(snap, net, layers, progress)
    scene.board, scene.outline_lines = board_body(snap)
    return scene
