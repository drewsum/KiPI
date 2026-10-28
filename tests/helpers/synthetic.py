"""A tiny hand-built 4-layer board with easily computed copper areas."""

from kipi.core.model.stackup import DEFAULT_INNER_CU_MM, DEFAULT_OUTER_CU_MM  # noqa: F401
from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Footprint, Net, Pad, Poly, Stackup, Track,
    Via, Zone,
)

F, IN1, IN2, B = 3, 4, 5, 34


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def square(cx, cy, size):
    h = size / 2
    return rect(cx - h, cy - h, cx + h, cy + h)


def make_board() -> BoardSnapshot:
    # 35 um outer, 17.5 um inner; 0.2 / 1.0 / 0.2 mm cores -> 1.505 mm total
    cu_t = [0.035, 0.0175, 0.0175, 0.035]
    diel_t = [0.2, 1.0, 0.2]
    ids = [F, IN1, IN2, B]
    names = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    total = sum(cu_t) + sum(diel_t)
    z = total
    copper, diels = [], []
    for i in range(4):
        z -= cu_t[i]
        copper.append(CopperLayer(id=ids[i], name=names[i], index=i, thickness_mm=cu_t[i],
                                  z_bottom_mm=z))
        if i < 3:
            z -= diel_t[i]
            diels.append(DielectricLayer(thickness_mm=diel_t[i], material="FR4", epsilon_r=4.5,
                                         above_layer=ids[i], below_layer=ids[i + 1]))
    stackup = Stackup(copper=copper, dielectrics=diels, board_thickness_mm=total)

    pads = [
        Pad(id="p1", net="+3V3", footprint="U1", number="1", position=(0.0, 0.0),
            pad_type="smd", shapes={F: [Poly(square(0, 0, 1.0))]}),
        Pad(id="p2", net="+3V3", footprint="J1", number="1", position=(10.0, 0.0),
            pad_type="smd", shapes={F: [Poly(square(10, 0, 1.0))]}),
        Pad(id="p3", net="GND", footprint="U1", number="2", position=(0.0, 3.0),
            pad_type="smd", shapes={F: [Poly(square(0, 3, 1.0))]}),
    ]
    return BoardSnapshot(
        board_name="synthetic.kicad_pcb", project_dir="", stackup=stackup,
        nets=[Net(0, ""), Net(1, "+3V3"), Net(2, "GND"), Net(3, "Net-(U1-Pad3)")],
        tracks=[Track(net="+3V3", layer=F, width_mm=0.5, start=(0.0, 0.0), end=(10.0, 0.0),
                     id="t1")],
        vias=[Via(id="v1", net="+3V3", position=(10.0, 0.0), drill_mm=0.3, diameter_mm=0.6,
                  layers=ids)],
        pads=pads,
        footprints=[Footprint(id="f1", reference="U1", value="LDO", position=(0, 0), side="top",
                              pad_ids=["p1", "p3"]),
                    Footprint(id="f2", reference="J1", value="CONN", position=(10, 0),
                              side="top", pad_ids=["p2"])],
        zones=[
            Zone(id="z1", net="GND", name="", priority=0,
                 filled={IN1: [Poly(rect(0, -5, 20, 5), [square(10, 0, 1.0)])]}),
            Zone(id="z2", net="+3V3", name="", priority=0, filled={IN2: [Poly(rect(0, -5, 20, 5))]}),
        ],
        outline=[Poly(rect(-2, -6, 22, 6))],
        source="synthetic",
    )
