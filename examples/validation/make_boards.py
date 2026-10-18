"""Build the validation boards: small layouts whose results have closed-form answers.

    python examples/validation/make_boards.py

writes ``<name>/<name>.kipi.json.gz`` and ``<name>/<name>.kipi-setup.yml`` for every board
below. ``tests/accuracy`` solves each one and compares the results with the engineering
formulas they were designed around, so a change that moves the solvers' answers fails CI.
Open any of them without KiCad:

    python -m kipi --snapshot examples/validation/planes/planes.kipi.json.gz

The dimensions live in this module (the classes below), so the tests and the boards
always agree on them.

DC (copper resistivity ρ = 1.724e-5 Ω·mm, 1 oz = 35 µm, ½ oz = 17.5 µm):

* skinny_traces: thin tracks on an outer and an inner layer, and an arc track:
  R = ρL/(wt), and ρθ/(t·ln(r_o/r_i)) for the arc.
* right_angle: an L route and a Z route (two bends) drawn as copper polygons. Each square
  corner counts as 0.5587 squares (conformal mapping).
* layer_change: a 10 mm wide strip on F.Cu drops through a row of five vias onto In2.Cu:
  the strips, five barrels in parallel and the current crowding into the row of vias.
* planes: a disc-shaped plane on In1.Cu fed and loaded at two small pads (method of
  images), and one strip on F.Cu and B.Cu in parallel, B.Cu twice as thick.
* power_chain: a 12 V source, a switcher, a ferrite (series link) and a remote-sensed LDO
  on 2 mm strips over a ground strip: every voltage and current by hand.

AC (εr 4.5, 0.2 mm between the rail and ground planes):

* ac_plane: a 100 mm square plane pair: C = ε₀εrA/h, and the first cavity resonance at
  c/(2a√εr).
* ac_decap: the plane pair with one capacitor beside the probe: its ESR at the series
  resonance, and the anti-resonance of the mounted inductance with the plane capacitance.
* ac_ferrite: a filtered rail behind a ferrite (R + L series link): the probe sees the
  ferrite in series with the bulk capacitor.
"""

from __future__ import annotations

import sys
from pathlib import Path

from shapely.geometry import MultiPolygon, Point, box

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from kipi.ac.model import CapModel, Probe  # noqa: E402
from kipi.core.model.snapshot_io import save  # noqa: E402
from kipi.core.model.board import (  # noqa: E402
    BoardSnapshot, CopperLayer, DielectricLayer, Footprint, Net, Pad, Poly, Stackup, Track, Via,
    Zone,
)
from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm  # noqa: E402
from kipi.dc.solver.setup import SolveSettings  # noqa: E402

F, IN1, IN2, B = 3, 4, 5, 34
OZ1, OZ_HALF, OZ2 = 0.035, 0.0175, 0.070
ER = 4.5


# ---------------------------------------------------------------- stackups and parts

def stackup(copper=(OZ1, OZ_HALF, OZ_HALF, OZ1), diel=(0.2, 1.0, 0.2)) -> Stackup:
    """Four copper layers (or two, with one dielectric), FR4 εr 4.5."""
    ids = [F, IN1, IN2, B] if len(copper) == 4 else [F, B]
    names = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"] if len(copper) == 4 else ["F.Cu", "B.Cu"]
    total = sum(copper) + sum(diel)
    z, cu, ds = total, [], []
    for i, (lid, name, t) in enumerate(zip(ids, names, copper)):
        z -= t
        cu.append(CopperLayer(lid, name, i, t, z))
        if i < len(diel):
            z -= diel[i]
            ds.append(DielectricLayer(diel[i], "FR4", ER, lid, ids[i + 1]))
    return Stackup(cu, ds, total)


def polys(geom) -> list:
    geoms = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    return [Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])
            for g in geoms if not g.is_empty]


def disc(x, y, r):
    return Point(x, y).buffer(r, quad_segs=16)


