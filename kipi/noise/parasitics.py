"""A switcher phase's layout parasitics: its hot loop and its switch node (#69).

**Hot loop.** The commutating current of a buck flows from each input capacitor's
positive pad to the switching device's high-side pins, through the device to its
low-side (power ground) pins and back to the capacitor's ground pad; a boost's does the
same through its output capacitors. Each capacitor's loop is drawn as straight segments
between those pads (on the device's layer, with a via drop where the capacitor is on the
other side), each as wide as the net's copper along it, and its inductance comes from
partial inductances (:mod:`kipi.noise.extract.peec`), with the nearest solid plane under
the device taken as an image plane. The phase's loop inductance is the device's package
inductance plus the capacitors' branches (their ESL plus their loop) in parallel.

**Switch node.** Its capacitance to ground is the device's (Coss, from the datasheet, or a
typical value) plus that of the switch-node copper to the planes on the neighbouring
layers: ε0·εr·A/h over their overlap, the copper grown by h/2 for its fringing field.

The two set the ringing on the switch node's edges: f_ring = 1 / (2π·√(L·C)) with
damping ζ = R/2 · √(C/L), R the conducting switch's on-resistance, the capacitors' ESR and
the copper's resistance at f_ring (skin effect included).

These are estimates from the pads' positions, not a field solve of the loop's copper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kipi.ac.model import caps_dict
from kipi.ac.solver.physics import EPS0_F_PER_MM, skin_depth_mm
from kipi.core.model.board import BoardSnapshot, Pad
from kipi.core.model.parts import parse_capacitance
from kipi.core.model.stackup import combined_epsilon_r
from kipi.dc.pi.model import PinRef, PISetup
from kipi.noise.extract import peec
from kipi.noise.model import DEFAULT_COSS_F, DEFAULT_RDS_ON_OHM, AggressorPhase
from kipi.noise.waveforms import CapBranch

RHO_CU_OHM_MM = 1.724e-5                # annealed copper, 20 °C
VIA_MM = 0.3                            # a via drop's diameter
DEVICE_WIDTH_MM = 1.0                   # the current path through the device's pins
MIN_WIDTH_MM = 0.1
MAX_WIDTH_MM = 5.0
# A capacitor with no model: its value from the board, else this, with typical parasitics.
FALLBACK_C_F = 10e-6
FALLBACK_ESR_OHM = 0.005
FALLBACK_ESL_H = 0.5e-9


@dataclass
class HotLoopReport:
    """What the waveforms take from the layout, for one phase; printed with the results."""
    phase: int
    loop_l_h: float                 # package + capacitor branches in parallel
    package_l_h: float
    cap_loops_h: Dict[str, float]   # each hot-loop capacitor's board loop, without its ESL
    c_sw_f: float                   # Coss + copper
    coss_f: float
    coss_default: bool
    c_copper_f: float
    r_loop_ohm: float
    f_ring_hz: Optional[float]
    zeta: float
    image_plane: str = ""           # the layer whose plane takes the image currents
    notes: List[str] = field(default_factory=list)
    # The damping the loop's resistance alone gives; ``zeta`` is at least the settings'
    # least damping (NoiseSettings.min_ring_zeta).
    zeta_loop: float = 1.0


@dataclass
class PhaseLayout:
    report: HotLoopReport
    input_caps: List[CapBranch]
    output_caps: List[CapBranch]
    # Each capacitor's current loop (it carries that capacitor's ripple current, from its
    # positive pad around the loop), and the plane under the device taking image
    # currents: the z of its facing surface and its layer id (None without one).
    loops: Dict[str, List[peec.Segment]] = field(default_factory=dict)
    image_z: Optional[float] = None
    image_layer: Optional[int] = None


class _Board:
    """Lookups on the snapshot, cached for one solve."""

    def __init__(self, snap: BoardSnapshot, default_er: float):
        self.snap = snap
        self.stack = snap.stackup
        self.default_er = default_er
        self.by_ref: Dict[str, List[Pad]] = {}
        for p in snap.pads:
            if p.footprint:
                self.by_ref.setdefault(p.footprint, []).append(p)
        self.side = {f.reference: f.side for f in snap.footprints}
        self.value = {f.reference: f.value for f in snap.footprints}
        self._copper: Dict[Tuple[str, int], object] = {}
        self._planes: Dict[Tuple[int, str], object] = {}
        self.order = [c.id for c in self.stack.copper]

    # -- geometry
    def copper(self, net: str, layer: int):
        key = (net, layer)
        if key not in self._copper:
            from kipi.core.geometry.copper import net_layer_copper
            self._copper[key] = net_layer_copper(self.snap, net, layer, cut_drills=False)
        return self._copper[key]

    def planes(self, layer: int, not_net: str = ""):
        """Zone copper on ``layer`` of every net but ``not_net``."""
        key = (layer, not_net)
        if key not in self._planes:
            import shapely
            from kipi.core.geometry.copper import poly_to_shapely
            polys = [poly_to_shapely(p) for z in self.snap.zones if z.net != not_net
                     for p in z.filled.get(layer, [])]
            polys = [p for p in polys if p is not None and not p.is_empty]
            self._planes[key] = shapely.union_all(polys) if polys else None
        return self._planes[key]

    def z(self, layer: int) -> float:
        lay = self.stack.layer(layer)
        return lay.z_mid_mm if lay else 0.0

    def thickness(self, layer: int) -> float:
        lay = self.stack.layer(layer)
        return lay.thickness_mm if lay else 0.035

    def outer(self, ref: str) -> int:
        """The outer copper layer a part sits on."""
        top = self.side.get(ref, "top") == "top"
        return self.order[0] if top else self.order[-1]

    def pads(self, ref: str, net: Optional[str] = None, pins=()) -> List[Pad]:
        return [p for p in self.by_ref.get(ref, [])
                if (net is None or p.net == net) and (not pins or p.number in pins)]

    def gap_er(self, upper: int, lower: int) -> Tuple[float, float]:
        """Dielectric thickness (mm) and εr between two copper layers, upper above."""
        a, b = self.stack.layer(upper), self.stack.layer(lower)
        gap = max(a.z_bottom_mm - b.z_top_mm, 1e-3)
        ia, ib = sorted((self.order.index(upper), self.order.index(lower)))
        between = set(self.order[ia:ib + 1])
        subs = [(d.thickness_mm, d.epsilon_r) for d in self.stack.dielectrics
                if d.above_layer in between and d.below_layer in between]
        return gap, combined_epsilon_r(subs) or self.default_er

    def width(self, net: str, layer: int, a, b) -> float:
        """The net's copper width along the straight line a→b on ``layer``: the area of its
        copper within a corridor around the line, over the line's length."""
        from shapely.geometry import LineString
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        cu = self.copper(net, layer) if net else None
        if length < 1e-6 or cu is None or cu.is_empty:
            return MIN_WIDTH_MM
        corridor = LineString([a[:2], b[:2]]).buffer(MAX_WIDTH_MM / 2, cap_style="flat")
        w = cu.intersection(corridor).area / length
        return float(min(max(w, MIN_WIDTH_MM), MAX_WIDTH_MM))

    def coverage(self, net: str, layer: int, a, b) -> float:
        """The share of the straight line a→b that runs over ``net``'s copper on
        ``layer``."""
        from shapely.geometry import LineString
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        cu = self.copper(net, layer) if net else None
        if length < 1e-6 or cu is None or cu.is_empty:
            return 0.0 if length >= 1e-6 else 1.0
        return float(LineString([a[:2], b[:2]]).intersection(cu).length / length)

    def plane_under(self, layer: int, xy) -> Tuple[Optional[float], str, Optional[int]]:
        """The facing surface's z, name and id of the nearest solid plane inward of
        ``layer`` at ``xy``."""
        from shapely.geometry import Point
        i = self.order.index(layer)
        step = 1 if i < len(self.order) / 2 else -1
        j = i + step
        while 0 <= j < len(self.order):
            lid = self.order[j]
            g = self.planes(lid)
            if g is not None and g.contains(Point(xy[0], xy[1])):
                lay = self.stack.layer(lid)
                return (lay.z_top_mm if step > 0 else lay.z_bottom_mm), lay.name, lid
            j += step
        return None, "", None


