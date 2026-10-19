"""A switcher's waveforms from closed-form Fourier series (#69), against the textbook buck
and boost results."""

import math

import numpy as np
import pytest

from kipi.noise.waveforms import (
    CapBranch, PhaseParts, harmonic_count, ifft_periodic, pwl_spectrum, ringing_response,
    synthesize,
)


def test_pwl_spectrum_matches_the_fft_of_its_samples():
    T = 1e-6
    t = [0.0, 0.05e-6, 0.3e-6, 0.37e-6]
    y = [0.0, 5.0, 5.0, 0.0]
    n = 4096
    ts = np.arange(n) * T / n
    samples = np.interp(ts, [*t, T], [*y, y[0]])
    ref = np.fft.fft(samples) / n
    k = np.arange(1, 40)
    mean, c = pwl_spectrum(t, y, T, k)
    assert mean == pytest.approx(ref[0].real, rel=1e-6)
    assert np.allclose(c, ref[1:40], atol=1e-4 * 5)


def test_ifft_rebuilds_the_waveform():
    T = 1.0
    t, y = [0.0, 0.5], [-1.0, 1.0]           # a triangle
    k = np.arange(1, 200)
    mean, c = pwl_spectrum(t, y, T, k)
    x = ifft_periodic(mean, c, 2048)
    assert x[0] == pytest.approx(-1.0, abs=0.01)
    assert x[1024] == pytest.approx(1.0, abs=0.01)
    assert np.mean(x) == pytest.approx(0.0, abs=1e-9)


def _ideal(c=10e-6):
    return CapBranch("C", c, 1e-9, 0.0)


def _buck(L=2.2e-6, iout=10.0, vin=12.0, vout=1.2, fsw=500e3, n=1, rise=1e-9, k=2000,
          cin=None, cout=None):
    duty = vout / vin
    return synthesize("buck", vin, vout, iout, vout * iout / vin, duty, fsw, rise, rise,
                      [PhaseParts(L)] * n, k, cin or [_ideal()], cout or [_ideal(100e-6)],
                      k * fsw)


def test_buck_inductor_ripple_and_average():
    wf = _buck()
    d = 0.1
    assert wf.ripple_a[0] == pytest.approx((12 - 1.2) * d / (2.2e-6 * 500e3))
    il = wf.phases[0].inductor
    assert il.mean == pytest.approx(10.0)
    x = il.samples(wf.samples_per_period())
    assert np.ptp(x) == pytest.approx(wf.ripple_a[0], rel=0.01)


def test_buck_switch_node_averages_to_the_output():
    wf = _buck()
    v = wf.phases[0].switch_node
    assert v.mean == pytest.approx(1.2, rel=1e-6)
    x = v.samples(wf.samples_per_period())
    assert x.max() == pytest.approx(12.0, rel=0.02)


def test_buck_input_capacitor_rms_current():
    """I_cin,rms = Iout·√(D(1−D) + D·r²/12), r the ripple over Iout."""
    wf = _buck()
    d, r = 0.1, wf.ripple_a[0] / 10.0
    expect = 10.0 * math.sqrt(d * (1 - d) + d * r * r / 12)
    assert wf.input_caps["C"].ac_rms() == pytest.approx(expect, rel=0.02)


def test_buck_output_ripple_voltage():
    """An ideal output capacitor: ΔV = ΔI / (8·fsw·C)."""
    wf = _buck(cout=[CapBranch("C2", 100e-6, 0.0, 0.0)])
    expect = wf.ripple_a[0] / (8 * 500e3 * 100e-6)
    x = wf.output_ripple.samples(wf.samples_per_period())
    assert np.ptp(x) == pytest.approx(expect, rel=0.03)


def test_boost_output_capacitor_rms_current():
    """With little ripple, I_cout,rms = Iout·√(D/(1−D))."""
    vin, vout, iout, fsw = 5.0, 12.0, 2.0, 500e3
    d = 1 - vin / vout
    wf = synthesize("boost", vin, vout, iout, iout * vout / vin, d, fsw, 1e-9, 1e-9,
                    [PhaseParts(100e-6)], 2000, [_ideal()], [_ideal(22e-6)], 2000 * fsw)
    assert wf.phases[0].inductor.mean == pytest.approx(iout * vout / vin)
    assert wf.ripple_a[0] == pytest.approx(vin * d / (100e-6 * fsw))
    assert wf.output_caps["C"].ac_rms() == pytest.approx(iout * math.sqrt(d / (1 - d)),
                                                         rel=0.03)
    # A boost's switch node is low while its switch is on: it averages Vout·(1−D).
    assert wf.phases[0].switch_node.mean == pytest.approx(vout * (1 - d), rel=1e-6)


def test_interleaved_phases_cancel_harmonics():
    """Two phases 180° apart: their summed inductor current has no odd harmonics."""
    wf = _buck(n=2, k=20)
    total = wf.phases[0].inductor.c + wf.phases[1].inductor.c
    odd, even = np.abs(total[0::2]), np.abs(total[1::2])
    assert odd.max() < 1e-9 * even.max() + 1e-12
    assert even[0] > 0.01


def test_currents_split_by_capacitor_impedance():
    """Two equal capacitors take half each; a bigger ESR takes less."""
    a, b = CapBranch("A", 10e-6, 1e-9, 0.002), CapBranch("B", 10e-6, 1e-9, 0.002)
    wf = _buck(cin=[a, b])
    assert wf.input_caps["A"].ac_rms() == pytest.approx(wf.input_caps["B"].ac_rms())
    c = CapBranch("C", 10e-6, 1e-9, 0.050)
    wf = _buck(cin=[a, c])
    assert wf.input_caps["C"].ac_rms() < 0.5 * wf.input_caps["A"].ac_rms()


def test_ringing_peaks_at_its_frequency():
    f = np.array([1e6, 100e6, 1e9])
    h = ringing_response(f, 100e6, 0.05)
    assert abs(h[0]) == pytest.approx(1.0, rel=1e-3)
    assert abs(h[1]) == pytest.approx(1 / (2 * 0.05), rel=1e-6)
    assert abs(h[2]) < 0.05


def test_ringing_overshoots_the_edge():
    wf = synthesize("buck", 12.0, 1.2, 5.0, 0.5, 0.1, 500e3, 1e-9, 1e-9,
                    [PhaseParts(2.2e-6, 150e6, 0.05)], 4000, [_ideal()], [_ideal()], 2e9)
    x = wf.phases[0].switch_node.samples(wf.samples_per_period())
    assert x.max() > 15.0                 # well past 12 V
    assert x.min() < -1.0


def test_harmonic_count():
    k, fmax, capped = harmonic_count(1e6, 2e-9, 3e-9, None, None, 100000)
    assert fmax == pytest.approx(math.ceil(10 / (math.pi * 2e-9) / 1e6) * 1e6)
    assert not capped
    k, fmax, capped = harmonic_count(100e3, 0.5e-9, 0.5e-9, None, None, 1000)
    assert capped and k == 1000 and fmax == pytest.approx(100e6)
    k, fmax, _ = harmonic_count(1e6, 1e-9, 1e-9, 2e9, None, 100000)
    assert fmax == pytest.approx(3e9)     # the 3 GHz ceiling
    k, fmax, _ = harmonic_count(1e6, 1e-9, 1e-9, None, 50e6, 100000)
    assert (k, fmax) == (50, 50e6)