class Board:
    """Collects pads, footprints, vias, tracks and zones for one snapshot."""

    def __init__(self, name: str, st: Stackup):
        self.name, self.stackup = name, st
        self.pads, self.vias, self.tracks, self.zones = [], [], [], []
        self.footprints, self.nets = {}, []

    def _net(self, net):
        if net not in self.nets:
            self.nets.append(net)

    def pad(self, ref, number, net, geom, layers=(F,), drill=0.0, value=""):
        pid = f"{ref}_{number}".lower()
        c = geom.centroid
        self.pads.append(Pad(pid, net, ref, number, (c.x, c.y), "pth" if drill else "smd",
                             (drill, drill), {lid: polys(geom) for lid in layers}))
        fp = self.footprints.setdefault(ref, Footprint(f"fp_{ref}", ref, value, (c.x, c.y),
                                                       "top"))
        fp.value = fp.value or value
        fp.pad_ids.append(pid)
        self._net(net)

    def via(self, net, x, y, drill=0.3, dia=0.6, layers=(F, IN1, IN2, B)):
        self.vias.append(Via(f"v{len(self.vias)}", net, (x, y), drill, dia, list(layers)))
        self._net(net)

    def track(self, net, layer, width, start, end, mid=None):
        self.tracks.append(Track(net, layer, width, start, end, mid))
        self._net(net)

    def zone(self, net, layer, geom):
        self.zones.append(Zone(f"z{len(self.zones)}_{net}", net, "", 0, {layer: polys(geom)}))
        self._net(net)

    def snapshot(self) -> BoardSnapshot:
        x0, y0, x1, y1 = self._extent()
        nets = [Net(0, "")] + [Net(i, n) for i, n in enumerate(self.nets, start=1)]
        return BoardSnapshot(f"{self.name}.kicad_pcb", "", self.stackup, nets, self.tracks,
                             self.vias, self.pads, list(self.footprints.values()), self.zones,
                             [Poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])],
                             source=f"examples/validation/{self.name}")

    def _extent(self):
        xs, ys = [], []
        for p in self.pads:
            for ps in p.shapes.values():
                for q in ps:
                    xs += [v[0] for v in q.outline]
                    ys += [v[1] for v in q.outline]
        for z in self.zones:
            for ps in z.filled.values():
                for q in ps:
                    xs += [v[0] for v in q.outline]
                    ys += [v[1] for v in q.outline]
        for t in self.tracks:
            for pt in (t.start, t.end, t.mid) if t.mid else (t.start, t.end):
                xs.append(pt[0])
                ys.append(pt[1])
        return min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2


def source(name, ref, net, pins, volts=1.0, ground=None):
    return Vrm(name, PinRef(ref, net, pins), volts, "source", ground=ground)


def load(name, net, pins, amps=1.0, ground=None, tol=None):
    return Load(name, PinRef(name, net, pins), amps, ground=ground,
                tol_min=None if tol is None else -tol, tol_max=tol)


# ---------------------------------------------------------------- DC boards

class Traces:
    """Straight tracks between square pads, and a semicircular arc track."""
    PAD = 1.5                        # square pad side
    X0, X1 = 2.0, 42.0               # pad centres
    W_F, W_IN = 0.15, 0.10           # track widths: F.Cu (1 oz), In1.Cu (½ oz)
    Y_F, Y_IN = 5.0, 15.0
    ARC_C, ARC_R, W_ARC = (22.0, 25.0), 12.0, 0.2     # arc centre, radius, width (F.Cu)
    AMPS = 1.0


def skinny_traces():
    g, b = Traces, Board("skinny_traces", stackup())
    h = g.PAD / 2
    # Outer layer: SMD pads on F.Cu.
    b.pad("J1", "1", "TRACE_F", box(g.X0 - h, g.Y_F - h, g.X0 + h, g.Y_F + h))
    b.pad("U1", "1", "TRACE_F", box(g.X1 - h, g.Y_F - h, g.X1 + h, g.Y_F + h))
    b.track("TRACE_F", F, g.W_F, (g.X0, g.Y_F), (g.X1, g.Y_F))
    # Inner layer: through-hole pads reach In1.Cu.
    for ref, x in (("J2", g.X0), ("U2", g.X1)):
        b.pad(ref, "1", "TRACE_IN", box(x - h, g.Y_IN - h, x + h, g.Y_IN + h),
              layers=(F, IN1, IN2, B), drill=0.6)
    b.track("TRACE_IN", IN1, g.W_IN, (g.X0, g.Y_IN), (g.X1, g.Y_IN))
    # Arc: the top half of a circle, a pad on each end.
    cx, cy = g.ARC_C
    r = g.ARC_R
    b.pad("J3", "1", "TRACE_ARC", box(cx - r - h, cy - h, cx - r + h, cy + h))
    b.pad("U3", "1", "TRACE_ARC", box(cx + r - h, cy - h, cx + r + h, cy + h))
    b.track("TRACE_ARC", F, g.W_ARC, (cx - r, cy), (cx + r, cy), mid=(cx, cy + r))

    vrms, loads = [], []
    for k, net in enumerate(("TRACE_F", "TRACE_IN", "TRACE_ARC"), start=1):
        vrms.append(source(f"J{k}", f"J{k}", net, ["1"]))
        loads.append(load(f"U{k}", net, ["1"], g.AMPS))
    return b.snapshot(), PISetup(vrms, loads, [], SolveSettings(max_edge_mm=0.5))


