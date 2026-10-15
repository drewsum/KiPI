"""AC solves of the validation boards against hand calculations (#70).

The rail and ground planes are a parallel-plate capacitor, C = ε₀εrA/h, and a cavity
whose first mode resonates at c/(2a√εr). A capacitor on the planes is a series RLC: its
ESR at the series resonance, with its ESL plus the mounting inductance. A ferrite (R + L
series link) in front of a small plane is lumped parts, and resonates with that plane's
capacitance at 1/(2π√(LC)).
"""

import math

import numpy as np
import pytest

from validation_boards import boards, open_board
from kipi.ac.solver import solve_ac
from kipi.ac.solver.physics import EPS0_F_PER_MM

C0_MM_S = 299_792_458e3          # speed of light, mm/s


def _solve(name):
    snap, setup = open_board(name)
    return snap, setup, solve_ac(snap, setup, setup.ac.rail, setup.ac.ground)


def _plate(area_mm2, setup):
    """Parallel-plate capacitance of ``area_mm2`` of rail over ground, complex with the
    dielectric's loss tangent."""
    c = EPS0_F_PER_MM * boards.ER * area_mm2 / boards.AcPlane.H
    return c * (1 - 1j * setup.ac.loss_tangent)


def _hole_area():
    return boards.AcPlane.HOLE ** 2       # each part's GND pad cut-out in the rail plane


def _at(result, f):
    i = int(np.argmin(np.abs(result.freqs - f)))
    return result.freqs[i], result.probes[0].z[i]


def _peaks(result, lo, hi):
    """Local maxima of |Z| between ``lo`` and ``hi``, largest first."""
    m, f = np.abs(result.probes[0].z), result.freqs
    k = [i for i in range(1, len(m) - 1)
         if m[i] > m[i - 1] and m[i] > m[i + 1] and lo <= f[i] <= hi]
    return sorted(((f[i], m[i]) for i in k), key=lambda p: -p[1])


# ---------------------------------------------------------------- plane pair

@pytest.fixture(scope="module")
def plane():
    return _solve("ac_plane")


def test_plane_pair_is_a_parallel_plate_capacitor(plane):
    _, setup, res = plane
    g = boards.AcPlane
    c = _plate(g.SIZE ** 2 - _hole_area(), setup)
    assert res.plane_capacitance_f == pytest.approx(c.real, rel=1e-3)
    for f in (1e3, 1e5, 1e6):
        f, z = _at(res, f)
        assert z == pytest.approx(1 / (2j * math.pi * f * c), rel=5e-3)


def test_plane_pair_first_cavity_resonance(plane):
    _, _, res = plane
    g = boards.AcPlane
    f10 = C0_MM_S / (2 * g.SIZE * math.sqrt(boards.ER))
    (f_peak, _), *_ = _peaks(res, 0.5 * f10, 1.2 * f10)
    assert f_peak == pytest.approx(f10, rel=1.5e-2)


# ---------------------------------------------------------------- decoupling capacitor

@pytest.fixture(scope="module")
def decap():
    return _solve("ac_decap")


def test_decap_low_frequency_is_both_capacitances(decap):
    _, setup, res = decap
    g = boards.AcDecap
    cp = _plate(g.SIZE ** 2 - 2 * _hole_area(), setup)
    assert res.plane_capacitance_f == pytest.approx(cp.real, rel=1e-3)
    for f in (1e3, 1e4):
        f, z = _at(res, f)
        w = 2 * math.pi * f
        expected = 1 / (1 / (g.ESR + 1 / (1j * w * g.C)) + 1j * w * cp)
        assert z == pytest.approx(expected, rel=5e-3)


def test_decap_is_a_series_rlc(decap):
    _, setup, res = decap
    g = boards.AcDecap
    cp = _plate(g.SIZE ** 2 - 2 * _hole_area(), setup).real
    # Mounted inductance: X = ωL - 1/(ωC) between the series resonance and the planes'.
    ls = []
    for f in (5e6, 1e7, 2e7):
        f, z = _at(res, f)
        w = 2 * math.pi * f
        ls.append((z.imag + 1 / (w * (g.C + cp))) / w)
    assert max(ls) / min(ls) < 1.03
    mounted = float(np.mean(ls))
    assert g.ESL < mounted < g.ESL + 2e-9            # the ESL plus pads, vias and planes
    # Series resonance with that inductance, at the ESR plus a little copper.
    m = np.abs(res.probes[0].z)
    i = int(np.argmin(m))
    srf = 1 / (2 * math.pi * math.sqrt(mounted * g.C))
    assert res.freqs[i] == pytest.approx(srf, rel=3e-2)
    assert g.ESR <= m[i] <= 1.15 * g.ESR


# ---------------------------------------------------------------- ferrite

@pytest.fixture(scope="module")
def ferrite():
    return _solve("ac_ferrite")


def _ferrite_lumped(setup, f):
    g = boards.AcFerrite
    w = 2j * math.pi * f
    c_a = _plate(g.SIZE_A ** 2 - _hole_area(), setup)
    c_b = _plate(g.SIZE_B ** 2 - _hole_area(), setup)
    z_cap = g.ESR + w * g.ESL + 1 / (w * g.C)
    z_a = 1 / (1 / z_cap + w * c_a)                  # bulk capacitor || +3V3 plane
    z_link = g.R_FB + w * g.L_FB
    return 1 / (1 / (z_link + z_a) + w * c_b), c_b   # || +3V3_F plane


def test_ferrite_board_matches_its_lumped_parts(ferrite):
    _, setup, res = ferrite
    expected, c_b = _ferrite_lumped(setup, res.freqs)
    f0 = 1 / (2 * math.pi * math.sqrt(boards.AcFerrite.L_FB * c_b.real))
    ratio = np.abs(res.probes[0].z) / np.abs(expected)
    # Away from the peak (checked below), up to 100 MHz: above that, the probe's own
    # mounting inductance (a fraction of a nH) starts to cancel the plane's 1/(ωC).
    away = (np.abs(np.log(res.freqs / f0)) > 0.2) & (res.freqs <= 100e6)
    assert np.all(np.abs(ratio[away] - 1) < 0.01), (ratio[away].min(), ratio[away].max())
    high = res.freqs > 100e6
    assert np.all((ratio[high] < 1) & (ratio[high] > 0.9))


def test_ferrite_resonates_with_the_filtered_plane(ferrite):
    _, setup, res = ferrite
    expected, c_b = _ferrite_lumped(setup, res.freqs)
    f0 = 1 / (2 * math.pi * math.sqrt(boards.AcFerrite.L_FB * c_b.real))
    (f_peak, z_peak), = _peaks(res, f0 / 3, 3 * f0)
    assert f_peak == pytest.approx(f0, rel=1.5e-2)
    assert z_peak == pytest.approx(np.abs(expected).max(), rel=0.05)
