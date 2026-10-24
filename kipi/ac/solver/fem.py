"""2.5D AC model of one rail and its ground: copper sheets with two faces, the capacitance
between facing rail and ground copper, and plated barrels.

Every copper sheet has two faces (top and bottom). A triangle couples their potentials
through the 2x2 sheet impedance

    Z = [[Z11 + jωμ₀h_top/2, Z12], [Z12, Z11 + jωμ₀h_bot/2]]   (ohms per square)

where Z11, Z12 are the slab's internal impedance (:func:`physics.slab_impedance`: the DC
sheet resistance at low frequency, independent skin-effect faces at high frequency) and
μ₀h/2 is half the loop inductance per square of the cavity that face looks into: h is the
gap to the nearest copper of the *other* side (rail <-> ground) across that face's
dielectric. A face that sees only copper of its own side, or nothing, gets h = board
thickness, unless the sheet's other face sees a return: then it is shielded and carries
almost no high-frequency current. The element matrix is inv(Z) ⊗ (bbᵀ+ccᵀ)/4A, the DC
conduction element with a complex 2x2 sheet admittance.

For two facing planes this is exactly the plane-pair (cavity) model: loop inductance
μ₀h per square, capacitance ε/h per area, so the plane capacitance, spreading inductance
and cavity resonances come out of the mesh. Via inductance comes out the same way: the
current spreading from a barrel on the faces around it gives μ₀h/(2π)·ln(R/r).

The two faces meet wherever current can pass from one to the other around the copper:
on the copper boundary (plane edges, antipads, trace edges), drill rings and terminal
pads. Capacitance ε₀ε_r/h·(1 − j·tanδ) per facing area is lumped onto the rail face nodes
and tied to the nearest node of the facing ground face.

Unknowns: each node has the mean of its face potentials, c = (φ_top + φ_bot)/2, and their
difference, d = φ_top − φ_bot; where the faces meet, d = 0 and the node has no d unknown.
(Per face, the low-frequency system holds large, nearly cancelling face-to-face terms that
direct solvers cannot pivot through accurately; in c and d they separate into a DC-sized
block and a large, diagonally dominant one.)

Lumped parts (capacitors, series links, the probe) are ports: each side's pads are one
node. :meth:`ACSystem.port_impedance` returns the board's port impedance matrix at one
frequency; the parts are attached afterwards (:mod:`.sweep`), which keeps the expensive
field solve independent of the part values.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import scipy.sparse as sp
import shapely
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from kipi.ac.solver.near_rail import near_rail
from kipi.ac.solver.physics import (
    EPS0_F_PER_MM, MU0_H_PER_MM, slab_difference, slab_impedance,
)
from kipi.core.geometry.copper import net_layer_copper
from kipi.core.mesh.mesher import NetMesh, combine_meshes, mesh_net
from kipi.core.model.board import BoardSnapshot, Stackup
from kipi.core.progress import NULL, ProgressReporter
from kipi.core.solver import direct
from kipi.core.temperature import at as temperature_at
from kipi.dc.solver.fem import _merge_groups, triangle_geometry
from kipi.dc.solver.setup import REF_TEMP_C
from kipi.dc.solver.via_barrel import barrel_copper_area, rule_for

TIE_ADMITTANCE_S = 1e-9     # weak tie for copper reached only through parts
SHIELDED_FACE_HEIGHT = 1000  # x board thickness: the inductance height of a shielded face
# Copper at a temperature (#77): triangles whose resistivity is within this fraction share
# one sheet impedance per frequency, so a board's temperature map costs a few evaluations.
RHO_STEP = 1e-3


class ACError(RuntimeError):
    pass



def dielectric_summary(stackup: Stackup, default_er: float) -> str:
    """The εr the AC solve uses for each dielectric, top to bottom: the stackup's, or
    ``default_er`` (marked) where the stackup gives none."""
    if not stackup.dielectrics:
        return f"{default_er:g} (Default εr: the stackup lists no dielectrics)"
    parts = []
    for d in stackup.dielectrics:
        name = d.material or "dielectric"
        er = f"{d.epsilon_r:g}" if d.epsilon_r > 0 else f"{default_er:g} (Default εr)"
        parts.append(f"{name} {d.thickness_mm * 1000:.0f} µm εr {er}")
    note = "" if stackup.from_board else " [stackup not read from the board]"
    if stackup.edited:
        note += " [stackup edited in KiPI]"
    return ", ".join(parts) + note

@dataclass
class PortDef:
    """A two-terminal port: current enters the board at ``plus`` and leaves at ``minus``."""
    name: str
    kind: str                     # "cap", "link" or "probe"
    plus_pads: List[str]
    minus_pads: List[str]
    element: object = None        # CapModel, DcLink or Probe


@dataclass
class ACStats:
    n_nodes: int = 0
    n_tris: int = 0
    n_unknowns: int = 0
    nnz: int = 0
    n_ports: int = 0
    method: str = ""
    times: Dict[str, float] = field(default_factory=dict)
    t_copper: Optional[Tuple[float, float]] = None   # coolest, hottest copper °C (#77)


def _boundary_nodes(tris: np.ndarray) -> np.ndarray:
    """Nodes on edges that belong to only one triangle."""
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    e.sort(axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    return np.unique(uniq[counts == 1].ravel())


class ACSystem:
    """The assembled AC model of one rail (``rail_nets``) against ``gnd_net``: a net, or
    the ground's nets (#171), joined by the series links among the ports and shorts.

    ``ports`` lists the parts and probes; ``short_links`` are pairs of pad lists merged
    into one node (series links with neither resistance nor inductance). The minus side
    of port ``reference_port`` is the 0 V reference.

    ``temperature`` (#77), if given, maps KiCad xy (an (N, 2) array, mm) to the copper's
    temperature in °C, which sets its resistivity; without it the copper is at the DC
    settings' reference temperature.
    """

    def __init__(self, snap: BoardSnapshot, rail_nets: Sequence[str], gnd_net,
                 ports: List[PortDef], short_links: List[Tuple[List[str], List[str]]],
                 dc_settings, ac_settings, progress: ProgressReporter = NULL,
                 reference_port: int = 0,
                 temperature: Optional[Callable[[np.ndarray], np.ndarray]] = None):
        t0 = time.perf_counter()
        self.temperature = temperature
        self.snap, self.ports = snap, ports
        self.rail_nets = list(rail_nets)
        self.gnd_nets = [gnd_net] if isinstance(gnd_net, str) else list(gnd_net)
        self.gnd = self.gnd_nets[0]
        self.dc, self.ac = dc_settings, ac_settings
        self.stats = ACStats(n_ports=len(ports))
        self.warnings: List[str] = []
        st = snap.stackup
        if not ports:
            raise ACError("Nothing to solve: no probe on this rail")

        # ------------------------------------------------------------ mesh
        terminal_ids = list(dict.fromkeys(
            [pid for p in ports for pid in (*p.plus_pads, *p.minus_pads)]
            + [pid for a, b in short_links for pid in (*a, *b)]))
        pad_net = {p.id: p.net for p in snap.pads}
        nets = [*self.rail_nets, *self.gnd_nets]
        stray = [pid for pid in terminal_ids if pad_net.get(pid) not in nets]
        if stray:
            raise ACError(f"Pads are not on the rail or its ground: {stray}")
        # Via grading only near the rail: far stitching vias carry almost none of its
        # current, and grading them is most of a large board's mesh (#123).
        graded = None
        if ac_settings.grade_near_rail_only:
            graded = near_rail(snap, self.rail_nets, terminal_ids, dc_settings.circle_quad_segs)
        meshes: List[NetMesh] = []
        kinds = []
        with progress.timed("Meshing rail and ground"):
            for net in nets:
                progress.check_cancel()
                progress.info(f"-- {net}")
                mine = [pid for pid in terminal_ids if pad_net.get(pid) == net]
                meshes.append(mesh_net(
                    snap, net, mine, max_edge_mm=ac_settings.max_edge_mm,
                    min_angle_deg=dc_settings.min_angle_deg,
                    simplify_mm=dc_settings.simplify_mm,
                    circle_quad_segs=dc_settings.circle_quad_segs,
                    min_edge_mm=ac_settings.min_edge_mm, grading=ac_settings.grading,
                    progress=progress, graded=graded))
                kinds.append(1 if net in self.gnd_nets else 0)
        mesh = combine_meshes(meshes, " + ".join(nets), [c.id for c in st.copper])
        self.mesh = mesh
        node_kind = np.concatenate([np.full(m.n_nodes, k, dtype=np.int8)
                                    for m, k in zip(meshes, kinds)])
        self.node_ground = node_kind == 1           # for the board map
        n = mesh.n_nodes
        self.stats.n_nodes, self.stats.n_tris = n, mesh.n_tris
        progress.info(f"AC mesh: {n} nodes, {mesh.n_tris} triangles")

        with progress.timed("Building the AC model"):
            self._build(node_kind, short_links, progress, reference_port)
        self.stats.times["setup"] = time.perf_counter() - t0
        for w in self.warnings:
            progress.warn(w)

    # ================================================================ build
    # Unknown ids before merging: c of node n = 2n, d of node n = 2n + 1.
    def _build(self, node_kind, short_links, progress, reference_port):
        mesh, st = self.mesh, self.snap.stackup
        n = mesh.n_nodes
        tris = mesh.tris
        tri_kind = node_kind[tris[:, 0]]

        bnodes = _boundary_nodes(tris)
        no_d = np.zeros(n, dtype=bool)              # faces meet: d = 0
        no_d[bnodes] = True
        groups: List[np.ndarray] = []               # c unknowns tied together
        for bar in mesh.barrels:
            for nodes in bar.ring_nodes.values():
                groups.append(2 * nodes)
                no_d[nodes] = True
        pad_nodes = {pid: np.concatenate(list(per.values()))
                     for pid, per in mesh.pad_nodes.items()}
        for nodes in pad_nodes.values():
            groups.append(2 * nodes)
            no_d[nodes] = True

        def side(pads):
            missing = [p for p in pads if p not in pad_nodes]
            if missing:
                raise ACError(f"Pads have no meshed copper: {missing}")
            return 2 * np.concatenate([pad_nodes[p] for p in pads])
        for p in self.ports:
            groups.extend([side(p.plus_pads), side(p.minus_pads)])
        for a, b in short_links:
            groups.append(np.concatenate([side(a), side(b)]))
        merged_of, nm = _merge_groups(2 * n, groups)
        self.merged_of, self.nm = merged_of, nm
        dead = np.zeros(nm, dtype=bool)             # d unknowns fixed at zero
        dead[merged_of[2 * np.flatnonzero(no_d) + 1]] = True

        def unknown(pads):
            return int(merged_of[side(pads)[0]])
        self.port_nodes = [(unknown(p.plus_pads), unknown(p.minus_pads)) for p in self.ports]
        for p, (a, b) in zip(self.ports, self.port_nodes):
            if a == b:
                raise ACError(f"{p.name}: its two sides are connected directly (the same "
                              "copper or a short series link)")

        # --- triangle geometry
        b, c, two_a = triangle_geometry(mesh.points, tris)
        area = np.abs(two_a) / 2
        if np.any(area <= 0):
            raise ACError("Degenerate triangles in the AC mesh")
        S = (b[:, :, None] * b[:, None, :] + c[:, :, None] * c[:, None, :]) \
            / (4 * area)[:, None, None]
        self._S = S.reshape(len(tris), 9)

        # --- what each face looks into
        self._facing(tri_kind, area, node_kind, progress)

        # --- sparsity pattern: c/d blocks per triangle, barrels, capacitance
        rows, cols = [], []
        for s_ in (0, 1):
            for t_ in (0, 1):
                ms = merged_of[2 * tris + s_]
                mt = merged_of[2 * tris + t_]
                rows.append(np.repeat(ms, 3, axis=1).ravel())
                cols.append(np.tile(mt, (1, 3)).ravel())

        # Barrels: resistors between ring unknowns of consecutive layers (as in DC).
        order = {cu.id: i for i, cu in enumerate(st.copper)}
        bar_a, bar_b, bar_g = [], [], []
        for bar in mesh.barrels:
            layers = sorted(bar.ring_nodes, key=order.get)
            rule = rule_for(self.dc.via_rules, bar.hole_type, bar.drill_mm)
            if rule is None:
                raise ACError(f"No via rule covers a {bar.hole_type} hole of {bar.drill_mm:g} "
                              "mm drill; add one in the DC Mesh tab under Via plating")
            a_barrel = barrel_copper_area(rule, bar.drill_mm, bar.filled, self.dc.rho_ohm_mm)
            for la, lb in zip(layers[:-1], layers[1:]):
                dz = abs(st.layer(la).z_mid_mm - st.layer(lb).z_mid_mm)
                ua = merged_of[2 * bar.ring_nodes[la][0]]
                ub = merged_of[2 * bar.ring_nodes[lb][0]]
                if ua != ub:
                    bar_a.append(ua)
                    bar_b.append(ub)
                    bar_g.append(a_barrel / (self._rho_at(self.dc.rho_ohm_mm,
                                                          self.dc.tcr_per_c, bar.position,
                                                          (la, lb))
                                             * max(dz, 1e-6)))
        bar_a, bar_b = np.asarray(bar_a, dtype=np.int64), np.asarray(bar_b, dtype=np.int64)
        self._bar_g = np.asarray(bar_g, dtype=float)
        rows += [bar_a, bar_b, bar_a, bar_b]
        cols += [bar_a, bar_b, bar_b, bar_a]

        # Capacitance between a rail face and a ground face: the face potentials are
        # c ± d/2, so each coupling stamps y·v·vᵀ with v over (c_r, d_r, c_g, d_g).
        u = np.stack([merged_of[2 * self._cap_rn], merged_of[2 * self._cap_rn + 1],
                      merged_of[2 * self._cap_gn], merged_of[2 * self._cap_gn + 1]], axis=1)
        sr = np.where(self._cap_face == 0, 0.5, -0.5)      # top face: c + d/2
        sg = -sr                                            # the ground face is the other one
        v = np.stack([np.ones_like(sr), sr, -np.ones_like(sr), -sg], axis=1)
        self._cap_coef = (v[:, :, None] * v[:, None, :]).reshape(-1, 16)
        rows.append(np.repeat(u, 4, axis=1).ravel())
        cols.append(np.tile(u, (1, 4)).ravel())

        rows = np.concatenate(rows).astype(np.int64)
        cols = np.concatenate(cols).astype(np.int64)
        live_entry = ~(dead[rows] | dead[cols])

        # --- connectivity: copper the reference cannot reach is left out; copper that
        # holds a port terminal but is reached only through parts is tied weakly.
        graph = sp.coo_matrix((np.ones(int(live_entry.sum())),
                               (rows[live_entry], cols[live_entry])), shape=(nm, nm)).tocsr()
        ncomp, comp = connected_components(graph, directed=False)
        ref = self.port_nodes[reference_port][1]
        self.ref = ref
        live_comps = {comp[ref]}
        ties = []
        for p, (a, bb) in zip(self.ports, self.port_nodes):
            for uu in (a, bb):
                if comp[uu] not in live_comps:
                    live_comps.add(comp[uu])
                    ties.append(uu)
                    self.warnings.append(f"{p.name}: its copper reaches the rest of the rail "
                                         "only through parts (tied weakly for the solve)")
        in_live = np.isin(comp, list(live_comps))
        isolated = {int(comp[k]) for k in np.flatnonzero(~in_live & ~dead)}
        if isolated:
            self.warnings.append(f"{len(isolated)} copper island(s) touch neither the rail's "
                                 "parts nor its ground and were left out")
        keep = in_live & ~dead
        keep[ref] = False

        # --- conductors: pieces of copper joined by conduction (sheets, barrels, merged
        # pads), not by capacitance. Every conductor but the reference's floats, tied to
        # the rest only by the plane capacitance (and parts): at low frequency that is
        # ~1e-9 S against ~1e3 S of copper, which no direct solver handles accurately.
        # Each floating conductor is held at one *anchor* unknown (a mean potential c);
        # port_impedance solves the grounded, well-conditioned system for the rest and
        # finds the anchor potentials from each conductor's total current (KCL summed
        # over the conductor), which involves only the capacitive and part currents.
        n_cond = len(rows) - 16 * len(self._cap_c)
        is_cond = np.zeros(len(rows), dtype=bool)
        is_cond[:n_cond] = True
        sel = is_cond & live_entry
        cgraph = sp.coo_matrix((np.ones(int(sel.sum())), (rows[sel], cols[sel])),
                               shape=(nm, nm)).tocsr()
        _, ccomp = connected_components(cgraph, directed=False)
        is_c = np.zeros(nm, dtype=bool)
        is_c[merged_of[2 * np.arange(n)]] = True
        terminals = [u for pn in self.port_nodes for u in pn]
        anchor_of: Dict[int, int] = {}                       # conductor -> anchor unknown
        for u in terminals:                                  # prefer a port terminal
            if keep[u] and ccomp[u] != ccomp[ref]:
                anchor_of.setdefault(int(ccomp[u]), u)
        for u in np.flatnonzero(keep & is_c):
            if ccomp[u] != ccomp[ref]:
                anchor_of.setdefault(int(ccomp[u]), int(u))
        anchors = np.array(sorted(anchor_of.values()), dtype=np.int64)
        free = keep.copy()
        free[anchors] = False

        new_index = np.full(nm, -1, dtype=np.int64)
        self.n_free = int(free.sum())
        new_index[free] = np.arange(self.n_free)             # free unknowns first,
        new_index[anchors] = self.n_free + np.arange(len(anchors))   # anchors last
        self.n = int(keep.sum())
        self.index = new_index
        self._ties = new_index[np.asarray(ties, dtype=np.int64)]
        # KCL rows: the c unknowns of each floating conductor, summed.
        cond_ids = sorted(anchor_of)
        which = {c_: i for i, c_ in enumerate(cond_ids)}
        members = np.flatnonzero(keep & is_c)
        members = members[np.isin(ccomp[members], cond_ids)]
        self._R = sp.csr_matrix(
            (np.ones(len(members)), ([which[int(c_)] for c_ in ccomp[members]],
                                     new_index[members])), shape=(len(cond_ids), self.n))
        # Cross-conductor entries (capacitance), for those sums.
        cap_ok = keep[rows[n_cond:]] & keep[cols[n_cond:]]
        self._x_sel = np.flatnonzero(cap_ok)
        self._x_rows = new_index[rows[n_cond:][cap_ok]]
        self._x_cols = new_index[cols[n_cond:][cap_ok]]
        self.n_conductors = len(cond_ids)

        # Entries between kept unknowns, mapped to the reduced numbering, then CSR with a
        # fixed order: per frequency only the values are recomputed.
        ok = keep[rows] & keep[cols]
        r, cc = new_index[rows[ok]], new_index[cols[ok]]
        diag = np.arange(self.n)
        r = np.concatenate([r, diag])          # every kept unknown gets a diagonal entry
        cc = np.concatenate([cc, diag])
        key = r * self.n + cc
        uniq, inv = np.unique(key, return_inverse=True)
        self._n_entries = len(uniq)
        ur, uc = uniq // self.n, uniq % self.n
        self._indices = uc.astype(np.int32 if self.n < 2**31 else np.int64)
        self._indptr = np.concatenate([[0], np.cumsum(np.bincount(ur, minlength=self.n))])
        self._tie_pos = np.searchsorted(uniq, self._ties * self.n + self._ties)
        self._assembly(np.flatnonzero(ok), inv[:int(ok.sum())])
        # The free block's upper triangle, which is what gets factorized: its entries'
        # positions in the values, and its CSR structure (rows stay column-sorted).
        up = (ur <= uc) & (uc < self.n_free)
        self._up_sel = np.flatnonzero(up)
        self._up_indices = uc[up].astype(np.int32 if self.n < 2**31 else np.int64)
        self._up_indptr = np.concatenate(
            [[0], np.cumsum(np.bincount(ur[up], minlength=self.n_free))])
        self._factors = direct.ComplexSeries()
        self.stats.n_unknowns, self.stats.nnz = self.n, self._n_entries
        progress.info(f"{self.n} unknowns (faces meet at {int(no_d.sum())} of {n} nodes), "
                      f"{self._n_entries} non-zeros, {len(self._bar_g)} barrel segments, "
                      f"{len(self._cap_c)} capacitance couplings, {len(self.ports)} ports, "
                      f"{self.n_conductors} floating conductor(s)")

    def _assembly(self, pos, entry):
        """Matrices that assemble the system's values from the per-frequency coefficients.

        ``pos`` are the kept raw stamps (in the order the sparsity pattern was built:
        triangles' cc, cd, dc and dd blocks, barrels, capacitance) and ``entry`` the matrix
        entry each one lands in. The values at one frequency are then
        M_cc y_cc + M_cd y_cd + M_dd y_dd + M_cap y_c + (barrels and ties), where the y are
        one admittance per triangle or capacitance coupling.
        """
        n_tri = len(self._S)
        t9 = 9 * n_tri
        n_bar = len(self._bar_g)
        S = self._S.ravel()
        ne = self._n_entries

        def stamps(lo, hi, weight, column, n_cols):
            sel = (pos >= lo) & (pos < hi)
            q = pos[sel] - lo
            return sp.csr_matrix((weight(q), (entry[sel], column(q))), shape=(ne, n_cols))

        def tri_block(*blocks):
            return sum(stamps(b * t9, (b + 1) * t9, lambda q: S[q], lambda q: q // 9, n_tri)
                       for b in blocks)
        self._M_cc, self._M_cd, self._M_dd = tri_block(0), tri_block(1, 2), tri_block(3)
        base = 4 * t9 + 4 * n_bar
        coef = self._cap_coef.ravel()
        self._M_cap = stamps(base, base + len(coef), lambda q: coef[q], lambda q: q // 16,
                             len(self._cap_c))
        sign = np.repeat([1.0, 1.0, -1.0, -1.0], n_bar)
        bar = stamps(4 * t9, base, lambda q: sign[q] * self._bar_g[q % n_bar],
                     lambda q: np.zeros(len(q), dtype=np.int64), 1)
        self._const = bar.toarray().ravel()
        self._const[self._tie_pos] += TIE_ADMITTANCE_S
        del self._S                            # in the matrices now

    def _facing(self, tri_kind, area, node_kind, progress):
        """Per triangle and face: the external inductance height and, on rail faces that
        look at ground copper, the capacitance to the nearest ground node."""
        snap, mesh, st = self.snap, self.mesh, self.snap.stackup
        tris = mesh.tris
        layers = st.copper
        idx_of = {cu.id: i for i, cu in enumerate(layers)}
        cen = mesh.points[tris].mean(axis=1)
        tri_li = np.array([idx_of[l] for l in mesh.tri_layer])

        # Rail / ground copper per layer, for point-in-copper tests.
        def side_geom(nets):
            out = {}
            for cu in layers:
                parts = [net_layer_copper(snap, nt, cu.id, cut_drills=False,
                                          quad_segs=self.dc.circle_quad_segs) for nt in nets]
                parts = [g for g in parts if not g.is_empty]
                if parts:
                    g = shapely.union_all(parts)
                    shapely.prepare(g)
                    out[cu.id] = g
            return out
        geom = (side_geom(self.rail_nets), side_geom(self.gnd_nets))

        h_far = max(st.board_thickness_mm, 0.5)
        h_ext = np.full((len(tris), 2), h_far)
        cap_tri, cap_face, cap_layer, cap_c = [], [], [], []
        for li in np.unique(tri_li):
            sel = np.flatnonzero(tri_li == li)
            for face, step in ((0, -1), (1, +1)):          # top looks up, bottom down
                todo = sel
                j = li + step
                while len(todo) and 0 <= j < len(layers):
                    lj = layers[j].id
                    hit_r = (shapely.contains_xy(geom[0][lj], cen[todo, 0], cen[todo, 1])
                             if lj in geom[0] else np.zeros(len(todo), bool))
                    hit_g = (shapely.contains_xy(geom[1][lj], cen[todo, 0], cen[todo, 1])
                             if lj in geom[1] else np.zeros(len(todo), bool))
                    hit = hit_r | hit_g
                    if hit.any():
                        tri_here = todo[hit]
                        opposite = np.where(tri_kind[tri_here] == 0, hit_g[hit], hit_r[hit])
                        gap, er = self._gap(li, j)
                        h_ext[tri_here[opposite], face] = gap
                        rail_faces = tri_here[opposite & (tri_kind[tri_here] == 0)]
                        if len(rail_faces):
                            cap_tri.append(rail_faces)
                            cap_face.append(face)
                            cap_layer.append(lj)
                            cap_c.append(EPS0_F_PER_MM * er / gap * area[rail_faces])
                    todo = todo[~hit]
                    j += step
        # A face that sees no return while the sheet's other face does is shielded: the
        # field lives in the cavity on the other side, so it carries almost no current at
        # high frequency (on a plane pair, the outer faces). Splitting the current in
        # inverse proportion to a "board thickness" height there made the loop inductance
        # of a plane pair about 12% low.
        facing = h_ext < h_far
        one_sided = facing.any(axis=1) & ~facing.all(axis=1)
        h_ext[one_sided & ~facing[:, 0], 0] = SHIELDED_FACE_HEIGHT * h_far
        h_ext[one_sided & ~facing[:, 1], 1] = SHIELDED_FACE_HEIGHT * h_far
        self._h_ext = h_ext
        self._tri_li = tri_li
        self._rho_bins(cen, tri_li)

        # Capacitance: each rail face triangle's share goes to its three nodes, tied to
        # the nearest node of the ground copper it faces (that copper's facing face).
        gnd_trees: Dict[int, Tuple[cKDTree, np.ndarray]] = {}
        rn_all, gn_all, face_all, c_all = [], [], [], []
        for tri_sel, face, lj, cvals in zip(cap_tri, cap_face, cap_layer, cap_c):
            if lj not in gnd_trees:
                nodes = np.flatnonzero((node_kind == 1) & (mesh.node_layer == lj))
                gnd_trees[lj] = (cKDTree(mesh.points[nodes]), nodes)
            tree, gnodes = gnd_trees[lj]
            for k in range(3):
                rn = tris[tri_sel, k]
                _, nearest = tree.query(mesh.points[rn])
                rn_all.append(rn)
                gn_all.append(gnodes[nearest])
                face_all.append(np.full(len(rn), face))
                c_all.append(cvals / 3)
        if rn_all:
            rn, gn = np.concatenate(rn_all), np.concatenate(gn_all)
            fc, cv = np.concatenate(face_all), np.concatenate(c_all)
            key = (rn.astype(np.int64) * 2 + fc) * mesh.n_nodes + gn
            _, first, inv = np.unique(key, return_index=True, return_inverse=True)
            self._cap_rn, self._cap_gn, self._cap_face = rn[first], gn[first], fc[first]
            self._cap_c = np.bincount(inv, weights=cv)
        else:
            self._cap_rn = self._cap_gn = self._cap_face = np.zeros(0, dtype=np.int64)
            self._cap_c = np.zeros(0)
            self.warnings.append("No rail copper faces ground copper: the planes add no "
                                 "capacitance")
        self.total_plane_capacitance = float(self._cap_c.sum())
        progress.info(f"Dielectric εr, top to bottom: "
                      f"{dielectric_summary(self.snap.stackup, self.ac.default_er)}")
        progress.info(f"Rail-to-ground plane capacitance "
                      f"{self.total_plane_capacitance * 1e9:.3f} nF")

    def _gap(self, i: int, j: int) -> Tuple[float, float]:
        """Distance between the facing faces of copper layers i and j (stack indices) and
        the effective dielectric constant of what lies between."""
        st = self.snap.stackup
        a, b = (i, j) if i < j else (j, i)                 # a above b
        upper, lower = st.copper[a], st.copper[b]
        gap = max(upper.z_bottom_mm - lower.z_top_mm, 1e-4)
        ids_between = {cu.id for cu in st.copper[a:b]}
        diels = [d for d in st.dielectrics if d.above_layer in ids_between]
        t_sum = sum(d.thickness_mm for d in diels)
        dflt = self.ac.default_er
        if t_sum <= 0:
            return gap, dflt
        # Series combination; intermediate copper counts as the default dielectric.
        acc = sum(d.thickness_mm / (d.epsilon_r if d.epsilon_r > 0 else dflt) for d in diels)
        acc += max(gap - t_sum, 0.0) / dflt
        return gap, gap / acc

    def _rho_at(self, rho: float, tcr: float, xy, layers) -> float:
        """Resistivity ``rho`` (at the reference temperature) at copper point ``xy``, at
        the mean temperature of the copper layer ids ``layers`` there."""
        if self.temperature is None:
            return rho
        t = float(np.mean(temperature_at(self.temperature, np.tile(xy, (len(layers), 1)),
                                         np.asarray(layers))))
        return rho * (1.0 + tcr * (t - REF_TEMP_C))

    def _rho_bins(self, cen: np.ndarray, tri_li: np.ndarray):
        """Each triangle's copper resistivity at its temperature, binned: ``_bin_rho`` the
        resistivity and ``_bin_li`` the layer of each bin, ``_tri_bin`` each triangle's."""
        layers = self.snap.stackup.copper
        rho = np.array([self.dc.rho_for(cu.name) for cu in layers])
        if self.temperature is None:
            self._tri_bin, self._bin_li, self._bin_rho = tri_li, np.arange(len(layers)), rho
            return
        tcr = np.array([self.dc.tcr_for(cu.name) for cu in layers])
        ids = np.array([cu.id for cu in layers])
        temps = temperature_at(self.temperature, cen, ids[tri_li]).ravel()
        temps = np.where(np.isfinite(temps), temps, REF_TEMP_C)
        factor = np.maximum(1.0 + tcr[tri_li] * (temps - REF_TEMP_C), 0.05)
        q = np.round(np.log(factor) / RHO_STEP).astype(np.int64)
        keys, self._tri_bin = np.unique(np.stack([tri_li, q], axis=1), axis=0,
                                        return_inverse=True)
        self._tri_bin = self._tri_bin.ravel()
        self._bin_li = keys[:, 0]
        self._bin_rho = rho[keys[:, 0]] * np.exp(keys[:, 1] * RHO_STEP)
        self.stats.t_copper = (float(np.min(temps)), float(np.max(temps))) if len(temps) \
            else None

    # ============================================================== solve
    def matrix(self, omega: float):
        """The reduced system matrix (admittance form) at angular frequency ``omega``, and
        its cross-conductor part (capacitance and weak ties) on its own."""
        st = self.snap.stackup
        jw = 1j * omega
        nb = len(self._bin_rho)
        z11, zd = np.zeros(nb, complex), np.zeros(nb, complex)
        for i, (li, rho) in enumerate(zip(self._bin_li, self._bin_rho)):
            t_mm = st.copper[li].thickness_mm
            z11[i] = slab_impedance(float(rho), t_mm, omega)[0]
            zd[i] = slab_difference(float(rho), t_mm, omega)
        z11, zd = z11[self._tri_bin], zd[self._tri_bin]
        zs = 2 * z11 - zd                                  # Z11 + Z12
        a = jw * MU0_H_PER_MM * self._h_ext[:, 0] / 2      # top face, external
        b = jw * MU0_H_PER_MM * self._h_ext[:, 1] / 2      # bottom face
        det = zd * zs + (a + b) * z11 + a * b              # (Z11+a)(Z11+b) − Z12²
        y_cc = (2 * zd + a + b) / det
        y_cd = (b - a) / (2 * det)
        y_dd = (2 * zs + a + b) / (4 * det)
        yc = self._cap_c * omega * (self.ac.loss_tangent + 1j)
        vals = self._const.astype(complex)
        for M, y in ((self._M_cc, y_cc), (self._M_cd, y_cd), (self._M_dd, y_dd),
                     (self._M_cap, yc)):
            vals += M @ y.real                 # real matrices: no complex copy of them
            vals += 1j * (M @ y.imag)
        A = sp.csr_matrix((vals, self._indices, self._indptr), shape=(self.n, self.n))
        cap = (yc[:, None] * self._cap_coef).ravel()
        xr = np.concatenate([self._x_rows, self._ties])
        xc = np.concatenate([self._x_cols, self._ties])
        xd = np.concatenate([cap[self._x_sel], np.full(len(self._ties), TIE_ADMITTANCE_S)])
        X = sp.csr_matrix((xd, (xr, xc)), shape=(self.n, self.n))
        return A, X

    def port_impedance(self, omega: float, progress: ProgressReporter = NULL) -> np.ndarray:
        """The board's port impedance matrix (ports x ports, ohms) at ``omega``.

        The free unknowns (every floating conductor held at its anchor) are solved
        directly. With x_F = y_F − W x_A (y_F = A_FF⁻¹ b_F, W = A_FF⁻¹ A_FA), the anchor
        potentials x_A follow from KCL summed over each floating conductor, where the
        copper's own currents cancel exactly and only the capacitive ones remain:
        R X [x_A; x_F] = R b.
        """
        A, X = self.matrix(omega)
        nf = self.n_free
        upper = sp.csr_matrix((A.data[self._up_sel], self._up_indices, self._up_indptr),
                              shape=(nf, nf))
        factor = self._factors.factorize(upper, progress.warn)
        self.stats.method = factor.name
        AFA = A[:nf, nf:].tocsc()
        k = AFA.shape[1]                                    # floating conductors
        RX = (self._R @ X).tocsr()
        RX_A = RX[:, nf:].toarray()
        RX_F = RX[:, :nf]

        P = len(self.ports)
        plus = np.array([self.index[a] for a, _ in self.port_nodes])    # -1: the reference
        minus = np.array([self.index[b] for _, b in self.port_nodes])
        cols = np.arange(P)
        B = sp.csc_matrix(                                  # port currents (duplicates add)
            (np.concatenate([np.ones(int((plus >= 0).sum())),
                             -np.ones(int((minus >= 0).sum()))]).astype(complex),
             (np.concatenate([plus[plus >= 0], minus[minus >= 0]]),
              np.concatenate([cols[plus >= 0], cols[minus >= 0]]))), shape=(self.n, P))
        # Solve A_FF y = [A_FA, B_F] in blocks, keeping only what Z needs: y at the port
        # terminals and the floating conductors' capacitive currents R X_F y.
        rows = np.unique(np.concatenate([plus, minus]))
        rows = rows[(rows >= 0) & (rows < nf)]
        at = {int(r): i for i, r in enumerate(rows)}
        Y = np.zeros((len(rows), k + P), complex)
        RY = np.zeros((k, k + P), complex)
        block = direct.rhs_block(nf)
        for c0 in range(0, k + P, block):
            progress.check_cancel()
            cs = np.arange(c0, min(k + P, c0 + block))
            rhs = np.zeros((nf, len(cs)), complex, order="F")
            for j, c in enumerate(cs):
                col = AFA[:, [c]] if c < k else B[:nf, [c - k]]
                rhs[:, j] = col.toarray().ravel()
            y = factor(rhs)
            Y[:, cs] = y[rows]
            RY[:, cs] = RX_F @ y
        # x_F = y_F − W x_A, with W = A_FF⁻¹ A_FA (the first k columns).
        x_port = Y[:, k:]
        x_anchor = np.zeros((k, P), complex)
        if k:
            G = RX_A - RY[:, :k]                            # k x k
            x_anchor = np.linalg.solve(G, (self._R @ B).toarray() - RY[:, k:])
            x_port = x_port - Y[:, :k] @ x_anchor

        def potential(idx):
            out = np.zeros((P, P), complex)
            for i, u in enumerate(idx):
                if u >= nf:
                    out[i] = x_anchor[u - nf]
                elif u >= 0:
                    out[i] = x_port[at[int(u)]]
            return out
        Z = potential(plus) - potential(minus)
        return 0.5 * (Z + Z.T)              # symmetric up to round-off

    def close(self):
        """Release the solver's memory (the last factorization)."""
        self._factors.close()