class RightAngle:
    """Copper polygons W wide: an L (one bend) and a Z (two bends), pads P long at the ends.
    Leg lengths are measured to the outer edge of the bend."""
    W, P = 2.0, 1.0
    L_A, L_B = 20.0, 20.0             # L: horizontal leg, then vertical leg
    Z_A, Z_B, Z_C = 15.0, 12.0, 15.0  # Z: right, up, right
    Y_Z = 30.0                        # the Z starts at this y
    AMPS = 1.0


def right_angle():
    g, b = RightAngle, Board("right_angle", stackup((OZ1, OZ1), (1.5,)))
    w, p = g.W, g.P
    # L: (0, 0) -> (L_A, 0) -> up to L_B.
    lshape = box(0, 0, g.L_A, w).union(box(g.L_A - w, 0, g.L_A, g.L_B))
    b.zone("L_ROUTE", F, lshape)
    b.pad("J1", "1", "L_ROUTE", box(0, 0, p, w))
    b.pad("U1", "1", "L_ROUTE", box(g.L_A - w, g.L_B - p, g.L_A, g.L_B))
    # Z: right along y = Y_Z, up Z_B, right again.
    y, xb = g.Y_Z, g.Z_A
    zshape = (box(0, y, xb, y + w)
              .union(box(xb - w, y, xb, y + g.Z_B))
              .union(box(xb - w, y + g.Z_B - w, xb - w + g.Z_C, y + g.Z_B)))
    b.zone("Z_ROUTE", F, zshape)
    x_end = xb - w + g.Z_C
    b.pad("J2", "1", "Z_ROUTE", box(0, y, p, y + w))
    b.pad("U2", "1", "Z_ROUTE", box(x_end - p, y + g.Z_B - w, x_end, y + g.Z_B))
    vrms = [source("J1", "J1", "L_ROUTE", ["1"]), source("J2", "J2", "Z_ROUTE", ["1"])]
    loads = [load("U1", "L_ROUTE", ["1"], g.AMPS), load("U2", "Z_ROUTE", ["1"], g.AMPS)]
    return b.snapshot(), PISetup(vrms, loads, [], SolveSettings(max_edge_mm=0.1))


class LayerChange:
    """A W wide strip on F.Cu (x 0..X_VIA + E) overlapping one on In2.Cu (x X_VIA - E..L),
    joined by N through vias at x = X_VIA, PITCH apart across the width."""
    W, L, P = 10.0, 40.0, 1.0
    X_VIA, E = 20.0, 3.0
    N, DRILL, DIA = 5, 0.3, 0.6
    AMPS = 5.0

    @classmethod
    def pitch(cls):
        return cls.W / cls.N


def layer_change():
    g, b = LayerChange, Board("layer_change", stackup())
    b.zone("XFER", F, box(0, 0, g.X_VIA + g.E, g.W))
    b.zone("XFER", IN2, box(g.X_VIA - g.E, 0, g.L, g.W))
    for k in range(g.N):
        b.via("XFER", g.X_VIA, (k + 0.5) * g.pitch(), g.DRILL, g.DIA)
    b.pad("J1", "1", "XFER", box(0, 0, g.P, g.W))
    b.pad("U1", "1", "XFER", box(g.L - g.P, 0, g.L, g.W), layers=(IN2,))
    setup = PISetup([source("J1", "J1", "XFER", ["1"])], [load("U1", "XFER", ["1"], g.AMPS)],
                    [], SolveSettings(max_edge_mm=0.3, circle_quad_segs=8))
    return b.snapshot(), setup


