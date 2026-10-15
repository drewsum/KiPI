"""Copper thickness display units in the PI panel."""

import pytest

pytest.importorskip("qtpy")

from kipi.dc.gui.pi_panel import format_thickness  # noqa: E402


@pytest.mark.parametrize("mm, unit, text", [
    (0.035, "µm", "35.0 µm"),
    (0.0175, "µm", "17.5 µm"),
    (0.035, "mil", "1.38 mil"),
    (0.0254, "mil", "1.00 mil"),
    (0.035, "oz", "1 oz"),
    (0.0175, "oz", "0.5 oz"),
    (0.070, "oz", "2 oz"),
])
def test_format_thickness(mm, unit, text):
    assert format_thickness(mm, unit) == text
