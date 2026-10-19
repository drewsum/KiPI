"""A synthetic single-phase buck for the Coupled Noise mode (#69).

On F.Cu: the 12 V connector J1, input capacitor C1, power stage U1 (VIN, GND and SW pads),
inductor L1, output capacitor C2 and load U2, joined by tracks; a GND plane fills In1.Cu,
0.2 mm under them, with a via at every GND pad.
"""

from typing import Optional

from kipi.core.model.board import Footprint, Net, Pad, Poly, Track, Via, Zone
from synthetic import F, IN1, make_board, rect, square  # noqa: F401


def buck_board(sw_area: float = 4.0, victim_gap: Optional[float] = None,
               victim_layer: int = F):
    """``sw_area``: the side of the switch node's square pour at U1's SW pad, mm.

    With ``victim_gap``, a 0.2 mm signal trace SIG runs from U3 (x = 8) to U4 (x = 24)
    beside the switch node's pour (x 11-15), ``victim_gap`` from its top edge, on
    ``victim_layer``.
    """
    snap = make_board()
    snap.board_name = "buck.kicad_pcb"
    snap.nets = [Net(0, ""), Net(1, "VIN"), Net(2, "GND"), Net(3, "SW"), Net(4, "VOUT"),
                 Net(5, "SIG")]
    pads, fps, vias, tracks = [], [], [], []

    def part(ref, value, pins):
        ids = []
        for num, net, (x, y) in pins:
            pid = f"{ref}.{num}"
            pads.append(Pad(pid, net, ref, num, (x, y), "smd",
                            shapes={F: [Poly(square(x, y, 1.0))]}))
            ids.append(pid)
            if net == "GND":
                vias.append(Via(f"v{pid}", "GND", (x, y + 0.9), 0.3, 0.6, [F, IN1]))
        x0, y0 = pins[0][2]
        fps.append(Footprint(f"f{ref}", ref, value, (x0, y0), "top", ids))

    part("J1", "CONN", [("1", "VIN", (2.0, 10.0)), ("2", "GND", (2.0, 14.0))])
    part("C1", "10uF 25V X5R", [("1", "VIN", (6.0, 10.0)), ("2", "GND", (6.0, 14.0))])
    part("U1", "BUCK", [("1", "VIN", (10.0, 10.0)), ("2", "GND", (10.0, 14.0)),
                        ("3", "SW", (12.0, 12.0))])
    part("L1", "1uH", [("1", "SW", (16.0, 12.0)), ("2", "VOUT", (20.0, 12.0))])
    part("C2", "47uF", [("1", "VOUT", (24.0, 12.0)), ("2", "GND", (24.0, 15.0))])
    part("U2", "LOAD", [("1", "VOUT", (28.0, 12.0)), ("2", "GND", (28.0, 15.0))])
    for net, a, b, w in (("VIN", (2, 10), (10, 10), 1.0), ("GND", (2, 14), (10, 14), 1.0),
                         ("SW", (12, 12), (16, 12), 1.0), ("VOUT", (20, 12), (28, 12), 1.0),
                         ("GND", (24, 15), (28, 15), 1.0)):
        tracks.append(Track(net, F, w, a, b))
    if victim_gap is not None:
        y = 12.0 + sw_area / 2 + victim_gap + 0.1
        for ref, x in (("U3", 8.0), ("U4", 24.0)):
            pid = f"{ref}.1"
            pads.append(Pad(pid, "SIG", ref, "1", (x, y), "smd",
                            shapes={victim_layer: [Poly(square(x, y, 0.3))]}))
            fps.append(Footprint(f"f{ref}", ref, "IC", (x, y),
                                 "top" if victim_layer == F else "bottom", [pid]))
        tracks.append(Track("SIG", victim_layer, 0.2, (8.0, y), (24.0, y)))
    snap.pads, snap.footprints, snap.vias, snap.tracks = pads, fps, vias, tracks
    snap.zones = [Zone("zgnd", "GND", "", 0, filled={IN1: [Poly(rect(0, 0, 32, 20))]}),
                  Zone("zsw", "SW", "", 0,
                       filled={F: [Poly(square(13.0, 12.0, sw_area))]})]
    snap.outline = [Poly(rect(0, 0, 32, 20))]
    snap.invalidate_index()
    return snap


def buck_setup(load_a: float = 5.0):
    from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm
    setup = PISetup()
    setup.vrms.append(Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0,
                          ground=PinRef("J1", "GND", ["2"])))
    setup.vrms.append(Vrm("U1", PinRef("L1", "VOUT", ["2"]), 1.8, kind="smps",
                          input=PinRef("U1", "VIN", ["1"]),
                          ground=PinRef("U1", "GND", ["2"])))
    setup.loads.append(Load("U2", PinRef("U2", "VOUT", ["1"]), load_a,
                            ground=PinRef("U2", "GND", ["2"])))
    return setup