class Planes:
    """A disc-shaped plane of radius R0 on In1.Cu (½ oz), fed and loaded at round pads of
    radius A, D either side of its centre; and a strip on both F.Cu (1 oz) and B.Cu (2 oz),
    through-hole pads across its full width at both ends."""
    C, R0, D, A = (30.0, 30.0), 25.0, 10.0, 0.75
    STRIP_Y, W, L, P = 60.0, 10.0, 40.0, 1.0
    AMPS = 2.0


def planes():
    g = Planes
    b = Board("planes", stackup((OZ1, OZ_HALF, OZ_HALF, OZ2)))
    cx, cy = g.C
    b.zone("PLANE", IN1, Point(cx, cy).buffer(g.R0, quad_segs=64))
    b.pad("J1", "1", "PLANE", disc(cx - g.D, cy, g.A), layers=(IN1,))
    b.pad("U1", "1", "PLANE", disc(cx + g.D, cy, g.A), layers=(IN1,))
    y0, y1 = g.STRIP_Y, g.STRIP_Y + g.W
    for layer in (F, B):
        b.zone("PAIR", layer, box(0, y0, g.L, y1))
    b.pad("J2", "1", "PAIR", box(0, y0, g.P, y1), layers=(F, B), drill=0.5)
    b.pad("U2", "1", "PAIR", box(g.L - g.P, y0, g.L, y1), layers=(F, B), drill=0.5)
    vrms = [source("J1", "J1", "PLANE", ["1"]), source("J2", "J2", "PAIR", ["1"])]
    loads = [load("U1", "PLANE", ["1"], g.AMPS), load("U2", "PAIR", ["1"], g.AMPS)]
    return b.snapshot(), PISetup(vrms, loads, [], SolveSettings(max_edge_mm=0.25))


class PowerChain:
    """2 mm wide strips on F.Cu over a 2 mm ground strip on B.Cu, 1 oz, pads 1 mm long.
    Pad spans along x, F.Cu: J1.1 VIN, U1.1 VIN | U1.2 +3V3, U4.1, FB1.1 | FB1.2 +3V3_F,
    U2.1 | U2.2 +1V8, U3.1. B.Cu (GND): J1.2, U1.3, U4.2, U2.3, U3.2."""
    W = 2.0
    VIN = {"J1.1": 0.0, "U1.1": 29.0}
    V3V3 = {"U1.2": 32.0, "U4.1": 46.0, "FB1.1": 61.0}
    V3V3_F = {"FB1.2": 64.0, "U2.1": 79.0}
    V1V8 = {"U2.2": 82.0, "U3.1": 97.0}
    GND = {"J1.2": 0.0, "U1.3": 31.0, "U4.2": 46.0, "U2.3": 80.5, "U3.2": 97.0}
    V_SRC, V_SMPS, V_LDO = 12.0, 3.3, 1.8
    ETA, IQ_SMPS, IQ_LDO = 0.9, 0.005, 0.002
    R_FB = 0.05
    I_U3, I_U4 = 2.0, 0.5


def power_chain():
    g, b = PowerChain, Board("power_chain", stackup((OZ1, OZ1), (1.5,)))
    nets = {"VIN": g.VIN, "+3V3": g.V3V3, "+3V3_F": g.V3V3_F, "+1V8": g.V1V8}
    for net, pins in nets.items():
        xs = list(pins.values())
        b.zone(net, F, box(min(xs), 0, max(xs) + 1, g.W))
        for pin, x in pins.items():
            ref, num = pin.split(".")
            b.pad(ref, num, net, box(x, 0, x + 1, g.W))
    b.zone("GND", B, box(0, 0, max(g.GND.values()) + 1, g.W))
    for pin, x in g.GND.items():
        ref, num = pin.split(".")
        b.pad(ref, num, "GND", box(x, 0, x + 1, g.W), layers=(B,))

    gnd = lambda ref, pin: PinRef(ref, "GND", [pin])   # noqa: E731
    j1 = source("J1", "J1", "VIN", ["1"], g.V_SRC, ground=gnd("J1", "2"))
    u1 = Vrm("U1", PinRef("U1", "+3V3", ["2"]), g.V_SMPS, "smps", efficiency=g.ETA,
             quiescent_a=g.IQ_SMPS, input=PinRef("U1", "VIN", ["1"]), ground=gnd("U1", "3"))
    u2 = Vrm("U2", PinRef("U2", "+1V8", ["2"]), g.V_LDO, "ldo", quiescent_a=g.IQ_LDO,
             input=PinRef("U2", "+3V3_F", ["1"]), ground=gnd("U2", "3"),
             sense=PinRef("U3", "+1V8", ["1"]), ground_sense=gnd("U3", "2"))
    loads = [load("U3", "+1V8", ["1"], g.I_U3, ground=gnd("U3", "2"), tol=1.0),
             load("U4", "+3V3", ["1"], g.I_U4, ground=gnd("U4", "2"), tol=3.0)]
    fb1 = DcLink("FB1", PinRef("FB1", "+3V3", ["1"]), PinRef("FB1", "+3V3_F", ["2"]), g.R_FB)
    return b.snapshot(), PISetup([j1, u1, u2], loads, [fb1], SolveSettings(max_edge_mm=0.5))


