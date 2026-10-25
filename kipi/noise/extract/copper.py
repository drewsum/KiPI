"""Where a capacitor's loop current flows in the copper (#187).

A hot loop's current takes the copper's own path: the rail's pour and traces from the
capacitor's pad to the switching device's pins, and the ground's copper, vias and plane
from the device's ground pins back to the capacitor. Each leg is a DC conduction solve
(:mod:`kipi.dc.solver.fem`) on the mesh of the rails and ground the board model uses:
1 A enters the copper at one terminal and leaves at the other, the pins held at one
potential, and the solve gives every triangle's sheet current and every via barrel's.

That current distribution is the loop's source. For the partial inductances
(:mod:`kipi.noise.extract.peec`) it is binned into cells, each a short segment at the
cell's current-weighted centre along the current's direction, carrying the cell's
current; the cells grow with their distance from the loop, since the far spread of the
current over a plane matters little to a victim beside the loop, and far enough away
they are left out. The via barrels are vertical segments.

For the 3D view, streamlines are traced on the same field from the terminal the current
enters at to the one it leaves at: straight within each triangle (the potential is
linear there), through the via barrels where the current changes layer, taking the
layer that carries the most of it. They start at equal shares of the current leaving
the entry pad (through its copper, or straight down a via in the pad), so where they
bunch the current is densest.

The DC distribution is the path of least resistance. At the ringing frequency the
return in a solid plane gathers under the forward path instead; the plane's images
(:mod:`.inductive`) carry that, and the plane's own DC current counts only by what the
plane lets through at each frequency.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from kipi.core.mesh.mesher import NetMesh
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.solver.fem import LinearSystem, triangle_geometry
from kipi.dc.solver.setup import CurrentLoad, NetProblem, VoltageSource
from kipi.noise.extract import peec

CELL_MM = 0.5                   # the finest current cell
CELL_GROWTH = 6.0               # cells grow to their distance from the loop over this
REACH = 6.0                     # cells beyond this many loop sizes away are left out
MIN_REACH_MM = 10.0
STREAMLINES = 5                 # per leg
MAX_STEPS = 20000               # triangles one streamline may cross
SIMPLIFY_MM = 0.02              # a streamline's corners this close to straight are dropped
_TINY = 1e-12


@dataclass
class Flow:
    """1 A through the copper, from the pads at ``start`` to the pads at ``end``."""
    start: List[str]
    end: List[str]
    k: np.ndarray                   # (M, 2) sheet current per triangle, A/mm, along the flow
    barrels: List[Tuple[int, int, int, float]]   # (barrel, layer a, layer b, A from a to b)
    r_ohm: float                    # the copper's resistance along it
    v: np.ndarray                   # (N,) the potential at each node: it falls along the flow
    merged_of: np.ndarray           # (N,) the solve's unknown each node is part of
    merged: Optional[tuple] = None  # unknown -> its nodes, as (pointers, nodes)


class _Quiet(ProgressReporter):
    """The conduction solves' own notes (the other net's copper is an island to each)
    go to the debug log only."""

    def message(self, text: str, level: int = 0):
        super().message(text, logging.DEBUG)


_QUIET = _Quiet()


class CopperPaths:
    """Current paths through the copper of a mesh of the rails and ground, from DC
    conduction solves; one factorization per set of fixed pads (the device's pins).
    ``settings`` are the DC solve settings (resistivity, via plating), ``ac_settings`` the
    AC mesh settings, for meshing another net (:meth:`for_net`)."""

    def __init__(self, snap: BoardSnapshot, mesh: NetMesh, settings, ac_settings=None,
                 progress: ProgressReporter = NULL):
        self.snap, self.mesh, self.settings, self.progress = snap, mesh, settings, progress
        self.ac = ac_settings
        self._others: Dict[tuple, Optional["CopperPaths"]] = {}
        pts, tris = mesh.points, mesh.tris
        self.b, self.c, self.two_a = triangle_geometry(pts, tris)
        x, y = pts[tris, 0], pts[tris, 1]
        # λ_i = (a_i + b_i·x + c_i·y) / 2A: the shape functions, for the streamlines.
        self.a = np.stack([x[:, 1] * y[:, 2] - x[:, 2] * y[:, 1],
                           x[:, 2] * y[:, 0] - x[:, 0] * y[:, 2],
                           x[:, 0] * y[:, 1] - x[:, 1] * y[:, 0]], axis=1)
        self.area = np.abs(self.two_a) / 2
        self.centroid = pts[tris].mean(axis=1)
        self.z = {cu.id: cu.z_mid_mm for cu in snap.stackup.copper}
        self.thickness = {cu.id: cu.thickness_mm for cu in snap.stackup.copper}
        self.layer_name = {cu.id: cu.name for cu in snap.stackup.copper}
        self._systems: Dict[Tuple[str, ...], LinearSystem] = {}
        self._edges = None
        self._node_tris = None
        self._ring_of: Optional[Dict[int, int]] = None

    def for_net(self, net: str, terminal_ids: Sequence[str]) -> Optional["CopperPaths"]:
        """Paths through another net's copper (the switch node, which the rails' mesh
        does not hold), meshed on its own with ``terminal_ids`` as terminals; None where
        it cannot be meshed."""
        from kipi.core.mesh.mesher import MeshError, mesh_net
        key = (net, tuple(sorted(terminal_ids)))
        if key not in self._others:
            s, ac = self.settings, self.ac
            try:
                mesh = mesh_net(self.snap, net, terminal_ids,
                                max_edge_mm=ac.max_edge_mm if ac else s.max_edge_mm,
                                min_angle_deg=s.min_angle_deg, simplify_mm=s.simplify_mm,
                                circle_quad_segs=s.circle_quad_segs,
                                min_edge_mm=ac.min_edge_mm if ac else s.min_edge_mm,
                                grading=ac.grading if ac else s.grading, progress=_QUIET)
                self._others[key] = CopperPaths(self.snap, mesh, s, ac, self.progress)
            except MeshError:
                self._others[key] = None
        return self._others[key]

    # ------------------------------------------------------------ solves
    def _system(self, fixed: Sequence[str], groups: Sequence[Sequence[str]]) -> LinearSystem:
        key = tuple(fixed)
        if key not in self._systems:
            have = self.mesh.pad_nodes
            loads = [CurrentLoad(f"pads {i}", [p for p in g if p in have], 0.0, optional=True)
                     for i, g in enumerate(groups)]
            prob = NetProblem("loop", [VoltageSource("pins", [p for p in fixed if p in have],
                                                     0.0)],
                              [ld for ld in loads if ld.pad_ids], self.settings)
            self._systems[key] = LinearSystem(self.snap, self.mesh, prob, _QUIET)
        return self._systems[key]

    def flow(self, fixed: Sequence[str], pads: Sequence[str], from_fixed: bool,
             groups: Sequence[Sequence[str]]) -> Optional[Flow]:
        """1 A between the pads ``fixed`` (held at one potential: the device's pins) and
        ``pads``, from the fixed ones when ``from_fixed``; ``groups`` are every set of
        pads this fixed set will be asked about (one solve each, one factorization).
        None where the copper does not join them."""
        if not [p for p in fixed if p in self.mesh.pad_nodes]:
            return None
        system = self._system(fixed, groups)          # SolveError: the copper cannot be solved
        name = next((ld.name for ld in system.problem.loads
                     if set(ld.pad_ids) == {p for p in pads if p in self.mesh.pad_nodes}),
                    None)
        if name is None or system.dead_pads.get(name):
            return None
        sol = system.solve(currents={name: 1.0 if from_fixed else -1.0})
        mesh = self.mesh
        v_node = np.nan_to_num(sol.node_v)
        v = v_node[mesh.tris]
        grad = np.column_stack([(self.b * v).sum(axis=1), (self.c * v).sum(axis=1)]) \
            / self.two_a[:, None]
        gs = np.array([sol.sheet_g[lid] for lid in mesh.tri_layer])
        k = -gs[:, None] * grad
        barrels = []
        for e in sol.barrel_elements:
            dv = sol.v_merged[e.node_a] - sol.v_merged[e.node_b]
            if np.isfinite(dv) and abs(e.g * dv) > _TINY:
                barrels.append((e.barrel, e.layer_a, e.layer_b, float(e.g * dv)))
        live = [p for p in pads if p in mesh.pad_nodes]
        volts = [float(sol.node_v[next(iter(mesh.pad_nodes[p].values()))[0]]) for p in live]
        r = abs(float(np.mean(volts)))
        start, end = (list(fixed), live) if from_fixed else (live, list(fixed))
        merged_of = sol.merged_of
        order = np.argsort(merged_of, kind="stable")
        ptr = np.concatenate([[0], np.cumsum(np.bincount(merged_of))])
        return Flow(start, end, k, barrels, r, v_node, merged_of, (ptr, order))

    # ------------------------------------------------------------ elements
    def elements(self, flow: Flow, centre: Tuple[float, float], size_mm: float,
                 label: str = "") -> Tuple[List[peec.Segment], np.ndarray]:
        """The flow's current as segments for the partial inductances, each with the
        amperes it carries per ampere of the flow: cells of the copper's current (finest
        near the loop's ``centre``, growing with distance, none beyond its reach) and the
        via barrels."""
        mesh = self.mesh
        m = flow.k * self.area[:, None]                       # A·mm per triangle
        mag = np.hypot(m[:, 0], m[:, 1])
        d = np.hypot(self.centroid[:, 0] - centre[0], self.centroid[:, 1] - centre[1])
        reach = max(REACH * size_mm, MIN_REACH_MM)
        keep = np.flatnonzero((mag > _TINY) & (d <= reach))
        segs: List[peec.Segment] = []
        weights: List[float] = []
        if len(keep):
            level = np.floor(np.log2(np.maximum(d[keep] / (CELL_GROWTH * CELL_MM), 1.0)))
            s = CELL_MM * 2.0 ** level
            ix = np.floor(self.centroid[keep, 0] / s).astype(np.int64)
            iy = np.floor(self.centroid[keep, 1] / s).astype(np.int64)
            lid = mesh.tri_layer[keep]
            key = np.stack([lid, level.astype(np.int64), ix, iy], axis=1)
            cells, inv = np.unique(key, axis=0, return_inverse=True)
            inv = inv.ravel()
            n = len(cells)
            mx = np.bincount(inv, m[keep, 0], n)
            my = np.bincount(inv, m[keep, 1], n)
            w = np.bincount(inv, mag[keep], n)
            cx = np.bincount(inv, mag[keep] * self.centroid[keep, 0], n) / w
            cy = np.bincount(inv, mag[keep] * self.centroid[keep, 1], n) / w
            cs = CELL_MM * 2.0 ** cells[:, 1]
            cm = np.hypot(mx, my)
            for i in np.flatnonzero(cm > 1e-4 * cm.max()):
                u = np.array([mx[i], my[i]]) / cm[i]
                z = self.z[int(cells[i, 0])]
                half = 0.5 * cs[i] * u
                segs.append(peec.segment((cx[i] - half[0], cy[i] - half[1], z),
                                         (cx[i] + half[0], cy[i] + half[1], z), cs[i],
                                         self.thickness[int(cells[i, 0])],
                                         f"{label} {self.layer_name[int(cells[i, 0])]}"))
                weights.append(float(cm[i] / cs[i]))
        for bi, la, lb, amps in flow.barrels:
            bar = mesh.barrels[bi]
            x, y = bar.position
            if math.hypot(x - centre[0], y - centre[1]) > reach:
                continue
            # The tube's geometric mean radius is its radius: w = t with GMD 0.2235·(w+t).
            w = bar.drill_mm / (4 * peec.GMD_FACTOR)
            a, b = (la, lb) if amps > 0 else (lb, la)
            segs.append(peec.segment((x, y, self.z[a]), (x, y, self.z[b]), w, w,
                                     f"{label} via {bar.owner_id}"))
            weights.append(abs(amps))
        return segs, np.array(weights, float)

    # ------------------------------------------------------------ streamlines
    def _index(self):
        """Edge → triangles, node → triangles and ring node → barrel, built once."""
        mesh = self.mesh
        tris = mesh.tris
        n = mesh.n_nodes
        e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
        e.sort(axis=1)
        key = e[:, 0].astype(np.int64) * n + e[:, 1]
        order = np.argsort(key, kind="stable")
        self._edges = (key[order], np.tile(np.arange(len(tris)), 3)[order])
        order = np.argsort(tris.ravel(), kind="stable")
        self._node_tris = (np.concatenate([[0], np.cumsum(np.bincount(tris.ravel(),
                                                                     minlength=n))]),
                           order // 3)
        self._ring_of = {}
        for bi, bar in enumerate(mesh.barrels):
            for nodes in bar.ring_nodes.values():
                for nd in nodes:
                    self._ring_of[int(nd)] = bi

    def _edge_tris(self, i: int, j: int) -> np.ndarray:
        keys, tri = self._edges
        lo, hi = (i, j) if i < j else (j, i)
        k = lo * self.mesh.n_nodes + hi
        a = np.searchsorted(keys, k)
        b = np.searchsorted(keys, k, side="right")
        return tri[a:b]

    def _tris_of(self, node: int) -> np.ndarray:
        ptr, tri = self._node_tris
        return tri[ptr[node]:ptr[node + 1]]

    def _exits(self, flow: Flow, nodes, lid: Optional[int] = None):
        """The edges of the region ``nodes`` (a pad or a ring) that current leaves across,
        on layer ``lid`` if given: [(triangle, node j, node k, flux)], flux > 0, in order
        around the region."""
        mesh = self.mesh
        pts = mesh.points
        inside = {int(x) for x in nodes}
        seen = set()
        out = []
        cx, cy = pts[list(inside)].mean(axis=0)
        for nd in inside:
            if lid is not None and mesh.node_layer[nd] != lid:
                continue
            for t in self._tris_of(nd):
                t = int(t)
                if t in seen:
                    continue
                seen.add(t)
                tn = [int(x) for x in mesh.tris[t]]
                on = [x for x in tn if x in inside]
                if len(on) != 2:
                    continue
                j, k = on
                third = next(x for x in tn if x not in inside)
                mid = 0.5 * (pts[j] + pts[k])
                normal = pts[third] - mid
                normal /= max(np.linalg.norm(normal), _TINY)
                flux = float(flow.k[t] @ normal) * float(np.linalg.norm(pts[j] - pts[k]))
                if flux > _TINY:
                    out.append((t, j, k, flux, math.atan2(mid[1] - cy, mid[0] - cx)))
        out.sort(key=lambda r: r[4])
        return [r[:4] for r in out]

    def _down_barrel(self, flow: Flow, bi: int, lid: int) -> List[Tuple[int, float]]:
        """The layers the current goes on to from barrel ``bi``'s ring on ``lid``, with
        the amperes to each."""
        out = []
        for b, la, lb, amps in flow.barrels:
            if b == bi and la == lid and amps > 0:
                out.append((lb, amps))
            elif b == bi and lb == lid and amps < 0:
                out.append((la, -amps))
        return out

    def _region(self, flow: Flow, node: int) -> np.ndarray:
        """The nodes at one potential with ``node``: a pad's, a via ring's, or a pad's with
        the rings of the vias in it (one merged unknown of the solve)."""
        if flow.merged is None:
            return np.array([node])
        ptr, nodes = flow.merged
        u = flow.merged_of[node]
        return nodes[ptr[u]:ptr[u + 1]]

    def _ways(self, flow: Flow, region) -> List[tuple]:
        """How the current leaves a one-potential ``region``: across its edges into the
        copper, ("copper", (triangle, node j, node k), amps), or down a via whose ring is
        part of it, ("barrel", (barrel, from layer, to layer), amps)."""
        ways = [("copper", (t, j, k), flux) for t, j, k, flux in self._exits(flow, region)]
        seen = set()
        for nd in region:
            bi = self._ring_of.get(int(nd))
            if bi is None:
                continue
            lid = int(self.mesh.node_layer[nd])
            if (bi, lid) not in seen:
                seen.add((bi, lid))
                for layer, amps in self._down_barrel(flow, bi, lid):
                    ways.append(("barrel", (bi, lid, layer), amps))
        return ways

    @staticmethod
    def _pick(ways: List[tuple], share: float):
        """The way at ``share`` (0-1) of the current through them all."""
        total = sum(w[2] for w in ways)
        if total <= _TINY:
            return None
        acc = 0.0
        for w in ways:
            acc += w[2]
            if acc >= share * total:
                return w
        return ways[-1]

    def _seeds(self, flow: Flow, region: set, n: int):
        """``n`` starts at equal shares of the current leaving ``region`` (the start pads'
        nodes): across its edges into the copper, ("tri", triangle, xy, node), or straight
        down a via whose ring is part of it, ("via", barrel, layer, node)."""
        pts = self.mesh.points
        ways = self._ways(flow, region)
        seeds = []
        for q in (np.arange(n) + 0.5) / n:
            w = self._pick(ways, q)
            if w is None:
                break
            if w[0] == "copper":
                t, j, k = w[1]
                seeds.append(("tri", t, 0.5 * (pts[j] + pts[k]), j))
            else:
                bi, lid, _ = w[1]
                nd = next(int(x) for x in region if self._ring_of.get(int(x)) == bi
                          and self.mesh.node_layer[x] == lid)
                seeds.append(("via", bi, lid, nd))
        return seeds

    def _leave(self, flow: Flow, region, share: float, end: set, path: list):
        """Follow the current out of the one-potential ``region``, appending any via
        drops to ``path``: ("done", node) where the region (or one it reaches down a via)
        is part of the end region, ("tri", t, xy, layer) where the current comes out into
        copper, None where it goes nowhere. The way is picked by ``share`` (0-1) of the
        current among the ways out."""
        mesh = self.mesh
        pts = mesh.points
        for _ in range(4 * len(self.z) + 4):          # regions along a chain of vias
            hit = next((int(nd) for nd in region if int(nd) in end), None)
            if hit is not None:
                return "done", hit
            w = self._pick(self._ways(flow, region), share)
            if w is None:
                return None
            if w[0] == "copper":
                t, j, k = w[1]
                lid = int(mesh.tri_layer[t])
                p = 0.5 * (pts[j] + pts[k])
                path.append((p[0], p[1], self.z[lid]))
                return "tri", t, p, lid
            bi, lid, layer = w[1]
            x, y = mesh.barrels[bi].position
            path.append((x, y, self.z[lid]))
            path.append((x, y, self.z[layer]))
            region = self._region(flow, int(mesh.barrels[bi].ring_nodes[layer][0]))
        return None

    def _hop(self, flow: Flow, nodes: Sequence[int], hopped: set):
        """Where a streamline is stuck between triangles (the element current points back
        across an edge, as a finite-element gradient can), the current still runs downhill
        in the potential: the lowest of ``nodes`` not yet hopped to, and the triangle
        around it whose current leads away from it: (node, triangle, xy) or None."""
        pts = self.mesh.points
        for nd in sorted(nodes, key=lambda x: flow.v[x]):
            if nd in hopped:
                continue
            hopped.add(nd)
            best, best_t = 0.0, None
            for t in self._tris_of(nd):
                t = int(t)
                away = self.centroid[t] - pts[nd]
                along = float(flow.k[t] @ away) / max(float(np.linalg.norm(away)), _TINY)
                if along > best:
                    best, best_t = along, t
            if best_t is not None:
                return nd, best_t, pts[nd] + 0.05 * (self.centroid[best_t] - pts[nd])
        return None

    def _trace(self, flow: Flow, start, end: set, share: float) -> Tuple[list, Optional[int]]:
        """One streamline from ``start`` (a seed) to the region ``end``: its points and
        the end node it reached (None if it did not)."""
        mesh = self.mesh
        pts = mesh.points
        path: list = []
        if start[0] == "via":
            _, bi, lid, nd = start
            x, y = mesh.barrels[bi].position
            path.append((x, y, self.z[lid]))
            got = self._leave(flow, self._region(flow, nd), share, end, path)
            if got is None:
                return path, None
            if got[0] == "done":
                return path, got[1]
            _, t, p, lid = got
        else:
            _, t, p, _ = start
            lid = int(mesh.tri_layer[t])
            path.append((p[0], p[1], self.z[lid]))
        p = np.asarray(p, float)
        hopped: set = set()

        def leave(nd, q):
            """Into the one-potential region of node ``nd`` at point q: the new
            (t, p, lid), the end node, or None."""
            path.append((q[0], q[1], self.z[lid]))
            got = self._leave(flow, self._region(flow, nd), share, end, path)
            if got is None or got[0] == "done":
                return got
            return "tri", got[1], np.asarray(got[2], float), got[3]

        for _ in range(MAX_STEPS):
            nodes = [int(x) for x in mesh.tris[t]]
            if all(nd in end for nd in nodes):
                return path, nodes[0]
            k = flow.k[t]
            kn = float(np.hypot(k[0], k[1]))
            moved = False
            if kn > _TINY:
                u = k / kn
                lam = (self.a[t] + self.b[t] * p[0] + self.c[t] * p[1]) / self.two_a[t]
                dlam = (self.b[t] * u[0] + self.c[t] * u[1]) / self.two_a[t]
                moving = dlam < -1e-15
                dist = np.where(moving, -lam / np.where(moving, dlam, 1.0), np.inf)
                dist = np.where(dist < 0, 0.0, dist)
                for i in [int(i) for i in np.argsort(dist) if np.isfinite(dist[i])]:
                    q = p + u * dist[i]
                    j, kk = [nodes[x] for x in range(3) if x != i]
                    if j in end and kk in end:
                        path.append((q[0], q[1], self.z[lid]))
                        return path, j
                    others = [int(x) for x in self._edge_tris(j, kk) if x != t]
                    if others:
                        # Across the edge the current must carry on, not point back.
                        nb = others[0]
                        edge = pts[kk] - pts[j]
                        normal = np.array([edge[1], -edge[0]])
                        if normal @ (self.centroid[nb] - self.centroid[t]) < 0:
                            normal = -normal
                        if flow.k[nb] @ normal < 0:
                            continue
                        path.append((q[0], q[1], self.z[lid]))
                        t, p, moved = nb, q, True
                        break
                    if flow.merged_of[j] == flow.merged_of[kk] and len(self._region(flow, j)) > 1:
                        got = leave(j, q)              # a via ring, or a pad's outline
                        if got is None:
                            return path, None
                        if got[0] == "done":
                            return path, got[1]
                        _, t, p, lid = got
                        moved = True
                        break
            if moved:
                continue
            same = {flow.merged_of[nd] for nd in nodes}
            if kn <= _TINY and len(same) == 1 and len(self._region(flow, nodes[0])) > 1:
                got = leave(nodes[0], p)                # inside a one-potential region
                if got is None:
                    return path, None
                if got[0] == "done":
                    return path, got[1]
                _, t, p, lid = got
                continue
            hop = self._hop(flow, nodes, hopped)
            if hop is None:
                break
            nd, t_next, q = hop
            if nd in end:
                path.append((pts[nd][0], pts[nd][1], self.z[lid]))
                return path, nd
            if len(self._region(flow, nd)) > 1:
                got = leave(nd, pts[nd])
                if got is None:
                    return path, None
                if got[0] == "done":
                    return path, got[1]
                _, t, p, lid = got
                continue
            path.append((pts[nd][0], pts[nd][1], self.z[lid]))
            t, p = t_next, np.asarray(q, float)
        return path, None

    def streamlines(self, flow: Flow, n: int = STREAMLINES) -> Tuple[List[np.ndarray], bool]:
        """``n`` streamlines of the flow from the middle of its start pads to the middle
        of its end pads, each (N, 3) mm, and whether every one arrived."""
        if self._edges is None:
            self._index()
        mesh = self.mesh

        position = {p.id: p.position for p in self.snap.pads}

        def centres(pads):
            """Each pad's nodes, and each node's pad centre, per layer."""
            region, centre = set(), {}
            for p in pads:
                for lid, nodes in mesh.pad_nodes.get(p, {}).items():
                    c = position.get(p) or mesh.points[nodes].mean(axis=0)
                    for nd in nodes:
                        region.add(int(nd))
                        centre[int(nd)] = (c[0], c[1], self.z[lid])
            return region, centre
        start, start_c = centres(flow.start)
        end, end_c = centres(flow.end)
        lines, complete = [], True
        seeds = self._seeds(flow, start, n)
        for i, seed in enumerate(seeds):
            path, got = self._trace(flow, seed, end, (i + 0.5) / n)
            complete &= got is not None
            path = [start_c[seed[3]], *path] + ([end_c[got]] if got is not None else [])
            if len(path) >= 2:
                lines.append(_simplify(np.array(path, float)))
        return lines, complete and bool(seeds)


def _simplify(path: np.ndarray) -> np.ndarray:
    """Drop a streamline's corners that are within SIMPLIFY_MM of straight, one layer's
    run at a time (the via drops stay)."""
    from shapely.geometry import LineString
    out = []
    i = 0
    while i < len(path):
        j = i
        while j + 1 < len(path) and path[j + 1, 2] == path[i, 2]:
            j += 1
        run = path[i:j + 1]
        if len(run) > 2:
            xy = np.asarray(LineString(run[:, :2]).simplify(SIMPLIFY_MM).coords)
            run = np.column_stack([xy, np.full(len(xy), run[0, 2])])
        out.append(run)
        i = j + 1
    return np.vstack(out)
