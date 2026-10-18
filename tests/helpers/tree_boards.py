"""Power-tree test boards: three 1 mm wide strips, so every drop has a closed form."""

from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Zone,
)
from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm
from kipi.dc.solver.setup import RHO_CU_OHM_MM as RHO, SolveSettings

F, B = 3, 34
T = 0.035


def poly(g):
    return Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])


def pad(pid, net, ref, num, g, layer=F):
    return Pad(pid, net, ref, num, (g.centroid.x, g.centroid.y), "smd", shapes={layer: [poly(g)]})


def R(length):
    """Resistance of a 1 mm wide strip of this length."""
    return RHO * length / (1.0 * T)


def board():
    """VIN: J1.1 -> U1.1 on F (x 0..10).  +3V3: U1.2 -> U2.1 on F (x 12..22).
    GND on B (x 0..22): J1.2 at 0..1, U1.3 at 10..11, U2.2 at 21..22."""
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [pad("j1_1", "VIN", "J1", "1", box(0, 0, 1, 1)),
            pad("u1_1", "VIN", "U1", "1", box(9, 0, 10, 1)),
            pad("u1_2", "+3V3", "U1", "2", box(12, 0, 13, 1)),
            pad("u2_1", "+3V3", "U2", "1", box(21, 0, 22, 1)),
            pad("j1_2", "GND", "J1", "2", box(0, 0, 1, 1), B),
            pad("u1_3", "GND", "U1", "3", box(10, 0, 11, 1), B),
            pad("u2_2", "GND", "U2", "2", box(21, 0, 22, 1), B)]
    zones = [Zone("a", "VIN", "", 0, {F: [poly(box(0, 0, 10, 1))]}),
             Zone("b", "+3V3", "", 0, {F: [poly(box(12, 0, 22, 1))]}),
             Zone("c", "GND", "", 0, {B: [poly(box(0, 0, 22, 1))]})]
    nets = [Net(1, "VIN"), Net(2, "+3V3"), Net(3, "GND")]
    return BoardSnapshot("tree", "", st, nets, [], [], pads, [], zones, [])


def setup(kind="ldo", eff=0.9, iq=0.0, sense=False, ground=False, gsense=False):
    src = Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source",
              ground=PinRef("J1", "GND", ["2"]) if ground else None)
    reg = Vrm("U1", PinRef("U1", "+3V3", ["2"]), 3.3, kind, efficiency=eff, quiescent_a=iq,
              input=PinRef("U1", "VIN", ["1"]),
              sense=PinRef("U2", "+3V3", ["1"]) if sense else None,
              ground=PinRef("U1", "GND", ["3"]) if ground else None,
              ground_sense=PinRef("U2", "GND", ["2"]) if gsense else None)
    load = Load("U2", PinRef("U2", "+3V3", ["1"]), 1.0,
                ground=PinRef("U2", "GND", ["2"]) if ground else None, tol_min=-1.0, tol_max=1.0)
    return PISetup([src, reg], [load], [], SolveSettings(max_edge_mm=0.3))


def by_name(items, name):
    return next(x for x in items if x.name == name)