# ---------------------------------------------------------------- AC boards

class AcPlane:
    """A rail plane on F.Cu over a GND plane on In1.Cu, 0.2 mm apart. The probe U1 has a
    rail pad on the plane and a GND pad in a cut-out, with a via down to In1.Cu."""
    SIZE = 100.0
    H = 0.2
    PROBE = (5.0, 5.0)
    PAD, HOLE, GAP = 0.6, 1.2, 1.2     # pad side, GND pad cut-out side, rail pad -> GND pad
    MAX_EDGE = 2.0


def _ac_part(b: Board, ref, xy, value, rail="+3V3", gap=AcPlane.GAP):
    """A two-pad part: rail pad at ``xy``, GND pad ``gap`` to the right with a via down to
    In1.Cu. Returns the GND pad's cut-out in the rail plane."""
    x, y = xy
    s = AcPlane.PAD / 2
    b.pad(ref, "1", rail, box(x - s, y - s, x + s, y + s), value=value)
    gx = x + gap
    b.pad(ref, "2", "GND", box(gx - s, y - s, gx + s, y + s), value=value)
    b.via("GND", gx, y, 0.3, 0.6, layers=(F, IN1))
    h = AcPlane.HOLE / 2
    return box(gx - h, y - h, gx + h, y + h)


def _ac_board(name, size, parts, rail="+3V3"):
    b = Board(name, stackup((OZ1, OZ_HALF, OZ_HALF, OZ1), (AcPlane.H, 1.0, 0.2)))
    holes = [_ac_part(b, ref, xy, value, rail) for ref, xy, value in parts]
    plane = box(0, 0, size, size)
    for h in holes:
        plane = plane.difference(h)
    b.zone(rail, F, plane)
    b.zone("GND", IN1, box(0, 0, size, size))
    return b


def _ac_setup(vrms, probes, caps=(), links=(), **ac):
    s = PISetup(vrms, [], list(links), SolveSettings())
    s.caps, s.probes = list(caps), list(probes)
    s.ac.rail, s.ac.ground = vrms[0].name, "GND"
    for k, v in ac.items():
        setattr(s.ac, k, v)
    return s


def _probe(ref, rail="+3V3"):
    return Probe(ref, PinRef(ref, rail, ["1"]), PinRef(ref, "GND", ["2"]))


def ac_plane():
    g = AcPlane
    b = _ac_board("ac_plane", g.SIZE, [("U1", g.PROBE, "Probe")])
    vrm = Vrm("VRM1", PinRef("U1", "+3V3", ["1"]), 3.3, ground=PinRef("U1", "GND", ["2"]))
    setup = _ac_setup([vrm], [_probe("U1")], f_start_hz=1e3, f_stop_hz=1e9,
                      points_per_decade=100, field_points_per_decade=20,
                      max_edge_mm=g.MAX_EDGE)
    return b.snapshot(), setup


class AcDecap:
    """A 50 mm plane pair (as :class:`AcPlane`) with one capacitor C1 beside the probe."""
    SIZE = 50.0
    CAP = (25.0, 30.0)
    C, ESL, ESR = 10e-6, 0.5e-9, 0.02
    MAX_EDGE = 1.0


