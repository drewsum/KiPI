"""3D meshes for solver results (no Qt).

Each copper layer becomes a slab carrying:
  point data  "V" (volts) and "drop_mV" (mV below the reference: the VRM target voltage for a
              power-tree rail; negative where the copper sits above it, a boost)
  cell data   "J" (A/mm^2) and "P" (mW/mm^2 of board area)
Plated holes become barrels with cell data "I_mA" (current through the barrel).

KiCad's y axis is flipped (y -> -y) as everywhere else in the viewer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pyvista as pv
from scipy.spatial import cKDTree

from kipi.core.model.board import BoardSnapshot, Stackup
from kipi.core.temperature import at as temperature_at
from kipi.core.progress import NULL, ProgressReporter
from kipi.core.viz.meshes import slab_from_triangles

# name -> (array, association, label, unit). Association "via" means the field lives
# on the via barrels rather than the copper layers.
FIELDS = {
    "voltage": ("V", "point", "Voltage", "V"),
    "current_density": ("J", "cell", "Current density", "A/mm²"),
    "power_density": ("P", "cell", "Power density", "mW/mm²"),
    "temperature": ("T", "point", "Temperature", "°C"),
    "via_current": ("I_mA", "via", "Via current", "mA"),
}


def scale_decimals(lo: float, hi: float) -> int:
    """Decimals that tell apart six values spread over ``lo``..``hi``: a 3.3 V rail that
    sags by 0.4 mV needs five, or every one reads "3.3"."""
    span = abs(hi - lo)
    if not span > 0 or not math.isfinite(span):
        return 4
    return min(9, max(0, int(np.ceil(-np.log10(span / 5))) + 1))


@dataclass
class LayerResult:
    layer_id: int
    mesh: pv.PolyData
    # For probing: the layer's 2D triangulation (KiCad coords) and per-triangle values.
    pts2: np.ndarray
    tris: np.ndarray
    node_v: np.ndarray
    tri_j: np.ndarray
    tri_p: np.ndarray
    centroid_tree: cKDTree = field(repr=False, default=None)
    # Current-flow arrows (display coordinates: x, -y, z): positions just above the
    # copper's top face, unit directions, |J| in A/mm^2 and the grid spacing they were
    # sampled on. They are also drawn just below the bottom face, at ``arrow_z_below``,
    # so they show from either side.
    arrow_pts: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    arrow_dir: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    arrow_mag: np.ndarray = field(default_factory=lambda: np.zeros(0))
    arrow_spacing: float = 1.0
    arrow_z_below: Optional[float] = None


@dataclass
class ResultScene:
    net: str
    v_ref: float
    layers: Dict[int, LayerResult]
    vias: Optional[pv.PolyData]
    ranges: Dict[str, Tuple[float, float]]   # field -> (min, max) over all layers
    via_range: Tuple[float, float] = (0.0, 0.0)
    # Plated holes for click selection: (N, 2, 3) top/bottom center points in KiCad
    # coordinates (x, y down, z up) and the owning via/pad id of each.
    via_segments: np.ndarray = field(default_factory=lambda: np.zeros((0, 2, 3)))
    via_ids: List[str] = field(default_factory=list)

    def probe(self, layer_id: int, x: float, y: float) -> Optional[Dict[str, float]]:
        """Values at (x, y) KiCad mm on a layer, or None if not on this net's copper."""
        lr = self.layers.get(layer_id)
        if lr is None or len(lr.tris) == 0:
            return None
        _, cand = lr.centroid_tree.query((x, y), k=min(12, len(lr.tris)))
        for t in np.atleast_1d(cand):
            p = lr.pts2[lr.tris[t]]
            w = _barycentric(p, x, y)
            if w is not None:
                v = float(w @ lr.node_v[lr.tris[t]])
                return {"V": v, "drop_mV": (self.v_ref - v) * 1e3,
                        "J": float(lr.tri_j[t]), "P": float(lr.tri_p[t])}
        return None


def _barycentric(p: np.ndarray, x: float, y: float, tol: float = 1e-9):
    (x1, y1), (x2, y2), (x3, y3) = p
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(det) < 1e-18:
        return None
    a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / det
    b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / det
    c = 1.0 - a - b
    if min(a, b, c) < -tol:
        return None
    return np.array([a, b, c])


ARROW_GRID = 45          # arrows across the longest side of a layer's copper
ARROW_MIN_FRACTION = 0.02   # skip bins carrying less than this fraction of the peak |J|
ARROW_LIFT = 0.002       # mm between the copper face and the arrows drawn on it
# A flat arrow's outline as fractions of its length, centered on its sample point.
ARROW_SHAFT_HALF_WIDTH = 0.07
ARROW_HEAD_HALF_WIDTH = 0.22
ARROW_HEAD_LENGTH = 0.4


