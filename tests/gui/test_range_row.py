"""The min / max range row of loads' tolerance and VRMs' input range and DC accuracy: in
volts, volts from nominal or % from nominal (#131)."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.dc.gui.pi_panel import RangeRow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_switching_the_unit_converts_through_the_nominal_voltage(app):
    nominal = [3.3]
    row = RangeRow(lambda: nominal[0])
    assert [row.unit.itemText(i) for i in range(row.unit.count())] == ["V", "±V", "%"]
    row.set_values(-5.0, None, True)
    assert (row.pct(), row.dv()) == (True, False)
    row.unit.setCurrentText("±V")
    assert (row.pct(), row.dv()) == (False, True)
    assert row.values()[0] == pytest.approx(-0.165) and row.values()[1] is None
    assert row.min.suffix() == " V"
    row.unit.setCurrentText("V")
    assert row.values()[0] == pytest.approx(3.135)
    row.unit.setCurrentText("%")
    assert row.values()[0] == pytest.approx(-5.0)
    nominal[0] = None                        # no VRM on the rail: relative values clear
    row.unit.setCurrentText("±V")
    assert row.values() == (None, None)
    row.set_values(-0.1, 0.1, False, True)
    row.unit.setCurrentText("V")
    assert row.values() == (None, None)
