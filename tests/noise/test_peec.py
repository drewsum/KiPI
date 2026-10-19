"""Partial inductances (#69) against closed forms: Ruehli's bar, Grover's parallel
filaments and a square loop."""

import math

import pytest

from kipi.noise.extract.peec import loop_inductance, mutual, polyline, segment

MU0 = 4e-7 * math.pi


@pytest.mark.parametrize("l, w, t", [(10, 1, 0.035), (50, 0.2, 0.035), (5, 0.5, 0.0175)])
def test_bar_self_inductance(l, w, t):
    s = segment([0, 0, 0], [l, 0, 0], w, t)
    ref = MU0 * l * 1e-3 / (2 * math.pi) * (math.log(2 * l / (w + t)) + 0.5
                                            + 0.2235 * (w + t) / l)
    assert mutual(s, s) == pytest.approx(ref, rel=0.005)


def test_parallel_filaments():
    l, d = 20.0, 2.0
    a = segment([0, 0, 0], [l, 0, 0], 1e-4, 1e-4)
    b = segment([0, d, 0], [l, d, 0], 1e-4, 1e-4)
    ref = MU0 * l * 1e-3 / (2 * math.pi) * (math.log(l / d + math.sqrt(1 + l * l / d / d))
                                            - math.sqrt(1 + d * d / l / l) + d / l)
    assert mutual(a, b) == pytest.approx(ref, rel=1e-6)
    b_rev = segment([l, d, 0], [0, d, 0], 1e-4, 1e-4)
    assert mutual(a, b_rev) == pytest.approx(-ref, rel=1e-6)


def test_perpendicular_segments_do_not_couple():
    a = segment([0, 0, 0], [10, 0, 0], 0.1, 0.1)
    b = segment([0, 0, 0], [0, 10, 0], 0.1, 0.1)
    assert mutual(a, b) == 0.0


def test_square_loop():
    """L = 2μ0·s/π·(ln(s/r) − 0.774) for a square of side s in wire of radius r."""
    side, r = 10.0, 0.25
    w = r / 0.2235 / 2                    # a w × w bar has GMD radius r
    segs = polyline([[0, 0, 0], [side, 0, 0], [side, side, 0], [0, side, 0]], [w] * 4, w)
    ref = 2 * MU0 * side * 1e-3 / math.pi * (math.log(side / r) - 0.774)
    assert loop_inductance(segs) == pytest.approx(ref, rel=0.02)


def test_a_plane_under_a_loop_lowers_its_inductance():
    segs = polyline([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], [0.5] * 4, 0.035)
    free = loop_inductance(segs)
    far = loop_inductance(segs, z_plane=-5.0)
    near = loop_inductance(segs, z_plane=-0.2)
    assert near < far < free
    assert near < 0.4 * free