def _centroid(pads: List[Pad]) -> Optional[Tuple[float, float]]:
    if not pads:
        return None
    return (sum(p.position[0] for p in pads) / len(pads),
            sum(p.position[1] for p in pads) / len(pads))


def _pin_point(board: _Board, pin: Optional[PinRef]):
    if pin is None:
        return None
    return _centroid(board.pads(pin.ref, pin.net, pin.pins))


def _pad_short_side(pads: List[Pad]) -> float:
    """The smallest pad's shorter side: a part body's current path width."""
    best = None
    for p in pads:
        for polys in p.shapes.values():
            for poly in polys:
                xs = [q[0] for q in poly.outline]
                ys = [q[1] for q in poly.outline]
                s = min(max(xs) - min(xs), max(ys) - min(ys))
                best = s if best is None else min(best, s)
    return float(best) if best else 0.5


def _cap_branch(board: _Board, setup: PISetup, ref: str, volts: float,
                notes: List[str]) -> CapBranch:
    model = caps_dict(setup.caps).get(ref)
    if model and model.capacitance_f > 0:
        c = model.capacitance_at(volts if setup.ac.dc_bias else 0.0)
        return CapBranch(ref, c, model.esr_ohm, model.esl_h)
    c = parse_capacitance(board.value.get(ref, "")) or FALLBACK_C_F
    notes.append(f"{ref} has no capacitor model: taken as {c * 1e6:.3g} µF with "
                 f"{FALLBACK_ESR_OHM * 1e3:g} mΩ ESR and {FALLBACK_ESL_H * 1e9:g} nH ESL")
    return CapBranch(ref, c, FALLBACK_ESR_OHM, FALLBACK_ESL_H)


