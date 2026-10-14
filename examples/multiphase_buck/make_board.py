"""Build the example board: a 4-phase 12 V -> 0.9 V buck feeding a BGA core rail.

    python examples/multiphase_buck/make_board.py

writes ``multiphase_buck.kipi.json.gz`` next to this script. Open it without KiCad:

    python -m kipi --snapshot examples/multiphase_buck/multiphase_buck.kipi.json.gz

KiPI then loads ``multiphase_buck.kipi-setup.json`` from the same folder.

The board is 60 x 40 mm, four layers (1 oz everywhere):

* F.Cu: the VIN pour on the left, a GND strip, then the VCORE pour.
* In1.Cu: solid GND plane.
* In2.Cu: VIN plane under the power stages.
* B.Cu: VCORE pour, stitched to F.Cu next to each inductor and under the load.

Decoupling for AC mode: eight 1 µF capacitors C1-C8 around the load and a 100 µF bulk
capacitor C10-C13 after each inductor, each on F.Cu with a GND via next to its GND pad.

The phases sit in a column at y = 5, 15, 25 and 35 mm. Each phase is a power stage
Qn (VIN pad 1, GND pad 2) and an inductor Ln (output pad 2 on VCORE); the switch node
lives inside the VRM model and is not drawn. The load U10 sits near the top at
(46, 30), so phases 3 and 4 are close to it and phases 1 and 2 are far away. With equal
current sharing, the far phases have to sit at a higher voltage.
"""

from __future__ import annotations

import sys
from pathlib import Path

from shapely.geometry import MultiPolygon, Point, box
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from kipi.core.kicad_io.snapshot import save  # noqa: E402
from kipi.core.model.board import (  # noqa: E402
    BoardSnapshot, CopperLayer, DielectricLayer, Footprint, Net, Pad, Poly, Stackup, Via, Zone,
)
from kipi.ac.model import CapModel, Probe  # noqa: E402
from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm, VrmPhase  # noqa: E402
from kipi.dc.solver.setup import SolveSettings  # noqa: E402

F, IN1, IN2, B = 3, 4, 5, 34
ALL = [F, IN1, IN2, B]
CU = 0.035
CLEARANCE = 0.25
W, H = 60.0, 40.0
PHASE_Y = [5.0, 15.0, 25.0, 35.0]
LOAD = (46.0, 30.0)
PITCH = 1.27
BALLS = 6


def stackup() -> Stackup:
    diel = [0.2, 1.0, 0.2]
    total = 4 * CU + sum(diel)
    z, copper, diels = total, [], []
    for i, (lid, name) in enumerate(zip(ALL, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])):
        z -= CU
        copper.append(CopperLayer(lid, name, i, CU, z))
        if i < 3:
            z -= diel[i]
            diels.append(DielectricLayer(diel[i], "FR4", 4.5, lid, ALL[i + 1]))
    return Stackup(copper, diels, total)


def polys(geom) -> list:
    geoms = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    return [Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])
            for g in geoms if not g.is_empty]


class Builder:
    def __init__(self):
        self.pads, self.vias, self.footprints = [], [], {}
        self.copper = {}            # (layer, net) -> shapely geometry of pads and vias

    def _add_copper(self, layer, net, geom):
        key = (layer, net)
        self.copper[key] = unary_union([self.copper.get(key, geom), geom])

    def pad(self, ref, number, net, geom, layers=(F,), drill=0.0):
        pid = f"{ref}_{number}".lower()
        c = geom.centroid
        self.pads.append(Pad(pid, net, ref, number, (c.x, c.y), "pth" if drill else "smd",
                             (drill, drill), {lid: polys(geom) for lid in layers}))
        for lid in layers:
            self._add_copper(lid, net, geom)
        fp = self.footprints.setdefault(ref, Footprint(f"fp_{ref}", ref, "", (c.x, c.y),
                                                       "top"))
        fp.pad_ids.append(pid)

    def via(self, net, x, y, drill=0.3, dia=0.6):
        self.vias.append(Via(f"v{len(self.vias)}", net, (x, y), drill, dia, list(ALL)))
        for lid in ALL:
            self._add_copper(lid, net, Point(x, y).buffer(dia / 2, quad_segs=4))

    def zone(self, net, layer, region):
        """Fill ``region`` on ``layer``, clearing every other net's copper there and, like
        KiCad, removing islands that touch none of the net's own pads or vias."""
        others = [g for (lid, n), g in self.copper.items() if lid == layer and n != net]
        fill = region.difference(unary_union(others).buffer(CLEARANCE)) if others else region
        pieces = list(fill.geoms) if isinstance(fill, MultiPolygon) else [fill]
        own = self.copper.get((layer, net))
        biggest = max(pieces, key=lambda g: g.area)
        keep = [g for g in pieces if g is biggest or (own is not None and g.intersects(own))]
        return Zone(f"z_{net}_{layer}", net, "", 0, {layer: polys(MultiPolygon(keep))})