def flat_arrows(pts: np.ndarray, dirs: np.ndarray, lengths: np.ndarray,
                z_levels) -> pv.PolyData:
    """Flat arrows in the xy plane, one per point and z level, as triangles.

    Each arrow is centered on its point (x, y), points along its unit direction (only x
    and y are used) and is ``lengths`` long: a shaft (two triangles) and a head (one).
    """
    n = len(pts)
    z_levels = list(z_levels)
    if n == 0 or not z_levels:
        return pv.PolyData()
    d = np.asarray(dirs, dtype=float)[:, :2]
    side = np.column_stack([-d[:, 1], d[:, 0]])          # d turned 90° to the left
    L = np.asarray(lengths, dtype=float)[:, None]
    c = np.asarray(pts, dtype=float)[:, :2]
    tail = c - 0.5 * L * d
    neck = c + (0.5 - ARROW_HEAD_LENGTH) * L * d
    tip = c + 0.5 * L * d
    sw, hw = ARROW_SHAFT_HALF_WIDTH * L, ARROW_HEAD_HALF_WIDTH * L
    outline = np.stack([tail - sw * side, neck - sw * side, neck + sw * side,
                        tail + sw * side, neck - hw * side, tip, neck + hw * side], axis=1)
    local = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6]])
    points, faces = [], []
    for k, z in enumerate(z_levels):
        xyz = np.concatenate([outline, np.full((n, 7, 1), float(z))], axis=2).reshape(-1, 3)
        points.append(xyz)
        tri = (local[None, :, :] + 7 * (np.arange(n)[:, None, None] + k * n)).reshape(-1, 3)
        faces.append(np.hstack([np.full((len(tri), 1), 3), tri]))
    return pv.PolyData(np.concatenate(points), np.concatenate(faces).ravel())


def current_arrows(pts2: np.ndarray, tris: np.ndarray, grad: np.ndarray, area: np.ndarray,
                   z: float, grid: int = ARROW_GRID):
    """Sample the sheet current on a grid: one arrow per occupied cell.

    Each arrow sits at the cell's area-weighted centroid and points along the
    area-weighted mean current direction (-grad V). Returns display-space positions,
    unit directions, |J| and the cell size.
    """
    if len(tris) == 0:
        return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0), 1.0
    c = pts2[tris].mean(axis=1)
    lo = c.min(axis=0)
    span = max(float((c.max(axis=0) - lo).max()), 1e-6)
    h = span / grid
    key = np.floor((c - lo) / h).astype(np.int64)
    key = key[:, 0] * (grid + 2) + key[:, 1]
    flow = -np.nan_to_num(grad)                   # current direction in KiCad axes
    uniq, inv = np.unique(key, return_inverse=True)
    w = area
    sw = np.bincount(inv, weights=w)
    cx = np.bincount(inv, weights=w * c[:, 0]) / sw
    cy = np.bincount(inv, weights=w * c[:, 1]) / sw
    fx = np.bincount(inv, weights=w * flow[:, 0]) / sw
    fy = np.bincount(inv, weights=w * flow[:, 1]) / sw
    mag = np.hypot(fx, fy)
    keep = mag > ARROW_MIN_FRACTION * (mag.max() if len(mag) else 0.0)
    if not np.any(keep):
        return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0), h
    cx, cy, fx, fy, mag = cx[keep], cy[keep], fx[keep], fy[keep], mag[keep]
    pts = np.column_stack([cx, -cy, np.full(len(cx), z)])
    dirs = np.column_stack([fx / mag, -fy / mag, np.zeros(len(cx))])
    return pts, dirs, mag, h


def _range(chunks: List[np.ndarray]) -> Tuple[float, float]:
    vals = np.concatenate([c[np.isfinite(c)] for c in chunks]) if chunks else np.zeros(0)
    if len(vals) == 0:
        return (0.0, 1.0)
    return float(vals.min()), float(vals.max())


def build_scenes(tree, board: Union[BoardSnapshot, Stackup],
                 progress: ProgressReporter = NULL) -> Dict[str, ResultScene]:
    """The 3D views of every group of a solved power tree, with the copper temperature of an
    electro-thermal solve."""
    temp = tree.thermal.sampler() if tree.thermal is not None else None
    scenes = {}
    for name, g in tree.groups.items():
        progress.check_cancel()
        progress.info(f"  {name}: {g.result.mesh.n_tris} triangles")
        scenes[name] = build_result_scene(board, g.result, temperature=temp)
    return scenes