def _cap_loop(board: _Board, ref: str, plus_net: str, gnd_net: str, hops, dev_layer: int,
              plane_z: Optional[float] = None) -> Tuple[List[peec.Segment], float]:
    """The loop from capacitor ``ref``'s positive pad through ``hops`` to its ground pad
    and back across the capacitor, on ``dev_layer`` (with via drops at the capacitor when
    it is on the other side). Each hop is (point, net, width, label) for the segment
    arriving at the point: as wide as ``net``'s copper along it, or ``width`` when no net
    is given. A ground hop with no ground copper under most of it on ``dev_layer`` returns
    through the plane at ``plane_z`` instead, with via drops at its ends: the return current
    flows in the plane under the loop. Returns the segments and their DC resistance."""
    plus = _centroid(board.pads(ref, plus_net))
    minus = _centroid(board.pads(ref, gnd_net))
    if plus is None or minus is None or any(h[0] is None for h in hops):
        return [], 0.0
    cap_layer = board.outer(ref)
    zc, zd = board.z(cap_layer), board.z(dev_layer)
    t = board.thickness(dev_layer)
    body = _pad_short_side(board.pads(ref))
    segs: List[peec.Segment] = []
    r = 0.0

    def go(a, b, w, label, resist=True):
        nonlocal r
        if math.dist(a, b) > 1e-9:
            segs.append(peec.segment(a, b, w, t, label))
            if resist:
                r += RHO_CU_OHM_MM * math.dist(a, b) / (w * t)

    here = (*plus, zc)
    if cap_layer != dev_layer:
        go(here, (*plus, zd), VIA_MM, f"{ref}+ via")
        here = (*plus, zd)
    path = [*hops, (minus, gnd_net, None, f"{gnd_net} to {ref}-")]
    for pt, net, w, label in path:
        prev = here[:2]
        in_plane = (plane_z is not None and net == gnd_net
                    and board.coverage(net, dev_layer, prev, pt) < 0.5)
        if in_plane:
            go(here, (*prev, plane_z), VIA_MM, f"{label} via")
            go((*prev, plane_z), (*pt, plane_z), MAX_WIDTH_MM, f"{label} (plane)")
            go((*pt, plane_z), (*pt, zd), VIA_MM, f"{label} via")
        else:
            go(here, (*pt, zd), board.width(net, dev_layer, prev, pt) if net else w, label)
        here = (*pt, zd)
    if cap_layer != dev_layer:
        go(here, (*minus, zc), VIA_MM, f"{ref}- via")
        here = (*minus, zc)
    go(here, (*plus, zc), body, f"{ref} body", resist=False)
    return segs, r


def switch_node_copper_f(board: _Board, net: str) -> float:
    """Capacitance of the switch node's copper to the planes on the neighbouring layers."""
    total = 0.0
    if not net:
        return 0.0
    for i, lid in enumerate(board.order):
        cu = board.copper(net, lid)
        if cu is None or cu.is_empty:
            continue
        for j in (i - 1, i + 1):
            if not 0 <= j < len(board.order):
                continue
            other = board.order[j]
            planes = board.planes(other, not_net=net)
            if planes is None:
                continue
            upper, lower = (other, lid) if j < i else (lid, other)
            h, er = board.gap_er(upper, lower)
            overlap = cu.buffer(h / 2).intersection(planes).area
            total += EPS0_F_PER_MM * er * overlap / h
    return total