def build() -> BoardSnapshot:
    b = Builder()
    values = {"J1": "12V in", "U10": "SoC core"}

    # Input connector: two through-hole pins.
    b.pad("J1", "1", "VIN", Point(4, 24).buffer(1.5, quad_segs=4), ALL, drill=1.5)
    b.pad("J1", "2", "GND", Point(4, 16).buffer(1.5, quad_segs=4), ALL, drill=1.5)

    for k, y in enumerate(PHASE_Y, start=1):
        q, l = f"Q{k}", f"L{k}"
        values[q], values[l] = "Power stage", "150 nH"
        b.pad(q, "1", "VIN", box(7, y - 1.5, 10, y + 1.5))
        b.pad(q, "2", "GND", box(11, y - 1.5, 13, y + 1.5))
        b.pad(l, "2", "VCORE", box(15.5, y - 1.75, 18.5, y + 1.75))
        for dy in (-1.0, 0.0, 1.0):
            b.via("VIN", 8.5, y + dy)                 # power stage VIN to In2
            b.via("GND", 12.0, y + dy)                # power stage GND to In1
            for x in (19.5, 20.5):
                b.via("VCORE", x, y + dy)             # inductor output to B.Cu

    # Load: a 6 x 6 ball checkerboard of VCORE and GND, a via in every ball.
    x0 = LOAD[0] - PITCH * (BALLS - 1) / 2
    y0 = LOAD[1] - PITCH * (BALLS - 1) / 2
    for i in range(BALLS):
        for j in range(BALLS):
            x, y = x0 + i * PITCH, y0 + j * PITCH
            net = "VCORE" if (i + j) % 2 == 0 else "GND"
            b.pad("U10", f"{'ABCDEF'[j]}{i + 1}", net, box(x - 0.3, y - 0.3, x + 0.3, y + 0.3))
            b.via(net, x, y, drill=0.2, dia=0.45)
    # Decoupling: 1 µF around the load, 100 µF bulk after each inductor. Pad 1 on VCORE
    # (F.Cu pour), pad 2 on GND with a via down to the In1 plane.
    def cap(ref, value, x, y, size):
        values[ref] = value
        b.pad(ref, "1", "VCORE", box(x - size, y - size, x + size, y + size))
        b.pad(ref, "2", "GND", box(x + 1.2 - size, y - size, x + 1.2 + size, y + size))
        b.via("GND", x + 1.2 + size, y)
    for k, (x, y) in enumerate([(39.5, 25.0), (39.5, 30.0), (39.5, 35.0), (44.0, 22.5),
                                (48.0, 22.5), (51.0, 25.0), (51.0, 30.0), (51.0, 35.0)], 1):
        cap(f"C{k}", "1u", x, y, 0.3)
    for k, y in enumerate(PHASE_Y, start=10):
        cap(f"C{k}", "100u", 23.0, y, 0.6)
    # VCORE stitching on the way to the load.
    for x in (26, 32, 38):
        for y in (8, 16, 24, 32):
            b.via("VCORE", x, y)

    zones = [
        b.zone("VIN", F, box(0.5, 0.5, 10.4, H - 0.5)),
        b.zone("GND", F, box(10.6, 0.5, 13.4, H - 0.5)),
        b.zone("VCORE", F, box(14.5, 0.5, W - 0.5, H - 0.5)),
        b.zone("GND", IN1, box(0.5, 0.5, W - 0.5, H - 0.5)),
        b.zone("VIN", IN2, box(0.5, 0.5, 14.0, H - 0.5)),
        b.zone("VCORE", B, box(14.5, 0.5, W - 0.5, H - 0.5)),
    ]
    for fp in b.footprints.values():
        fp.value = values.get(fp.reference, "")
    return BoardSnapshot(
        "multiphase_buck.kicad_pcb", "", stackup(),
        [Net(0, ""), Net(1, "VIN"), Net(2, "VCORE"), Net(3, "GND")],
        [], b.vias, b.pads, list(b.footprints.values()), zones,
        [Poly([(0, 0), (W, 0), (W, H), (0, H)])], source="examples/multiphase_buck")


def make_setup() -> PISetup:
    """J1 feeds 12 V to U1, a 4-phase switcher regulating 0.9 V at the load's balls
    (remote sense C3, ground sense C4); U10 draws 40 A. For AC mode, capacitor models for
    the decoupling and a probe at the load's balls with a 2 mΩ target up to 20 MHz."""
    gnd = lambda ref, pins=None: PinRef(ref, "GND", pins or [])   # noqa: E731
    j1 = Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source", ground=gnd("J1", ["2"]))
    phases = [VrmPhase(PinRef(f"L{k}", "VCORE", ["2"]), PinRef(f"Q{k}", "VIN", ["1"]),
                       gnd(f"Q{k}", ["2"])) for k in range(1, 5)]
    u1 = Vrm("U1", phases[0].output, 0.9, "smps", efficiency=0.9, quiescent_a=0.01,
             input=phases[0].input, ground=phases[0].ground,
             sense=PinRef("U10", "VCORE", ["C3"]), ground_sense=gnd("U10", ["C4"]),
             phases=phases[1:], bandwidth_khz=100.0)
    load = Load("U10", PinRef("U10", "VCORE"), 40.0, ground=gnd("U10"), tolerance_pct=3.0)
    setup = PISetup([j1, u1], [load], [], SolveSettings(), nets=["VIN", "VCORE", "GND"])
    setup.caps = [CapModel("1u", 1e-6, 0.3e-9, 10e-3, [f"C{k}" for k in range(1, 9)]),
                  CapModel("100u", 100e-6, 0.8e-9, 3e-3, [f"C{k}" for k in range(10, 14)])]
    setup.probes = [Probe("U10", PinRef("U10", "VCORE"), gnd("U10"), 2e-3, 20e6)]
    setup.ac.rail, setup.ac.ground = "U1", "GND"
    return setup


if __name__ == "__main__":
    out = HERE / "multiphase_buck.kipi.json.gz"
    save(build(), out)
    make_setup().save(HERE / "multiphase_buck.kipi-setup.json")
    print(f"Wrote {out} and multiphase_buck.kipi-setup.json")
