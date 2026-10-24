"""A probe whose ground pins are on a ground tied to the simulated one by a series link
(#171): an analog ground joined to GND by a net tie, a 0 ohm resistor or a ferrite is
solved as part of the same return."""

import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.checks import ground_nets, rail_nets, validate_ac
from kipi.ac.solver import solve_ac
from kipi.core.model.board import Footprint, Net, Pad, Poly, Track, Via
from kipi.dc.pi.model import DcLink, PinRef
from synthetic import F, IN1, rect, square


def split_ground():
    """The plane pair with U1's ground pad on GNDA, no via of its own: a track takes it to
    R1, whose other pad is on GND with a via down to the GND plane."""
    snap = plane_pair()
    snap.nets.append(Net(3, "GNDA"))
    gnd_pad = next(p for p in snap.pads if p.id == "U1.2")
    gnd_pad.net = "GNDA"
    snap.vias = [v for v in snap.vias if v.id != "vU1"]
    snap.pads += [
        Pad("R1.1", "GNDA", "R1", "1", (6.2, 6.2), "smd",
            shapes={F: [Poly(square(6.2, 6.2, 0.6))]}),
        Pad("R1.2", "GND", "R1", "2", (7.4, 6.2), "smd",
            shapes={F: [Poly(square(7.4, 6.2, 0.6))]}),
    ]
    snap.footprints.append(Footprint("fR1", "R1", "0R", (6.8, 6.2), "top", ["R1.1", "R1.2"]))
    snap.tracks = [Track("GNDA", F, 0.3, (6.2, 5.0), (6.2, 6.2))]
    snap.vias.append(Via("vR1", "GND", (7.4, 6.2), 0.3, 0.6, [F, IN1]))
    rail = next(z for z in snap.zones if z.net == "+3V3")
    rail.filled = {F: [Poly(rect(0, 0, 50, 50), [rect(5.6, 4.4, 8.0, 6.8)])]}
    snap.invalidate_index()
    return snap


def split_setup(snap, resistance=0.0, esl_h=None, link=True):
    setup = plane_setup(snap, f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=5,
                        field_points_per_decade=2)
    setup.probes[0].ground = PinRef("U1", "GNDA", ["2"])
    if link:
        setup.links.append(DcLink("R1", PinRef("R1", "GNDA", ["1"]),
                                  PinRef("R1", "GND", ["2"]), resistance, esl_h))
    return setup


def test_the_ground_takes_in_the_nets_links_tie_to_it():
    setup = split_setup(split_ground())
    vrm = setup.vrms[0]
    assert ground_nets(setup, vrm, "GND") == {"GND", "GNDA"}
    assert rail_nets(setup, vrm, "GND") == {"+3V3"}
    assert ground_nets(setup, vrm, "") == frozenset()


def test_a_link_from_the_rail_to_ground_is_not_a_ground_join():
    setup = split_setup(split_ground())
    setup.links.append(DcLink("R9", PinRef("R9", "+3V3"), PinRef("R9", "GND"), 1.0))
    vrm = setup.vrms[0]
    assert ground_nets(setup, vrm, "GND") == {"GND", "GNDA"}
    assert rail_nets(setup, vrm, "GND") == {"+3V3"}


def test_a_probe_on_an_untied_ground_says_how_to_tie_it():
    setup = split_setup(split_ground(), link=False)
    errors = validate_ac(setup, "VRM1", "GND")
    assert any("GNDA, not on the ground being simulated (GND)" in e
               and "add a series link" in e for e in errors)
    assert not validate_ac(split_setup(split_ground()), "VRM1", "GND")


@pytest.fixture(scope="module")
def solved():
    snap = split_ground()
    return (solve_ac(snap, split_setup(snap), "VRM1", "GND"),
            solve_ac(snap, split_setup(snap, 0.05, 2e-9), "VRM1", "GND"))


def test_a_probe_across_a_net_tie_solves_against_both_grounds(solved):
    short, _ = solved
    assert short.ground == "GND + GNDA"
    z = short.probes[0].mag
    assert np.all(np.isfinite(z)) and np.all(z > 0)
    # Open loop with no capacitors: the plane capacitance, falling as 1/f.
    assert z[0] > 10 * z[len(z) // 2]


def test_a_ferrite_between_the_grounds_is_in_the_loop(solved):
    short, ferrite = solved
    assert ferrite.links == ["R1"]
    # GNDA returns only through R1, so the ferrite is in series with the probe's loop:
    # its 50 mOhm and 2 nH add to the impedance, wherever the copper was solved (in
    # between, it is interpolated). The planes' kilohms at 100 kHz leave a residue of a
    # few parts per million.
    f = ferrite.freqs
    solved_at = np.isclose(f[:, None], ferrite.field_freqs[None, :], rtol=1e-9).any(axis=1)
    zs, zf = short.probes[0].z[solved_at], ferrite.probes[0].z[solved_at]
    expected = 0.05 + 2j * np.pi * f[solved_at] * 2e-9
    assert solved_at.sum() >= 4
    assert np.all(np.abs(zf - zs - expected) <= 0.02 * np.abs(expected) + 1e-5 * np.abs(zs))