def phase_layout(snap: BoardSnapshot, setup: PISetup, phase: AggressorPhase, index: int,
                 topology: str, vin: float, vout: float, vin_net: str, vout_net: str,
                 gnd_net: str, min_zeta: float = 0.0) -> PhaseLayout:
    """The hot-loop report and the capacitor branches (with their loops) of one phase."""
    board = _Board(snap, setup.ac.default_er)
    notes: List[str] = []
    hs, ls = phase.high_side, phase.low_side
    a_pt, b_pt = _pin_point(board, hs), _pin_point(board, ls)
    dev_ref = (hs or ls).ref if (hs or ls) else ""
    dev_layer = board.outer(dev_ref) if dev_ref else board.order[0]
    gnd = ls.net if ls else gnd_net
    if a_pt is None or b_pt is None:
        notes.append("The switching device's high-side or low-side pins are not set or "
                     "not on the board: the hot loop is its package inductance only")
    # Inductor pads: the one on the switch node and the one on the rail it feeds.
    ind_pads = board.pads(phase.inductor) if phase.inductor else []
    l_sw = _centroid([p for p in ind_pads if p.net == phase.switch_net])
    rail_net = vout_net if topology == "buck" else vin_net
    l_rail = _centroid([p for p in ind_pads if p.net == rail_net])

    z_plane, plane_name, plane_id = (None, "", None)
    if a_pt is not None and b_pt is not None:
        mid = ((a_pt[0] + b_pt[0]) / 2, (a_pt[1] + b_pt[1]) / 2)
        z_plane, plane_name, plane_id = board.plane_under(dev_layer, mid)
    cap_loops: Dict[str, List[peec.Segment]] = {}

    def branches(refs: List[str], hot: bool, volts: float, plus_net: str):
        out, loops = [], {}
        for ref in refs:
            br = _cap_branch(board, setup, ref, volts, notes)
            if hot:
                hops = [(a_pt, plus_net, None, f"{ref}+ to {dev_ref}"),
                        (b_pt, None, DEVICE_WIDTH_MM, f"through {dev_ref}")]
            else:
                ind_w = _pad_short_side(ind_pads) if ind_pads else DEVICE_WIDTH_MM
                hops = [(l_rail, plus_net, None, f"{ref}+ to {phase.inductor}"),
                        (l_sw, None, ind_w, f"through {phase.inductor}"),
                        (b_pt, phase.switch_net, None, f"switch node to {dev_ref}")]
            segs, r = _cap_loop(board, ref, plus_net, gnd, hops, dev_layer, z_plane)
            if segs:
                cap_loops[ref] = segs
                br.path_l_h = max(peec.loop_inductance(segs, z_plane), 0.0)
                br.path_r_ohm = r
            loops[ref] = br.path_l_h
            out.append(br)
        return out, loops

    hot_in = topology == "buck"
    in_caps, in_loops = branches(phase.input_caps, hot_in, vin, vin_net)
    out_caps, out_loops = branches(phase.output_caps, not hot_in, vout, vout_net)
    hot_caps, hot_loops = (in_caps, in_loops) if hot_in else (out_caps, out_loops)

    pkg = phase.package_l_h
    if hot_caps:
        y = sum(1.0 / max(c.path_l_h + c.esl_h, 1e-15) for c in hot_caps)
        loop_l = pkg + 1.0 / y
        esr = 1.0 / sum(1.0 / max(c.esr_ohm, 1e-6) for c in hot_caps)
    else:
        loop_l, esr = pkg, 0.0
        side = "input" if hot_in else "output"
        notes.append(f"No {side} capacitors are set: the hot loop is its package "
                     "inductance only")
    coss_default = phase.coss_f is None
    coss = DEFAULT_COSS_F if coss_default else float(phase.coss_f)
    if coss_default:
        notes.append(f"No Coss entered: {coss * 1e12:g} pF assumed; the ringing is "
                     "approximate")
    c_cu = switch_node_copper_f(board, phase.switch_net)
    c_sw = coss + c_cu
    rds = phase.rds_on_hs_ohm if phase.rds_on_hs_ohm is not None else DEFAULT_RDS_ON_OHM
    f_ring = None
    zeta = zeta_loop = 1.0
    r_loop = rds + esr
    if loop_l > 0 and c_sw > 0:
        f_ring = 1.0 / (2 * math.pi * math.sqrt(loop_l * c_sw))
        # Copper at f_ring: current in a skin depth on each face of the foil.
        delta = float(skin_depth_mm(RHO_CU_OHM_MM, 2 * math.pi * f_ring))
        cu_r = 0.0
        if hot_caps:
            per_cap = [c.path_r_ohm for c in hot_caps]
            dc_r = 1.0 / sum(1.0 / max(r, 1e-9) for r in per_cap)
            t = board.thickness(dev_layer)
            cu_r = dc_r * max(t / min(t, 2 * delta), 1.0)
        r_loop = rds + esr + cu_r
        zeta_loop = 0.5 * r_loop * math.sqrt(c_sw / loop_l)
        zeta = max(zeta_loop, min_zeta)
    elif loop_l <= 0:
        notes.append("No hot-loop inductance: the switch node's edges do not ring")
    report = HotLoopReport(index, loop_l, pkg, hot_loops, c_sw, coss, coss_default, c_cu,
                           r_loop, f_ring, zeta, plane_name, notes, zeta_loop)
    return PhaseLayout(report, in_caps, out_caps, cap_loops, z_plane, plane_id)