def build_result_scene(board: Union[BoardSnapshot, Stackup], result, n_sides: int = 16,
                       temperature=None) -> ResultScene:
    """``board``: the board solved, or just its stackup (all that is needed, and all that a
    results file keeps). ``temperature`` (optional): callable KiCad xy (mm) -> degC from a
    thermal solve."""
    mesh, sol, fields = result.mesh, result.solution, result.fields
    st = board if isinstance(board, Stackup) else board.stackup
    v_ref = fields.v_ref
    layers: Dict[int, LayerResult] = {}
    for lid in mesh.layers:
        cu = st.layer(lid)
        node_sel = np.flatnonzero(mesh.node_layer == lid)
        tri_sel = np.flatnonzero(mesh.tri_layer == lid)
        if len(tri_sel) == 0:
            continue
        local = np.full(mesh.n_nodes, -1, dtype=np.int64)
        local[node_sel] = np.arange(len(node_sel))
        tris = local[mesh.tris[tri_sel]]
        pts2 = mesh.points[node_sel]
        v = sol.node_v[node_sel]
        tri_j = fields.j_density[tri_sel]
        tri_p = fields.power_density[tri_sel] * 1e3
        live_tri = np.all(np.isfinite(v[tris]), axis=1)
        tri_j = np.where(live_tri, tri_j, np.nan)
        tri_p = np.where(live_tri, tri_p, np.nan)
        slab, source = slab_from_triangles(pts2, tris, cu.z_bottom_mm, cu.thickness_mm)
        slab.point_data["V"] = np.tile(v, 2)
        slab.point_data["drop_mV"] = np.tile((v_ref - v) * 1e3, 2)
        temp = (np.broadcast_to(temperature_at(temperature, pts2, lid), (len(pts2),))
                if temperature is not None else np.full(len(pts2), np.nan))
        slab.point_data["T"] = np.tile(temp, 2)
        slab.cell_data["J"] = tri_j[source]
        slab.cell_data["P"] = tri_p[source]
        lr = LayerResult(lid, slab, pts2, tris, v, tri_j, tri_p, cKDTree(pts2[tris].mean(axis=1)))
        rho = cu.thickness_mm / sol.sheet_g[lid]          # this layer's resistivity
        apts, adir, amag, h = current_arrows(pts2, tris, fields.grad[tri_sel],
                                             fields.tri_area[tri_sel],
                                             cu.z_top_mm + ARROW_LIFT)
        lr.arrow_pts, lr.arrow_dir, lr.arrow_mag, lr.arrow_spacing = apts, adir, amag / rho, h
        lr.arrow_z_below = cu.z_bottom_mm - ARROW_LIFT
        layers[lid] = lr

    ranges = {}
    for key, (arr, assoc, _, _) in FIELDS.items():
        if assoc == "via":
            continue
        data = [(lr.mesh.point_data if assoc == "point" else lr.mesh.cell_data)[arr]
                for lr in layers.values()]
        ranges[key] = _range(data)

    vias, via_range, via_segments, via_ids = _via_barrels(st, result, n_sides)
    ranges["via_current"] = via_range
    return ResultScene(net=mesh.net, v_ref=v_ref, layers=layers, vias=vias, ranges=ranges,
                       via_range=via_range, via_segments=via_segments, via_ids=via_ids)


def _via_barrels(st: Stackup, result, n_sides: int):
    fields = result.fields
    blocks = []
    currents = []
    segments, ids = [], []
    for bar, vr in zip(result.mesh.barrels, fields.vias):
        layers = [st.layer(l) for l in bar.layers]
        if not layers:
            continue
        z_top = max(l.z_top_mm for l in layers)
        z_bot = min(l.z_bottom_mm for l in layers)
        if z_top - z_bot < 1e-6:
            continue
        x, y = bar.position
        cyl = pv.Cylinder(center=(x, -y, 0.5 * (z_top + z_bot)), direction=(0, 0, 1),
                          radius=max(bar.drill_mm, 0.05) / 2, height=z_top - z_bot,
                          resolution=n_sides, capping=True)
        cyl.cell_data["I_mA"] = np.full(cyl.n_cells, vr.current * 1e3)
        blocks.append(cyl)
        currents.append(vr.current * 1e3)
        segments.append([(x, y, z_top), (x, y, z_bot)])
        ids.append(bar.owner_id)
    if not blocks:
        return None, (0.0, 0.0), np.zeros((0, 2, 3)), []
    return pv.merge(blocks), (0.0, max(currents)), np.asarray(segments, dtype=float), ids
