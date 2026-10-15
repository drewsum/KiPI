"""Conforming triangle mesh of one net's copper on every layer.

Each connected copper polygon is meshed with Shewchuk's Triangle (quality + max
area). Terminal pad outlines are inserted as constraint segments so every pad has
its own nodes, and drill holes (vias, PTH pads) are holes whose boundary nodes
form the ring that the barrel connects to.

Coordinates stay in KiCad board space (mm, y down).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from shapely import STRtree
import triangle
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry

from kipi.geometry.copper import drill_holes, iter_polygons, net_layer_copper, poly_to_shapely
from kipi.model.board import BoardSnapshot, Pad
from kipi.progress import NULL, ProgressReporter

RING_EPS_MM = 1e-3      # tolerance when collecting nodes on a drill ring / inside a pad
# Steps big enough to announce before they start (they take seconds), not only after.
ANNOUNCE_LAYER_MM2 = 200.0   # copper area of one layer of one net
ANNOUNCE_HOLES = 50          # plated holes to join across layers


class MeshError(RuntimeError):
    pass


@dataclass
class Barrel:
    """A plated hole (via or PTH pad) joining copper on several layers."""
    owner_id: str             # via id or pad id
    kind: str                 # "via" or "pth"
    position: Tuple[float, float]
    drill_mm: float
    ring_nodes: Dict[int, np.ndarray] = field(default_factory=dict)   # layer -> node ids
    hole_type: str = "through"           # via type, or "pth" for a component hole
    filled: Optional[bool] = None        # KiCad's drill filling; None when not stated

    @property
    def layers(self) -> List[int]:
        return list(self.ring_nodes)


@dataclass
class NetMesh:
    net: str
    points: np.ndarray                 # (N, 2) mm, KiCad coordinates
    node_layer: np.ndarray             # (N,) copper layer id per node
    tris: np.ndarray                   # (M, 3) node ids
    tri_layer: np.ndarray              # (M,) copper layer id per triangle
    layers: List[int]                  # layer ids in stack order (top first)
    barrels: List[Barrel]
    pad_nodes: Dict[str, Dict[int, np.ndarray]]   # pad id -> layer -> node ids

    @property
    def n_nodes(self) -> int:
        return len(self.points)

    @property
    def n_tris(self) -> int:
        return len(self.tris)


# ---------------------------------------------------------------------------
# One polygon
# ---------------------------------------------------------------------------

# KiCad works in integer nanometers, so noding on a 1 nm grid loses nothing.
NODING_GRID_MM = 1e-6


def _linework(lines: Sequence[BaseGeometry]) -> Tuple[np.ndarray, np.ndarray]:
    """Node the lines against each other and return (vertices, segments).

    Noding uses GEOS snap-rounding on a fixed grid, so every intersection becomes a
    shared vertex exactly. (Noding in floating point and rounding afterwards could
    nudge a vertex just off the segment it splits; Triangle then crashes.)
    """
    parts_in = [l for l in lines if l is not None and not l.is_empty]
    if not parts_in:
        return np.zeros((0, 2)), np.zeros((0, 2), dtype=np.int64)
    noded = shapely.union_all(parts_in, grid_size=NODING_GRID_MM)
    parts = [g for g in shapely.get_parts(noded) if g.geom_type in ("LineString", "LinearRing")]
    seg_list = []
    for g in parts:
        c = np.asarray(g.coords)
        if len(c) >= 2:
            seg_list.append(np.stack([c[:-1], c[1:]], axis=1))
    if not seg_list:
        return np.zeros((0, 2)), np.zeros((0, 2), dtype=np.int64)
    segs = np.concatenate(seg_list)                         # (S, 2, 2)
    # Coordinates are already on the grid; rounding only removes float noise.
    verts, inv = np.unique(np.round(segs.reshape(-1, 2) / NODING_GRID_MM) * NODING_GRID_MM,
                           axis=0, return_inverse=True)
    idx = inv.reshape(-1, 2)
    idx = idx[idx[:, 0] != idx[:, 1]]
    idx = np.unique(np.sort(idx, axis=1), axis=0)
    return verts, idx


def pslg_problems(verts: np.ndarray, segs: np.ndarray, tol: float = NODING_GRID_MM / 10) -> int:
    """Count defects that can crash Triangle (a segfault takes the whole app down):
    segments that touch or cross without sharing a vertex, and vertices practically on
    a segment they do not belong to. Snap-rounding may leave a vertex a fraction of a
    grid step from a segment; Triangle's exact arithmetic handles that, so only
    distances well below the grid count."""
    from shapely.geometry import LineString as _LS, Point as _P
    from shapely.strtree import STRtree

    lines = [_LS(verts[a]) for a in segs]
    tree = STRtree(lines)
    bad = 0
    pairs = tree.query(lines, predicate="intersects")
    for i, j in zip(*pairs):
        if i < j and not (set(segs[i]) & set(segs[j])):
            bad += 1
    pts = [_P(v) for v in verts]
    near = tree.query(pts, predicate="dwithin", distance=tol)
    for k, j in zip(*near):
        if k not in segs[j]:
            bad += 1
    return bad


# Constraint lines (pad outlines) closer than this to the copper edge are dropped: the
# copper edge already bounds the pad there, and two nearly coincident lines leave hair-
# thin slivers that Triangle's quality refinement can split essentially forever.
CONSTRAINT_CLEARANCE_MM = 0.01


def _clip_constraint(line: BaseGeometry, poly: Polygon) -> Optional[BaseGeometry]:
    """The part of ``line`` inside ``poly``, kept clear of the polygon's edges.

    Pieces within CONSTRAINT_CLEARANCE_MM of an edge are removed (they would run almost
    on top of it), and each remaining end that stops near an edge is extended straight
    to that edge, so a pad outline still meets the copper boundary exactly but always
    crosses it rather than running alongside it.
    """
    from shapely.geometry import LineString as _LS
    from shapely.ops import nearest_points

    tol = CONSTRAINT_CLEARANCE_MM
    edge = poly.boundary
    inner = shapely.intersection(line, poly)
    inner = shapely.difference(inner, edge.buffer(tol, quad_segs=2))
    parts = []
    for part in shapely.get_parts(inner):
        if part.geom_type not in ("LineString", "LinearRing") or part.length < tol:
            continue
        coords = list(part.coords)
        for end in (0, -1):
            pt = shapely.Point(coords[end])
            if edge.distance(pt) < 2.5 * tol:
                on_edge = nearest_points(edge, pt)[0]
                if end == 0:
                    coords.insert(0, (on_edge.x, on_edge.y))
                else:
                    coords.append((on_edge.x, on_edge.y))
        parts.append(_LS(coords))
    return shapely.union_all(parts) if parts else None


class MeshCapHit(RuntimeError):
    """Triangle hit its Steiner-point cap: the input geometry is pathological."""


def _steiner_cap(area_sum: float, n_input: int) -> int:
    """A generous bound on the points Triangle may add, so it always terminates.

    A healthy mesh needs about 2 points per unit of (area / max triangle area); allow
    far more than that plus slack for boundary refinement.
    """
    return int(8 * area_sum + 50 * n_input + 20_000)


def _triangulate(data: dict, switches: str, cap: int) -> dict:
    n_in = len(data["vertices"])
    out = triangle.triangulate(data, f"{switches}S{cap}")
    if len(out.get("vertices", ())) - n_in >= cap:
        raise MeshCapHit()
    return out


def mesh_polygon(poly: Polygon, constraints: Sequence[BaseGeometry], max_area: float,
                 min_angle: float, size_fn=None, warn=None) -> Tuple[np.ndarray, np.ndarray]:
    """Quality-mesh a polygon (with holes) honoring extra constraint lines inside it.

    ``size_fn(xy) -> h`` optionally gives the desired edge length at triangle centroids;
    the mesh is then refined once more with per-triangle area limits. ``warn(text)``
    receives notes when the mesher has to fall back.
    """
    warn = warn or (lambda _t: None)
    clipped_constraints = [g for g in (_clip_constraint(c, poly) for c in constraints)
                           if g is not None]
    holes = [Polygon(r).representative_point() for r in poly.interiors if len(r.coords) >= 4]

    def first_pass(with_constraints: bool):
        lines: List[BaseGeometry] = [poly.exterior, *poly.interiors]
        if with_constraints:
            lines += clipped_constraints
        verts, segs = _linework(lines)
        if len(segs) < 3:
            return None
        if pslg_problems(verts, segs):
            raise MeshCapHit()      # treat malformed input like a non-converging mesh
        data = {"vertices": verts, "segments": segs}
        if holes:
            data["holes"] = np.array([[h.x, h.y] for h in holes])
        cap = _steiner_cap(poly.area / max_area, len(verts))
        return _triangulate(data, f"pq{min_angle:g}a{max_area:.6g}Q", cap)

    where = f"near ({poly.centroid.x:.2f}, {poly.centroid.y:.2f}) mm"
    try:
        out = first_pass(True)
    except MeshCapHit:
        if not clipped_constraints:
            raise MeshError(f"Mesher could not converge on the copper {where}") from None
        warn(f"Mesher struggled with pad outlines {where}; meshing without them "
             "(pads are still found by position)")
        try:
            out = first_pass(False)
        except MeshCapHit:
            raise MeshError(f"Mesher could not converge on the copper {where}") from None
    if out is None:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    tris = out.get("triangles")
    if tris is None or len(tris) == 0:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    pts = np.asarray(out["vertices"], dtype=float)
    tris = np.asarray(tris, dtype=np.int64)
    if size_fn is not None:
        h = size_fn(pts[tris].mean(axis=1))
        limit = np.minimum(math.sqrt(3) / 4 * h * h, max_area)
        if np.any(limit < max_area):
            x, y = pts[tris, 0], pts[tris, 1]
            area = 0.5 * np.abs((x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0])
                                - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0]))
            cap = _steiner_cap(float((area / limit).sum()), len(pts))
            try:
                ref = _triangulate({"vertices": pts, "triangles": tris,
                                    "segments": out["segments"], "triangle_max_area": limit},
                                   f"rpq{min_angle:g}aQ", cap)
                pts = np.asarray(ref["vertices"], dtype=float)
                tris = np.asarray(ref["triangles"], dtype=np.int64)
            except MeshCapHit:
                warn(f"Via refinement {where} did not converge; using the unrefined mesh there")
    return pts, tris


def graded_size(features: np.ndarray, h_min: float, h_max: float, grading: float):
    """Size field growing linearly away from plated holes.

    ``features`` rows are (x, y, radius). Near a hole the potential varies like 1/r,
    which a uniform mesh resolves poorly; this keeps element size proportional to
    the distance from the nearest hole.
    """
    if len(features) == 0 or grading <= 0:
        return None
    tree = cKDTree(features[:, :2])
    radius = features[:, 2]

    def size(xy: np.ndarray) -> np.ndarray:
        dist, idx = tree.query(xy)
        d = np.maximum(dist - radius[idx], 0.0)
        return np.clip(h_min + grading * d, h_min, h_max)
    return size


# ---------------------------------------------------------------------------
# Whole net
# ---------------------------------------------------------------------------

def _pad_geom(pad: Pad, layer: int) -> Optional[BaseGeometry]:
    geoms = [g for g in (poly_to_shapely(p) for p in pad.shapes.get(layer, [])) if g is not None]
    return shapely.union_all(geoms) if geoms else None


def _nodes_in(geom: BaseGeometry, pts: np.ndarray, tree: cKDTree, offset: int) -> np.ndarray:
    """Global ids of nodes (of one layer) inside ``geom`` grown by RING_EPS_MM."""
    minx, miny, maxx, maxy = geom.bounds
    c = ((minx + maxx) / 2, (miny + maxy) / 2)
    r = math.hypot(maxx - minx, maxy - miny) / 2 + 2 * RING_EPS_MM
    cand = np.asarray(tree.query_ball_point(c, r), dtype=np.int64)
    if len(cand) == 0:
        return cand
    grown = geom.buffer(RING_EPS_MM)
    inside = shapely.contains_xy(grown, pts[cand, 0], pts[cand, 1])
    return cand[inside] + offset


def _via_ring_centers(vias, pads, layer: int, terminal_ids) -> Optional[cKDTree]:
    """Centers of plated holes on ``layer`` whose ring could be dead copper."""
    pts = [v.position for v in vias if layer in v.layers]
    pts += [p.position for p in pads
            if p.pad_type == "pth" and layer in p.shapes and p.id not in terminal_ids]
    return cKDTree(np.asarray(pts)) if pts else None


def _is_dead_via_ring(poly: Polygon, centers: Optional[cKDTree], terminals) -> bool:
    """A copper island that is only one plated hole's annular ring (one hole, small, centered
    on the hole) touches nothing but its own barrel, so no current can flow through it."""
    if centers is None or len(poly.interiors) != 1:
        return False
    c = poly.centroid
    dist, _ = centers.query((c.x, c.y))
    if dist > 1e-3:
        return False
    minx, miny, maxx, maxy = poly.bounds
    if max(maxx - minx, maxy - miny) > 3.0:          # anything bigger than a via pad
        return False
    return not any(poly.intersects(t) for t in terminals)


def _hole_features(snap: BoardSnapshot, net: str, layer: int) -> np.ndarray:
    _, vias, pads, _ = snap.items_on_net(net)
    rows = [(v.position[0], v.position[1], v.drill_mm / 2) for v in vias
            if layer in v.layers and v.drill_mm > 0]
    rows += [(p.position[0], p.position[1], min(p.drill_mm) / 2) for p in pads
             if p.pad_type == "pth" and layer in p.shapes and min(p.drill_mm) > 0]
    return np.asarray(rows, dtype=float).reshape(-1, 3)


def mesh_net(snap: BoardSnapshot, net: str, terminal_pad_ids: Sequence[str] = (),
             max_edge_mm: float = 0.5, min_angle_deg: float = 28.0, simplify_mm: float = 0.003,
             circle_quad_segs: int = 2, min_edge_mm: float = 0.02, grading: float = 0.2,
             progress: ProgressReporter = NULL) -> NetMesh:
    st = snap.stackup
    _, vias, pads, _ = snap.items_on_net(net)
    pad_by_id = {p.id: p for p in pads}
    missing = [pid for pid in terminal_pad_ids if pid not in pad_by_id]
    if missing:
        raise MeshError(f"Terminal pads are not on net {net}: {missing}")
    terminals = [pad_by_id[pid] for pid in dict.fromkeys(terminal_pad_ids)]
    max_area = math.sqrt(3) / 4 * max_edge_mm ** 2

    all_pts, all_tris, node_layer, tri_layer = [], [], [], []
    skipped_rings = 0
    layer_ranges: Dict[int, Tuple[int, int]] = {}
    n_nodes = 0
    t0 = time.perf_counter()
    copper_layers = [c for c in st.copper]
    for li, cu in enumerate(copper_layers):
        progress.check_cancel()
        geom = net_layer_copper(snap, net, cu.id, quad_segs=circle_quad_segs)
        if geom.is_empty:
            continue
        if simplify_mm > 0:
            geom = geom.simplify(simplify_mm, preserve_topology=True)
        if geom.area >= ANNOUNCE_LAYER_MM2:
            # Big planes take seconds each; say so before starting, not only after.
            progress.info(f"  {cu.name}: meshing {geom.area:.0f} mm² of copper...")
        constraints = [g.boundary for g in (_pad_geom(p, cu.id) for p in terminals) if g is not None]
        size_fn = graded_size(_hole_features(snap, net, cu.id), min_edge_mm, max_edge_mm, grading)
        start = n_nodes
        n_layer_tris = 0
        ring_centers = _via_ring_centers(vias, pads, cu.id, set(terminal_pad_ids))
        terminal_geoms = [g for g in (_pad_geom(p, cu.id) for p in terminals) if g is not None]
        for poly in iter_polygons(geom):
            if poly.area < 1e-6:
                continue
            if _is_dead_via_ring(poly, ring_centers, terminal_geoms):
                skipped_rings += 1
                continue
            pts, tris = mesh_polygon(poly, constraints, max_area, min_angle_deg, size_fn,
                                     warn=progress.warn)
            if len(tris) == 0:
                continue
            all_pts.append(pts)
            all_tris.append(tris + n_nodes)
            node_layer.append(np.full(len(pts), cu.id))
            tri_layer.append(np.full(len(tris), cu.id))
            n_nodes += len(pts)
            n_layer_tris += len(tris)
        if n_nodes > start:
            layer_ranges[cu.id] = (start, n_nodes)
            progress.info(f"  {cu.name}: {n_nodes - start} nodes, {n_layer_tris} triangles")
        progress.fraction((li + 1) / len(copper_layers))

    if not all_pts:
        raise MeshError(f"Net {net} has no copper to mesh")
    points = np.vstack(all_pts)
    mesh = NetMesh(net=net, points=points, node_layer=np.concatenate(node_layer),
                   tris=np.vstack(all_tris), tri_layer=np.concatenate(tri_layer),
                   layers=[c.id for c in st.copper if c.id in layer_ranges],
                   barrels=[], pad_nodes={})

    trees = {lid: cKDTree(points[a:b]) for lid, (a, b) in layer_ranges.items()}

    # Plated holes: collect each hole's boundary nodes per layer.
    n_holes = sum(v.drill_mm > 0 for v in vias) + sum(
        p.pad_type == "pth" and p.drill_mm[0] > 0 for p in pads)
    if n_holes >= ANNOUNCE_HOLES:
        progress.info(f"  Joining layers through {n_holes} plated holes...")
    def ring(lid, hole_geom):
        a, b = layer_ranges[lid]
        return _nodes_in(hole_geom, points[a:b], trees[lid], a)

    for v in vias:
        if v.drill_mm <= 0:
            continue
        bar = Barrel(owner_id=v.id, kind="via", position=v.position, drill_mm=v.drill_mm,
                     hole_type=v.via_type, filled=v.filled)
        hole = Point(v.position).buffer(v.drill_mm / 2, quad_segs=circle_quad_segs)
        for lid in v.layers:
            if lid in layer_ranges:
                nodes = ring(lid, hole)
                if len(nodes):
                    bar.ring_nodes[lid] = nodes
        if bar.ring_nodes:   # even one layer: the plated wall shorts the hole edge
            mesh.barrels.append(bar)
    # The net's holes on each layer, unioned once per layer (not once per pad and layer)
    # and indexed so each pad finds its own part quickly.
    layer_holes: Dict[int, Optional[Tuple[np.ndarray, STRtree]]] = {}

    def pad_hole(lid, pos):
        if lid not in layer_holes:
            hole = drill_holes(snap, net, lid, circle_quad_segs)
            parts = shapely.get_parts(hole) if hole is not None else None
            layer_holes[lid] = (parts, STRtree(parts)) if parts is not None else None
        if layer_holes[lid] is None:
            return None
        parts, tree = layer_holes[lid]
        hits = tree.query(Point(pos), predicate="dwithin", distance=1e-6)
        # The first such part in union order, as a linear scan would find it.
        return parts[hits.min()] if len(hits) else None

    for p in pads:
        if p.pad_type != "pth" or p.drill_mm[0] <= 0:
            continue
        bar = Barrel(owner_id=p.id, kind="pth", position=p.position,
                     drill_mm=min(p.drill_mm[0], p.drill_mm[1] or p.drill_mm[0]),
                     hole_type="pth", filled=False)
        for lid in [c.id for c in st.copper if c.id in p.shapes and c.id in layer_ranges]:
            # The net's holes on this layer; keep only the part belonging to this pad.
            mine = pad_hole(lid, p.position)
            if mine is None:
                continue
            nodes = ring(lid, mine)
            if len(nodes):
                bar.ring_nodes[lid] = nodes
        if bar.ring_nodes:   # even one layer: the plated wall shorts the hole edge
            mesh.barrels.append(bar)

    # Terminal pads: nodes inside the pad outline on each of its layers.
    for p in terminals:
        per_layer: Dict[int, np.ndarray] = {}
        for lid in p.shapes:
            if lid not in layer_ranges:
                continue
            g = _pad_geom(p, lid)
            if g is None:
                continue
            nodes = ring(lid, g)
            if len(nodes):
                per_layer[lid] = nodes
        if not per_layer:
            raise MeshError(f"Pad {p.label} on net {net} has no meshed copper")
        mesh.pad_nodes[p.id] = per_layer

    if skipped_rings:
        progress.info(f"Skipped {skipped_rings} isolated via/PTH ring(s): with nothing else "
                      "attached they cannot carry current")
    progress.info(f"Mesh: {mesh.n_nodes} nodes, {mesh.n_tris} triangles on "
                  f"{len(mesh.layers)} layer(s), {len(mesh.barrels)} plated holes "
                  f"({time.perf_counter() - t0:.2f} s)")
    return mesh


def combine_meshes(meshes: Sequence[NetMesh], name: str, layer_order: Sequence[int]) -> NetMesh:
    """Concatenate per-net meshes into one (node ids offset; nothing is connected)."""
    pts, nl, tris, tl, barrels, pads = [], [], [], [], [], {}
    off = 0
    for m in meshes:
        pts.append(m.points)
        nl.append(m.node_layer)
        tris.append(m.tris + off)
        tl.append(m.tri_layer)
        for b in m.barrels:
            barrels.append(Barrel(b.owner_id, b.kind, b.position, b.drill_mm,
                                  {l: n + off for l, n in b.ring_nodes.items()},
                                  b.hole_type, b.filled))
        for pid, per_layer in m.pad_nodes.items():
            pads[pid] = {l: n + off for l, n in per_layer.items()}
        off += m.n_nodes
    present = {l for m in meshes for l in m.layers}
    return NetMesh(net=name, points=np.vstack(pts), node_layer=np.concatenate(nl),
                   tris=np.vstack(tris), tri_layer=np.concatenate(tl),
                   layers=[l for l in layer_order if l in present], barrels=barrels,
                   pad_nodes=pads)


def mesh_nets(snap: BoardSnapshot, nets: Sequence[str], terminal_pad_ids: Sequence[str] = (),
              name: Optional[str] = None, progress: ProgressReporter = NULL,
              **kwargs) -> NetMesh:
    """Mesh several nets (joined electrically by series parts) as one problem."""
    if len(nets) == 1:
        return mesh_net(snap, nets[0], terminal_pad_ids, progress=progress, **kwargs)
    pad_net = {p.id: p.net for p in snap.pads}
    meshes = []
    for net in nets:
        progress.info(f"-- {net}")
        mine = [pid for pid in terminal_pad_ids if pad_net.get(pid) == net]
        meshes.append(mesh_net(snap, net, mine, progress=progress, **kwargs))
    stray = [pid for pid in terminal_pad_ids if pad_net.get(pid) not in nets]
    if stray:
        raise MeshError(f"Terminal pads are not on any of {list(nets)}: {stray}")
    mesh = combine_meshes(meshes, name or " + ".join(nets), [c.id for c in snap.stackup.copper])
    progress.info(f"Combined mesh: {mesh.n_nodes} nodes, {mesh.n_tris} triangles, "
                  f"{len(nets)} nets")
    return mesh
