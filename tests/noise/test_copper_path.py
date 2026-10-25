"""The hot loops traced through the copper (#187): the conduction solves' current as the
loop's segments and streamlines, their inductance, and what the solve does with them."""

import math

import numpy as np
import pytest

from kipi.ac.model import CapModel
from kipi.noise.checks import seed_phases
from kipi.noise.extract.inductive import LoopSource, loop_sources
from kipi.noise.extract.peec import (
    distribution_inductance, loop_inductance, mutual_matrix, plane_sides, polyline, segment,
)
from kipi.noise.model import Aggressor, Victim
from kipi.noise.parasitics import phase_layout
from kipi.noise.solver import solve_noise
from noise_boards import buck_board, buck_setup
from synthetic import F, IN1


def _setup(snap, victims=(), board_model=True):
    setup = buck_setup(board_model=board_model)
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1 buck", "U1", fsw_hz=1e6)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    a.phases[0].coss_f = 300e-12
    setup.aggressors.append(a)
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"]),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    setup.victims = list(victims)
    return setup


def _copper(snap, setup):
    """The rails' and ground's mesh and its paths, as the solve builds them."""
    from kipi.core.progress import NULL
    from kipi.noise.checks import operating_point
    from kipi.noise.solver import _copper
    agg = setup.aggressors[0]
    warnings = []
    mesh, copper = _copper(snap, setup, agg, operating_point(setup, agg), {}, warnings, NULL)
    assert copper is not None, warnings
    return mesh, copper


def _layout(snap, setup, copper=None):
    return phase_layout(snap, setup, setup.aggressors[0].phases[0], 1, "buck", 12.0, 1.8,
                        "VIN", "VOUT", "GND", copper=copper)


# ---------------------------------------------------------------- partial inductances

def test_a_distribution_with_unit_weights_is_the_loop():
    sq = [[0, 0, 0.5], [6, 0, 0.5], [6, 4, 0.5], [0, 4, 0.5]]
    segs = polyline(sq, [0.4] * 4, 0.035)
    w = np.ones(4)
    assert distribution_inductance(segs, w) == pytest.approx(loop_inductance(segs))
    # All above the plane: mirrored in its top face, as the loop's images are.
    assert distribution_inductance(segs, w, (0.0, 0.035)) == pytest.approx(
        loop_inductance(segs, 0.035))


def test_two_parallel_filaments_sharing_a_current_have_less_inductance():
    """Half the current in each of two parallel paths: L = (L11 + L12) / 2 < L11."""
    a = polyline([[0, 0, 0], [10, 0, 0], [10, 5, 0], [0, 5, 0]], [0.2] * 4, 0.035)
    b = polyline([[0, 10, 0], [10, 10, 0], [10, 15, 0], [0, 15, 0]], [0.2] * 4, 0.035)
    one = distribution_inductance(a, np.ones(4))
    both = distribution_inductance(a + b, np.full(8, 0.5))
    assert both < one
    m = mutual_matrix(a, b).sum()
    assert both == pytest.approx(0.25 * (one + distribution_inductance(b, np.ones(4)) + 2 * m),
                                 rel=1e-3)


def test_plane_sides():
    segs = [segment([0, 0, 1.0], [1, 0, 1.0], 0.1, 0.035),
            segment([0, 0, 0.2], [1, 0, 0.2], 0.1, 0.035),
            segment([0, 0, -1.0], [1, 0, -1.0], 0.1, 0.035),
            segment([0, 0, 0.6], [0, 0, 0.2], 0.1, 0.1)]       # a via drop: its middle above
    assert list(plane_sides(segs, 0.17, 0.21)) == [1, 0, -1, 1]
    assert distribution_inductance(segs[1:2], np.ones(1), (0.17, 0.21)) == 0.0


def test_the_sides_of_a_plane_do_not_couple_across_it():
    top = polyline([[0, 0, 1], [8, 0, 1], [8, 4, 1], [0, 4, 1]], [0.3] * 4, 0.035)
    bottom = polyline([[0, 0, -1], [8, 0, -1], [8, 4, -1], [0, 4, -1]], [0.3] * 4, 0.035)
    w = np.ones(8)
    plane = (-0.0175, 0.0175)
    both = distribution_inductance(top + bottom, w, plane)
    each = distribution_inductance(top, w[:4], plane) + distribution_inductance(bottom, w[:4],
                                                                                plane)
    assert both == pytest.approx(each)


def test_a_reduced_source_keeps_the_segments_carrying_the_current():
    segs = [segment([i, 0, 0], [i + 1, 0, 0], 0.5, 0.035) for i in range(100)]
    w = np.full(100, 0.001)
    w[[3, 40, 77]] = 1.0
    src = LoopSource("x", segs, np.ones(2), weights=w).reduced(0.9, 10)
    assert {s.a[0] for s in src.segments} >= {3.0, 40.0, 77.0} and len(src.segments) <= 10
    assert src.weights.max() == 1.0


# ---------------------------------------------------------------- the traced loop

@pytest.fixture(scope="module")
def traced():
    snap = buck_board()
    setup = _setup(snap)
    mesh, copper = _copper(snap, setup)
    return snap, setup, mesh, copper, _layout(snap, setup, copper)


