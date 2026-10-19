"""Synthetic boards for the AC solver: plane pairs with a probe, and decoupling caps."""

from kipi.core.model.board import BoardSnapshot, Footprint, Net, Pad, Poly, Via, Zone
from synthetic import B, F, IN1, IN2, make_board, rect, square  # noqa: F401


def plane_pair(size=50.0, probe_xy=(5.0, 5.0), caps=(), rail=None,
               ground=None) -> BoardSnapshot:
    """+3V3 plane on F.Cu over a GND plane on In1.Cu (0.2 mm FR4, er 4.5).

    The probe U1 has a +3V3 pad on the plane and a GND pad in a cut-out, with a via down to
    In1. ``caps`` is a list of (ref, value, (x, y)): each gets a +3V3 pad on the plane and
    a GND pad + via 1.2 mm to its right. ``rail`` and ``ground`` are the planes' outlines
    (default: a ``size`` square).
    """
    rail = rail or rect(0, 0, size, size)
    ground = ground or rect(0, 0, size, size)
    snap = make_board()
    snap.board_name = "plane_pair.kicad_pcb"
    snap.tracks, snap.vias, snap.pads, snap.footprints = [], [], [], []
    snap.nets = [Net(0, ""), Net(1, "+3V3"), Net(2, "GND")]
    holes = []
    pads, fps, vias = [], [], []

    def part(ref, value, xy):
        x, y = xy
        gx = x + 1.2
        pads.append(Pad(f"{ref}.1", "+3V3", ref, "1", (x, y), "smd",
                        shapes={F: [Poly(square(x, y, 0.6))]}))
        pads.append(Pad(f"{ref}.2", "GND", ref, "2", (gx, y), "smd",
                        shapes={F: [Poly(square(gx, y, 0.6))]}))
        vias.append(Via(f"v{ref}", "GND", (gx, y), 0.3, 0.6, [F, IN1]))
        holes.append(square(gx, y, 1.2))
        fps.append(Footprint(f"f{ref}", ref, value, (x, y), "top", [f"{ref}.1", f"{ref}.2"]))

    part("U1", "LOAD", probe_xy)
    for ref, value, xy in caps:
        part(ref, value, xy)
    snap.pads, snap.footprints, snap.vias = pads, fps, vias
    snap.zones = [
        Zone("z1", "+3V3", "", 0, filled={F: [Poly(rail, holes)]}),
        Zone("z2", "GND", "", 0, filled={IN1: [Poly(ground)]}),
    ]
    xs, ys = [x for x, _ in ground], [y for _, y in ground]
    snap.outline = [Poly(rect(min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1))]
    snap.invalidate_index()
    return snap


def plane_setup(snap, **ac):
    from kipi.ac.model import Probe
    from kipi.dc.pi.model import PISetup, PinRef, Vrm
    setup = PISetup()
    setup.vrms.append(Vrm("VRM1", PinRef("U1", "+3V3", ["1"]), 3.3,
                          ground=PinRef("U1", "GND", ["2"])))
    setup.probes.append(Probe("U1", PinRef("U1", "+3V3", ["1"]), PinRef("U1", "GND", ["2"])))
    for k, v in ac.items():
        setattr(setup.ac, k, v)
    return setup
