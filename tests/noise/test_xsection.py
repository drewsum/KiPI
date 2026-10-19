"""The 2D cross-section field solve (#69) against closed forms: microstrip
(Hammerstad–Jensen), stripline (Wheeler), two wires over ground (Maxwell's potential
coefficients) and a wide plate."""

import math

import numpy as np
import pytest

from kipi.noise.extract.xsection import (
    AGGRESSOR, GROUND, VICTIM, Band, Cache, Rect, solve,
)

EPS0 = 8.8541878128e-12


def hammerstad_jensen(w, h, er):
    u = w / h
    a = (1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49
         + math.log(1 + (u / 18.1) ** 3) / 18.7)
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    ee = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    f = 6 + (2 * math.pi - 6) * math.exp(-(30.666 / u) ** 0.7528)
    return 60 * math.log(f / u + math.sqrt(1 + 4 / u ** 2)) / math.sqrt(ee), ee


@pytest.mark.parametrize("w, h, er", [(0.3, 0.2, 4.5), (0.1, 0.1, 4.0), (1.0, 0.2, 4.5),
                                      (0.2, 0.5, 3.5)])
def test_microstrip(w, h, er):
    W = 8.0
    rects = [Rect(-W, W, -0.035, 0.0, GROUND), Rect(-w / 2, w / 2, h, h + 0.001, VICTIM)]
    r = solve(rects, [Band(0.0, h, er)], (-W, W, -0.5, 8.0))
    z0, ee = hammerstad_jensen(w, h, er)
    assert r.z0_ohm == pytest.approx(z0, rel=0.03)
    assert r.er_eff == pytest.approx(ee, rel=0.01)
    assert r.cm == {}


@pytest.mark.parametrize("w, b", [(0.2, 0.4), (0.1, 0.3)])
def test_stripline(w, b):
    er, W = 4.2, 6.0
    rects = [Rect(-W, W, -0.035, 0.0, GROUND), Rect(-W, W, b, b + 0.035, GROUND),
             Rect(-w / 2, w / 2, b / 2, b / 2 + 0.001, VICTIM)]
    r = solve(rects, [Band(0, b, er)], (-W, W, -0.5, b + 0.5))
    we = w if w / b >= 0.35 else w - (0.35 - w / b) ** 2 * b
    z0 = 30 * math.pi / math.sqrt(er) * b / (we + 0.441 * b)
    assert r.z0_ohm == pytest.approx(z0, rel=0.03)
    assert r.er_eff == pytest.approx(er, rel=1e-6)          # fully embedded


def _two_wires(d, swap=False):
    s, h, W = 0.1, 1.0, 20.0
    v, a = (AGGRESSOR, VICTIM) if swap else (VICTIM, AGGRESSOR)
    rects = [Rect(-W, W, -0.035, 0.0, GROUND),
             Rect(-s / 2, s / 2, h - s / 2, h + s / 2, v),
             Rect(d - s / 2, d + s / 2, h - s / 2, h + s / 2, a)]
    return solve(rects, [], (-W, W, -0.5, 20.0))


def test_two_wires_over_ground():
    """Square wires of side s are round wires of radius 0.59·s."""
    s, h, d = 0.1, 1.0, 1.0
    a = 0.5902 * s
    p11 = math.log(2 * h / a)
    p12 = math.log(math.sqrt(d * d + 4 * h * h) / d)
    c = np.linalg.inv(np.array([[p11, p12], [p12, p11]]) / (2 * math.pi * EPS0)) * 1e-3
    r = _two_wires(d)
    assert r.c_f_per_mm == pytest.approx(c[0, 0], rel=0.03)
    assert r.cm[AGGRESSOR] == pytest.approx(-c[0, 1], rel=0.03)


def test_mutual_capacitance_is_reciprocal_and_falls_with_distance():
    near, back = _two_wires(1.0), _two_wires(1.0, swap=True)
    assert near.cm[AGGRESSOR] == pytest.approx(back.cm[AGGRESSOR], rel=0.02)
    far = _two_wires(4.0)
    assert far.cm[AGGRESSOR] < 0.3 * near.cm[AGGRESSOR]


def test_wide_plate_is_a_parallel_plate_plus_fringe():
    w, h, er, W = 10.0, 0.2, 4.5, 20.0
    rects = [Rect(-W, W, -0.035, 0.0, GROUND), Rect(-w / 2, w / 2, h, h + 0.035, VICTIM)]
    r = solve(rects, [Band(0, h, er)], (-W, W, -0.5, 10.0))
    plate = EPS0 * 1e-3 * er * w / h
    assert plate < r.c_f_per_mm < 1.08 * plate


def test_a_plane_between_shields_the_aggressor():
    W = 6.0
    victim = Rect(-0.1, 0.1, -0.4, -0.365, VICTIM)
    agg = Rect(-0.5, 0.5, 0.2, 0.235, AGGRESSOR)
    bands = [Band(-0.365, 0.0, 4.5), Band(0.0, 0.2, 4.5)]
    open_ = solve([victim, agg], bands, (-W, W, -3, 3))
    plane = Rect(-W, W, -0.035, 0.0, GROUND)
    shielded = solve([victim, agg, plane], bands, (-W, W, -3, 3))
    assert shielded.cm.get(AGGRESSOR, 0.0) < 1e-3 * open_.cm[AGGRESSOR]


def test_the_cache_reuses_a_cut():
    cache = Cache()
    rects = [Rect(-5, 5, -0.035, 0, GROUND), Rect(-0.1, 0.1, 0.2, 0.235, VICTIM)]
    a = cache.solve(rects, [Band(0, 0.2, 4.5)], (-5, 5, -1, 3))
    nudged = [rects[0], Rect(-0.1001, 0.1001, 0.2, 0.235, VICTIM)]   # within 10 µm
    b = cache.solve(nudged, [Band(0, 0.2, 4.5)], (-5, 5, -1, 3))
    assert a is b and (cache.hits, cache.misses) == (1, 1)
