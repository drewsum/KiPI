"""Build PyVista meshes from board geometry. This module has no Qt dependency.

Copper is drawn as slabs at the true stackup Z. Vias are tubes. The viewer
applies any Z exaggeration as a plotter scale, so the meshes here stay in
real millimeters.

KiCad's y axis points down. The meshes flip it (y -> -y) so the board reads
correctly when viewed from +Z.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
import shapely
from shapely.geometry.base import BaseGeometry

from kipi.core.geometry.copper import drill_holes, iter_polygons, poly_to_shapely
from kipi.core.model.board import BoardSnapshot, CopperLayer
from kipi.core.sorting import edge_keys, single_mask
from kipi.core.viz.layer_colors import DEFAULT_COPPER, coating_rgb, layer_key

# Copper layer colors, keyed like a KiCad color theme ("f", "in1".., "b"): KiCad's default
# theme until :func:`set_layer_colors` gives the PCB editor's.
_layer_colors: Dict[str, Tuple[int, int, int]] = dict(DEFAULT_COPPER)


def set_layer_colors(colors: Dict[str, Tuple[int, int, int]]):
    """Use these copper colors (0..255 RGB by theme key) for nets built from now on."""
    global _layer_colors
    _layer_colors = {**DEFAULT_COPPER, **colors}


def layer_color(layer: CopperLayer, n_layers: int) -> Tuple[float, float, float]:
    rgb = _layer_colors.get(layer_key(layer.id, layer.index, n_layers), (184, 115, 51))
    return tuple(v / 255.0 for v in rgb)


try:
    import triangle as _triangle
except ImportError:                     # GEOS triangulates alone, only more slowly
    _triangle = None

# A vertex this close to a segment it is not an end of could crash Triangle (see
# kipi.core.mesh.mesher.pslg_problems); such a polygon is left to GEOS.
_NEAR_MM = 1e-7


def _outline(geom) -> Tuple[np.ndarray, np.ndarray]:
    """The rings of a (multi)polygon as unique vertices and the segments between them."""
    rings = shapely.get_rings(shapely.get_parts(geom))
    coords, ring_of = shapely.get_coordinates(rings, return_index=True)
    # Each ring's last point closes it: segments run from every point but the last.
    last = np.r_[ring_of[1:] != ring_of[:-1], True]
    first = np.r_[True, last[:-1]]
    idx = np.arange(len(coords))
    segs = np.column_stack([idx[~last], idx[~first]])
    # As complex numbers the points sort in one dimension, much faster than by rows.
    uniq, inverse = np.unique(coords[:, 0] + 1j * coords[:, 1], return_inverse=True)
    segs = inverse.ravel()[segs]
    return np.column_stack([uniq.real, uniq.imag]), segs[segs[:, 0] != segs[:, 1]]


def _safe_for_triangle(geom, verts: np.ndarray, segs: np.ndarray) -> bool:
    """Whether the rings neither cross nor touch, which Triangle meets only through its
    exact arithmetic. Rings that touch are safe if they meet only at shared vertices and
    no vertex lies on or almost on a segment it is not an end of."""
    if shapely.is_simple(shapely.multilinestrings(shapely.get_rings(shapely.get_parts(geom)))):
        return True
    if not shapely.is_valid(geom):
        return False
    tree = shapely.STRtree(shapely.linestrings(verts[segs]))
    k, j = tree.query(shapely.points(verts), predicate="dwithin", distance=_NEAR_MM)
    return bool((segs[j] == k[:, None]).any(axis=1).all())


def _triangle_fast(poly) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """The triangles of a (multi)polygon from Triangle (#202), with no points added:
    fifteen times faster than GEOS on a plane with thousands of antipads. None when
    Triangle is not installed, or not safe or not right for this geometry."""
    if _triangle is None:
        return None
    try:
        verts, segs = _outline(poly)
        if len(segs) < 3 or not _safe_for_triangle(poly, verts, segs):
            return None
        out = _triangle.triangulate({"vertices": verts, "segments": segs}, "pQ")
        pts = np.asarray(out["vertices"], dtype=float)
        tris = np.asarray(out.get("triangles", np.zeros((0, 3))), dtype=np.int64)
        if len(pts) != len(verts) or len(tris) == 0:
            return None
        # Holes are filled in: keep the triangles inside the polygon.
        centers = pts[tris].mean(axis=1)
        tris = tris[shapely.contains_xy(poly, centers[:, 0], centers[:, 1])]
        a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
        area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                            - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])).sum()
        if abs(area - poly.area) > 1e-6 * max(poly.area, 1e-6):
            return None
        return pts, tris
    except Exception:
        return None


def _geos(poly) -> Tuple[np.ndarray, np.ndarray]:
    """A polygon's constrained Delaunay triangles from GEOS."""
    tris = shapely.get_parts(shapely.constrained_delaunay_triangles(poly))
    if len(tris) == 0:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    coords = shapely.get_coordinates(tris).reshape(-1, 4, 2)[:, :3, :].reshape(-1, 2)
    uniq, inverse = np.unique(np.round(coords, 7), axis=0, return_inverse=True)
    return uniq, inverse.reshape(-1, 3)


def triangulate(geom: BaseGeometry) -> Tuple[np.ndarray, np.ndarray]:
    """Triangulation of a (multi)polygon, by Triangle where it is safe, else GEOS.

    Returns ``(points[N,2], triangles[M,3])`` with shared vertices merged.
    """
    whole = _triangle_fast(geom) if not geom.is_empty else None
    if whole is not None:
        return whole
    pts_list, tri_list, offset = [], [], 0
    for poly in iter_polygons(geom):
        pts, tris = _triangle_fast(poly) or _geos(poly)
        if len(tris) == 0:
            continue
        pts_list.append(pts)
        tri_list.append(tris + offset)
        offset += len(pts)
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
    # Directed edges of CCW triangles; boundary edges have no reversed twin, so they are
    # the edges that only one triangle has.
    e, key = edge_keys(tris, n)
    e_tri = np.tile(np.arange(m), 3)
    is_boundary = single_mask(key)
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


@functools.lru_cache(maxsize=None)
def _unit_cylinder(n_sides: int) -> Tuple[np.ndarray, np.ndarray]:
    """A capped cylinder of radius and height 1 standing on the origin: its points
    ``(P, 3)`` and triangles ``(F, 3)``, made once for every net's vias."""
    unit = pv.Cylinder(center=(0, 0, 0.5), direction=(0, 0, 1), radius=1.0, height=1.0,
                       resolution=n_sides, capping=True).triangulate()
    up = np.array(unit.points)
    uf = np.array(unit.faces.reshape(-1, 4)[:, 1:])
    up.flags.writeable = uf.flags.writeable = False
    return up, uf


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
    up, uf = _unit_cylinder(n_sides)
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


BODY_GAP_FRACTION = 0.01    # body inset from the outer copper, as a share of its span


def _body_span(stackup) -> Tuple[float, float]:
    """``(z_bottom, thickness)`` of the laminate body.

    The body sits inside the outer copper with a small gap: a face coplanar with copper
    z-fights with it (speckle that shifts as the view rotates). Falls back to the full
    board thickness when the stackup leaves no room between the outer copper.
    """
    t = stackup.board_thickness_mm
    tol = 1e-6
    lo = max([c.z_top_mm for c in stackup.copper if c.z_bottom_mm <= tol], default=0.0)
    hi = min([c.z_bottom_mm for c in stackup.copper if c.z_top_mm >= t - tol], default=t)
    if hi - lo <= 0:
        return 0.0, t
    gap = BODY_GAP_FRACTION * (hi - lo)
    return lo + gap, hi - lo - 2 * gap


def board_body(snap: BoardSnapshot) -> Tuple[Optional[pv.PolyData], Optional[pv.PolyData]]:
    """Translucent laminate body and its outline edges."""
    if not snap.outline:
        return None, None
    geoms = [poly_to_shapely(p) for p in snap.outline]
    geom = shapely.union_all([g for g in geoms if g is not None])
    t = snap.stackup.board_thickness_mm
    body = slab(geom, *_body_span(snap.stackup))
    lines = []
    for poly in iter_polygons(geom):
        for ring in [poly.exterior, *poly.interiors]:
            xy = np.asarray(ring.coords)
            for z in (0.0, t):
                pts = np.column_stack([xy[:, 0], -xy[:, 1], np.full(len(xy), z)])
                lines.append(pv.lines_from_points(pts))
    return body, (pv.merge(lines) if lines else None)


COATING_GAP_MM = 0.002     # coatings off the copper, so their faces do not z-fight


def board_coatings(snap: BoardSnapshot, sides=("top", "bottom")
                   ) -> List[Tuple[str, pv.PolyData, Tuple[float, ...]]]:
    """The solder masks and silkscreens as slabs over the board (#82): ``(kind, mesh,
    color)`` for each, on ``sides``. A mask is open over the pads of the copper under it;
    a silkscreen is what it prints (see
    :func:`~kipi.core.geometry.silkscreen.printed_silk`)."""
    from kipi.core.geometry.silkscreen import mask_openings, printed_silk
    st = snap.stackup
    if not snap.outline or not st.coatings or not st.copper:
        return []
    board = shapely.union_all([g for g in (poly_to_shapely(p) for p in snap.outline)
                               if g is not None])
    out = []
    for side in sides:
        cu = st.copper[0] if side == "top" else st.copper[-1]
        z = cu.z_top_mm if side == "top" else cu.z_bottom_mm
        openings = mask_openings(snap, side)
        z += COATING_GAP_MM if side == "top" else -COATING_GAP_MM
        for c in st.coatings_on(side):
            if c.kind == "mask":
                geom = board if openings is None else board.difference(openings)
            else:
                geom = printed_silk(snap, side, board)
            t = c.thickness_mm
            m = slab(geom, z if side == "top" else z - t, t) if geom is not None else None
            if m is not None:
                out.append((c.kind, m, coating_rgb(c)))
            z += t if side == "top" else -t
    return out


def build_net_scene(snap: BoardSnapshot, net: str, layers: Dict[int, BaseGeometry],
                    progress=None, holes: bool = True) -> NetScene:
    """Build the meshes for one net from its per-layer copper geometry; with ``holes``
    False, only its copper (as for a part of a net built in pieces)."""
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
    if holes:
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