def ac_decap():
    g = AcDecap
    b = _ac_board("ac_decap", g.SIZE, [("U1", (25.0, 25.0), "Probe"), ("C1", g.CAP, "10u")])
    vrm = Vrm("VRM1", PinRef("U1", "+3V3", ["1"]), 3.3, ground=PinRef("U1", "GND", ["2"]))
    caps = [CapModel("10u", g.C, g.ESL, g.ESR, ["C1"])]
    setup = _ac_setup([vrm], [_probe("U1")], caps, f_start_hz=1e3, f_stop_hz=1e9,
                      points_per_decade=100, field_points_per_decade=10,
                      max_edge_mm=g.MAX_EDGE)
    return b.snapshot(), setup


class AcFerrite:
    """+3V3 (a 40 mm plane pair with a bulk capacitor C1) feeds +3V3_F (a 15 mm plane pair
    with the probe U2) through ferrite FB1, a series link of R_FB and L_FB. L_FB is large
    next to the copper's own inductance, so the probe sees lumped parts: FB1 resonates
    with the +3V3_F plane's capacitance."""
    SIZE_A, SIZE_B, GAP = 40.0, 15.0, 5.0
    C, ESL, ESR = 100e-6, 1e-9, 0.01
    R_FB, L_FB = 1.0, 1e-6
    MAX_EDGE = 1.0


def ac_ferrite():
    g = AcFerrite
    b = Board("ac_ferrite", stackup((OZ1, OZ_HALF, OZ_HALF, OZ1), (AcPlane.H, 1.0, 0.2)))
    xb = g.SIZE_A + g.GAP                      # +3V3_F plane's left edge
    yb = (g.SIZE_A - g.SIZE_B) / 2
    holes_a = [_ac_part(b, "C1", (20.0, 20.0), "100u")]
    holes_b = [_ac_part(b, "U2", (xb + 7.0, yb + 7.5), "Probe", rail="+3V3_F")]
    b.pad("U1", "1", "+3V3", box(5 - 0.3, 20 - 0.3, 5 + 0.3, 20 + 0.3), value="Regulator")
    s = AcPlane.PAD / 2
    b.pad("FB1", "1", "+3V3", box(g.SIZE_A - 1.0 - s, 20 - s, g.SIZE_A - 1.0 + s, 20 + s),
          value="Ferrite")
    b.pad("FB1", "2", "+3V3_F", box(xb + 1.0 - s, 20 - s, xb + 1.0 + s, 20 + s))
    plane_a, plane_b = box(0, 0, g.SIZE_A, g.SIZE_A), box(xb, yb, xb + g.SIZE_B, yb + g.SIZE_B)
    for h in holes_a:
        plane_a = plane_a.difference(h)
    for h in holes_b:
        plane_b = plane_b.difference(h)
    b.zone("+3V3", F, plane_a)
    b.zone("+3V3_F", F, plane_b)
    b.zone("GND", IN1, box(0, 0, xb + g.SIZE_B, g.SIZE_A))
    vrm = Vrm("U1", PinRef("U1", "+3V3", ["1"]), 3.3)
    caps = [CapModel("100u", g.C, g.ESL, g.ESR, ["C1"])]
    fb1 = DcLink("FB1", PinRef("FB1", "+3V3", ["1"]), PinRef("FB1", "+3V3_F", ["2"]),
                 g.R_FB, esl_h=g.L_FB)
    setup = _ac_setup([vrm], [_probe("U2", "+3V3_F")], caps, [fb1], f_start_hz=1e3,
                      f_stop_hz=300e6, points_per_decade=100, field_points_per_decade=10,
                      max_edge_mm=g.MAX_EDGE)
    return b.snapshot(), setup


BOARDS = {f.__name__: f for f in (skinny_traces, right_angle, layer_change, planes,
                                  power_chain, ac_plane, ac_decap, ac_ferrite)}


def paths(name: str):
    """The snapshot and setup file of board ``name``."""
    d = HERE / name
    return d / f"{name}.kipi.json.gz", d / f"{name}.kipi-setup.yml"


def write(name: str):
    snap, setup = BOARDS[name]()
    snap_path, setup_path = paths(name)
    snap_path.parent.mkdir(exist_ok=True)
    save(snap, snap_path)
    setup.save(setup_path)
    return snap_path, setup_path


if __name__ == "__main__":
    for name in sys.argv[1:] or BOARDS:
        print("Wrote", *(p.relative_to(HERE).as_posix() for p in write(name)))