def test_the_flow_runs_from_the_capacitor_to_the_device_through_the_copper(traced):
    snap, setup, mesh, copper, lay = traced
    loop = lay.loops["C1"]
    assert loop.traced and loop.complete and lay.report.traced
    # Segments on the VIN track between C1 (x = 6) and U1 (x = 10), carrying the 1 A.
    rail = [(s, w) for s, w in zip(loop.elements, loop.weights)
            if "C1+ to U1" in s.label and "via" not in s.label]
    assert rail and all(5.0 <= s.a[0] <= 11.0 for s, _ in rail)
    # The cells' current elements add up to the ampere along the 3 mm of track between
    # the pads (inside a pad the copper is one node and carries no element), +x.
    assert sum(w * (s.b[0] - s.a[0]) for s, w in rail) == pytest.approx(3.0, rel=0.25)
    # The ground return takes the top track and the plane through the vias: vertical
    # segments reach In1.Cu.
    z1 = snap.stackup.layer(IN1).z_mid_mm
    vias = [s for s in loop.elements if "via" in s.label]
    assert vias and any(min(s.a[2], s.b[2]) == pytest.approx(z1) for s in vias)
    assert loop.r_ohm > 0 and loop.size_mm == pytest.approx(4.0)
    assert 0.1e-9 < lay.report.cap_loops_h["C1"] < 3e-9


def test_the_streamlines_start_at_the_capacitor_and_end_at_the_device(traced):
    snap, setup, mesh, copper, lay = traced
    paths = lay.loops["C1"].paths
    zf = snap.stackup.layer(F).z_mid_mm
    # Each line runs from the middle of C1's VIN pad to the middle of U1's.
    rail = [p for p in paths if tuple(p[0]) == (6.0, 10.0, zf) and len(p) > 2]
    assert len(rail) == 5
    for p in rail:
        assert tuple(p[-1]) == (10.0, 10.0, zf)
        assert (np.diff(p[:, 0]) >= -1e-9).all()                     # along the track
    gnd = [p for p in paths if tuple(p[0]) == (10.0, 14.0, zf) and len(p) > 2]
    assert len(gnd) == 5 and all(tuple(p[-1]) == (6.0, 14.0, zf) for p in gnd)
    # Some of the return goes down U1's ground via into the plane and up C1's.
    z1 = snap.stackup.layer(IN1).z_mid_mm
    assert any((p[:, 2] == z1).any() for p in gnd)
    assert all((p[:, 2] == z1).any() for p in gnd) is False           # and some stays on top
    # The device and the capacitor's body close the loop.
    assert any(len(p) == 2 and tuple(p[0, :2]) == (10.0, 10.0) and tuple(p[1, :2]) == (10.0, 14.0)
               for p in paths)
    assert any(len(p) == 2 and tuple(p[0, :2]) == (6.0, 14.0) and tuple(p[1, :2]) == (6.0, 10.0)
               for p in paths)


def test_the_output_loop_goes_through_the_switch_node(traced):
    snap, setup, mesh, copper, lay = traced
    loop = lay.loops["C2"]
    assert loop.traced
    assert any("L1 to U1" in s.label for s in loop.elements)         # the SW net, meshed alone
    assert any("through L1" in s.label for s in loop.elements)


def test_a_detour_in_the_copper_lengthens_the_loop():
    straight, bent = buck_board(), buck_board(detour=True)
    s_lay = _layout(straight, _setup(straight), _copper(straight, _setup(straight))[1])
    b_lay = _layout(bent, _setup(bent), _copper(bent, _setup(bent))[1])
    assert b_lay.report.cap_loops_h["C1"] > 1.5 * s_lay.report.cap_loops_h["C1"]
    assert b_lay.loops["C1"].r_ohm > 1.5 * s_lay.loops["C1"].r_ohm
    ys = np.concatenate([p[:, 1] for p in b_lay.loops["C1"].paths])
    assert ys.min() < 4.6                                            # it goes up to y = 4
    # Drawn straight between the pads, the detour is all but invisible.
    sketch = (_layout(bent, _setup(bent)).report.cap_loops_h["C1"]
              / _layout(straight, _setup(straight)).report.cap_loops_h["C1"])
    assert sketch < 1.2


def test_the_solve_traces_the_loops_and_says_so():
    snap = buck_board(victim_gap=0.2, victim_by="hot_loop")
    r = solve_noise(snap, _setup(snap, [Victim("SIG")]), "U1 buck")
    assert r.hot_loops[0].traced
    assert any("traced through the copper" in line for line in r.summary_lines())
    assert set(r.loop_paths) == {"C1 (phase 1)", "C2 (phase 1)"}
    z1 = snap.stackup.layer(IN1).z_mid_mm
    assert (np.abs(r.loop_paths["C1 (phase 1)"][:, :, 2] - z1) < 1e-9).any()
    v = r.victims[0]
    assert v.dominant == "inductive" and v.m_h["C1 loop (phase 1)"] != 0
    off = solve_noise(snap, _setup(snap, [Victim("SIG")], board_model=False), "U1 buck")
    assert not off.hot_loops[0].traced
    assert any("straight between the pads" in w for w in off.warnings)
    # Beside the forward track both agree within a factor: the traced loop spreads the
    # return over the top ground track and the plane, the sketch runs it along the track.
    ratio = v.peak_by["inductive"] / off.victims[0].peak_by["inductive"]
    assert 0.3 < ratio < 3.0


def test_loop_sources_carry_the_weights(traced):
    snap, setup, mesh, copper, lay = traced
    from kipi.noise.waveforms import Spectrum
    class _WF:
        input_caps = {"C1": Spectrum("I(C1)", "A", 0.0, np.ones(3))}
        output_caps = {}
    (src,) = loop_sources([lay], _WF())
    assert len(src.weights) == len(src.segments) == len(lay.loops["C1"].elements)
    assert src.size_mm == pytest.approx(4.0) and src.image_layer == IN1
    small = loop_sources([lay], _WF(), estimate=True)[0]
    assert len(small.segments) <= len(src.segments)
    assert math.isclose(np.abs(small.weights).max(), np.abs(src.weights).max())
