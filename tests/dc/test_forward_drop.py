# Copyright (c) 2026 Tomachie LLC.
"""A series diode's forward drop from the forward-voltage figures of its datasheet."""

import pytest

from kipi.dc.pi.importers.pdn_figures import forward_drop


def diode(*figs):
    return {"class": "diode", "datasheet_figures": list(figs)}


def test_one_figure_is_a_constant_drop():
    vf, vf_max, slope, notes = forward_drop(diode(
        {"spec": "v_forward", "max": "0.85", "unit": "V", "if": "5A", "temperature": "25C"},
        {"spec": "i_forward_avg", "max": "5", "unit": "A"}))
    assert (vf, vf_max, slope) == (0.85, None, 0.0)
    assert "at 5 A" in notes[0] and "drops less" in notes[0]


def test_two_figures_give_a_drop_and_a_slope():
    vf, vf_max, slope, _ = forward_drop(diode(
        {"spec": "vf", "typ": "0.40", "unit": "V", "if": "1 A"},
        {"spec": "vf", "typ": "0.50", "unit": "V", "if": "3A"},
        {"spec": "vfm", "max": "0.60", "unit": "V", "if": "3A"}))
    assert slope == pytest.approx(0.05)                 # 0.1 V over 2 A
    assert vf == pytest.approx(0.35)                    # 0.40 V - 1 A x 50 mOhm
    assert vf_max == pytest.approx(0.45)                # the max figure 0.1 V above typical


def test_milliamp_test_currents_and_no_figures():
    _, _, slope, _ = forward_drop(diode({"spec": "vf", "typ": "0.30", "unit": "V", "if": "100mA"},
                                        {"spec": "vf", "typ": "0.40", "unit": "V", "if": "1A"}))
    assert slope == pytest.approx(0.1 / 0.9)
    assert forward_drop(diode({"spec": "i_leakage", "max": "1", "unit": "mA"})) == \
        (None, None, 0.0, [])


def test_a_vf_curve_is_fitted_where_the_diode_conducts():
    """The 25 C curve, a line through its points above a tenth of its highest current;
    a printed maximum lifts it for the worst case."""
    d = diode({"spec": "vf", "max": "0.60", "unit": "V", "if": "3A"})
    d["vf_curves"] = [
        {"label": "TJ=125C", "points": [[0.1, 0.20], [1, 0.30], [3, 0.40]]},
        {"label": "TJ=25C", "points": [[0.01, 0.15], [0.5, 0.375], [1, 0.40], [3, 0.50]],
         "page": 3}]
    vf, vf_max, slope, notes = forward_drop(d)
    assert slope == pytest.approx(0.05, rel=1e-6)      # through 0.5, 1 and 3 A
    assert vf == pytest.approx(0.35, rel=1e-6)
    assert vf_max == pytest.approx(0.60 - 3 * 0.05)
    assert "TJ=25C" in notes[0] and "page 3" in notes[0]
