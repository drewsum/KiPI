"""A narrow trace over a ground plane has the microstrip's inductance, not the cavity
model's μ₀·gap/(2w) per length (#258): its field fringes past its edges."""

import math

import numpy as np
import pytest

from kipi.ac.solver.fem import ACSystem, PortDef, local_width
from kipi.ac.solver.physics import FRINGE_NONE, MU0_H_PER_MM, fringe_factor
from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Footprint, Net, Pad, Poly, Stackup, Track,
    Via, Zone,
)
from kipi.dc.pi.model import PISetup
from synthetic import B, F, IN1, IN2, rect, square
from shapely.geometry import Polygon


def _microstrip_nh_per_mm(w, h):
    """Hammerstad's microstrip Z₀ in air, over c."""
    u = w / h
    z0 = (60 * math.log(8 / u + u / 4) if u <= 1
          else 120 * math.pi / (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return z0 / 299792458 * 1e6


def _board(h, w, length):
    """A +3V3 trace of width w on F.Cu, h above a GND plane on In1, from pad A to pad B,
    with a GND pad and via beside each end; a +3V3 plane on In2, tied to the trace at B."""
    cu_t, diel = [0.035] * 4, [h, 0.5, 0.5]
    ids, names = [F, IN1, IN2, B], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    z = sum(cu_t) + sum(diel)
    copper, dielectrics = [], []
    for i in range(4):
        z -= cu_t[i]
        copper.append(CopperLayer(ids[i], names[i], i, cu_t[i], z))
        if i < 3:
            z -= diel[i]
            dielectrics.append(DielectricLayer(diel[i], "FR4", 4.5, ids[i], ids[i + 1]))
    stackup = Stackup(copper, dielectrics, sum(cu_t) + sum(diel))
    pads, vias = [], []
    for ref, x in (("A", 0.0), ("B", length)):
        gy = 0.6 + w / 2
        pads.append(Pad(f"{ref}1", "+3V3", ref, "1", (x, 0), "smd",
                        shapes={F: [Poly(square(x, 0, w))]}))
        pads.append(Pad(f"{ref}2", "GND", ref, "2", (x, gy), "smd",
                        shapes={F: [Poly(square(x, gy, 0.4))]}))
        vias.append(Via(f"v{ref}", "GND", (x, gy), 0.2, 0.4, [F, IN1]))
    vias.append(Via("vr", "+3V3", (length, 0), 0.2, 0.4, [F, IN1, IN2]))
    plane = rect(-4, -4, length + 4, 4)
    return BoardSnapshot(
        "trace.kicad_pcb", "", stackup, [Net(0, ""), Net(1, "+3V3"), Net(2, "GND")],
        [Track("+3V3", F, w, (0, 0), (length, 0), id="t")], vias, pads,
        [Footprint("fA", "A", "", (0, 0), "top", ["A1", "A2"]),
         Footprint("fB", "B", "", (length, 0), "top", ["B1", "B2"])],
        [Zone("zg", "GND", "", 0, filled={IN1: [Poly(plane, [square(length, 0, 0.8)])]}),
         Zone("zr", "+3V3", "", 0, filled={IN2: [Poly(plane)]})],
        [Poly(rect(-5, -5, length + 5, 5))], source="synthetic")


def _loop_nh(h, w, length, f=100e6):
    """Inductance looking into A with B shorted."""
    setup = PISetup()
    for k, v in dict(max_edge_mm=0.2, min_edge_mm=0.03, grade_near_rail_only=False,
                     coarsen_far=False).items():
        setattr(setup.ac, k, v)
    ports = [PortDef("A", "probe", ["A1"], ["A2"]), PortDef("B", "probe", ["B1"], ["B2"])]
    system = ACSystem(_board(h, w, length), ["+3V3"], "GND", ports, [], setup.settings,
                      setup.ac)
    z = system.port_impedance(2 * math.pi * f)
    zin = z[0, 0] - z[0, 1] * z[1, 0] / z[1, 1]
    return zin.imag / (2 * math.pi * f) * 1e9


def test_the_factor_brings_a_narrow_strip_to_the_microstrips_inductance():
    for w, h in [(0.25, 0.48), (0.25, 0.2), (0.1, 0.2)]:
        cavity = MU0_H_PER_MM * 1e9 * h / (2 * w)        # its own face's, nH/mm
        assert cavity * fringe_factor(w, h) == pytest.approx(_microstrip_nh_per_mm(w, h),
                                                             rel=2e-3)


def test_wide_copper_and_planes_keep_the_cavity_model():
    assert fringe_factor(FRINGE_NONE * 0.2, 0.2) == pytest.approx(1.0, abs=0.01)
    assert np.all(fringe_factor(np.array([1.0, 10.0, np.inf]), 0.2) == 1.0)


def test_local_width_of_a_strip_and_a_plane():
    strip = Polygon(rect(0, 0, 10, 0.25))
    w = local_width(strip, np.array([5.0, 5.0]), np.array([0.125, 0.02]), 5.0)
    assert np.all((w > 0.2) & (w < 0.3))
    plane = Polygon(rect(0, 0, 20, 20))
    assert np.isinf(local_width(plane, np.array([10.0]), np.array([10.0]), 5.0))[0]


def test_a_dogbone_sized_trace_has_about_the_microstrips_inductance():
    """0.25 mm at 0.48 mm: the cavity model alone gave 1.25 nH/mm, 2.3x the microstrip."""
    h, w = 0.48, 0.25
    per_mm = (_loop_nh(h, w, 4.0) - _loop_nh(h, w, 2.0)) / 2.0
    assert per_mm == pytest.approx(_microstrip_nh_per_mm(w, h), rel=0.2)
