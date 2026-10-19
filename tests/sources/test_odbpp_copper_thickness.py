"""A copper layer's thickness from its ODB++ attributes, as KiCad and Altium write them."""

import pytest

from kipi.sources.odbpp.reader import _copper_thickness

MM, INCH = 1.0, 25.4


@pytest.mark.parametrize("attrs,unit,mm", [
    # KiCad: the copper's own thickness, precise enough, in line with its weight.
    ({".layer_dielectric": "0.0350", ".copper_weight": "1"}, MM, 0.035),
    # KiCad at 2 decimals: 0.03 says less than the weight.
    ({".layer_dielectric": "0.03", ".copper_weight": "1"}, MM, 0.035),
    # Altium: the neighbouring dielectric's thickness (0.052362 in = 1.33 mm) beside a
    # 1 oz weight - the weight is the copper.
    ({".layer_dielectric": "0.052362", ".copper_weight": "1"}, INCH, 0.035),
    ({".layer_dielectric": "0.000984", ".copper_weight": "1.05711176"}, INCH,
     1.05711176 * 0.035),
    # The weight alone, in an inch file: millimetres, not scaled again.
    ({".copper_weight": "2"}, INCH, 0.070),
    # A thickness alone, in inches.
    ({".copper_thickness": "0.0014"}, INCH, 0.0014 * 25.4),
    ({}, MM, None),
])
def test_copper_thickness(attrs, unit, mm):
    got = _copper_thickness(attrs, unit)
    assert got == (None if mm is None else pytest.approx(mm))
