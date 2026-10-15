"""A two-phase VRM test board: three 1 mm wide, 20 mm long strips."""

from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Stackup, Zone,
)
from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm, VrmPhase
from kipi.dc.solver.setup import SolveSettings
from tree_boards import B, F, T, pad, poly

I_LOAD = 2.0


def board():
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [pad("l1_2", "VOUT", "L1", "2", box(0, 0, 1, 1)),
            pad("u2_1", "VOUT", "U2", "1", box(5, 0, 6, 1)),
            pad("l2_2", "VOUT", "L2", "2", box(19, 0, 20, 1)),
            pad("j1_1", "VIN", "J1", "1", box(0, 3, 1, 4)),
            pad("q1_1", "VIN", "Q1", "1", box(9, 3, 10, 4)),
            pad("q2_1", "VIN", "Q2", "1", box(19, 3, 20, 4)),
            pad("j1_2", "GND", "J1", "2", box(0, 0, 1, 1), B),
            pad("u2_2", "GND", "U2", "2", box(5, 0, 6, 1), B),
            pad("q1_2", "GND", "Q1", "2", box(9, 0, 10, 1), B),
            pad("q2_2", "GND", "Q2", "2", box(19, 0, 20, 1), B)]
    zones = [Zone("a", "VOUT", "", 0, {F: [poly(box(0, 0, 20, 1))]}),
             Zone("b", "VIN", "", 0, {F: [poly(box(0, 3, 20, 4))]}),
             Zone("c", "GND", "", 0, {B: [poly(box(0, 0, 20, 1))]})]
    nets = [Net(1, "VOUT"), Net(2, "VIN"), Net(3, "GND")]
    return BoardSnapshot("multiphase", "", st, nets, [], [], pads, [], zones, [])


def two_phase(kind="ldo", eff=0.9, iq=0.0, sense=False, ground=False):
    gnd = (lambda ref: PinRef(ref, "GND", ["2"])) if ground else (lambda ref: None)
    src = Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source", ground=gnd("J1"))
    reg = Vrm("U1", PinRef("L1", "VOUT", ["2"]), 1.0, kind, efficiency=eff, quiescent_a=iq,
              input=PinRef("Q1", "VIN", ["1"]), ground=gnd("Q1"),
              sense=PinRef("U2", "VOUT", ["1"]) if sense else None,
              phases=[VrmPhase(PinRef("L2", "VOUT", ["2"]), PinRef("Q2", "VIN", ["1"]),
                               gnd("Q2"))])
    load = Load("U2", PinRef("U2", "VOUT", ["1"]), I_LOAD, ground=gnd("U2"))
    return PISetup([src, reg], [load], [], SolveSettings(max_edge_mm=0.3))
