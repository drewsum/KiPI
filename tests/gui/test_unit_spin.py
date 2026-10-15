"""Value fields take SI prefixes and units (#60)."""

import pytest

pytest.importorskip("qtpy")

from qtpy.QtCore import QLocale  # noqa: E402
from qtpy.QtGui import QValidator  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.dc.gui.pi_panel import _optional_volts, _spin  # noqa: E402
from kipi.gui.unit_spin import parse_value, split_unit  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def app():
    before = QLocale()
    QLocale.setDefault(QLocale.c())
    yield QApplication.instance() or QApplication([])
    QLocale.setDefault(before)


@pytest.mark.parametrize("suffix, unit", [
    (" µF", ("F", 1e-6)), (" mΩ", ("Ω", 1e-3)), (" kHz", ("Hz", 1e3)), (" MHz", ("Hz", 1e6)),
    (" A", ("A", 1.0)), (" mm", ("m", 1e-3)), (" nH", ("H", 1e-9)),
    (" %", (None, 1.0)), (" A/µs", (None, 1.0)), (" µΩ·cm", (None, 1.0)), ("", (None, 1.0)),
])
def test_split_unit(suffix, unit):
    assert split_unit(suffix) == unit


@pytest.mark.parametrize("text, suffix, value", [
    # Plain numbers are in the field's unit, with or without the unit written.
    ("4.7", " µF", 4.7), ("4.7 µF", " µF", 4.7), ("4.7µF", " µF", 4.7), ("1e-3", " µF", 1e-3),
    ("-2.5", " A", -2.5),
    # Prefixes, alone or with the unit in any common spelling.
    ("10n", " µF", 0.01), ("10nF", " µF", 0.01), ("10 nf", " µF", 0.01), ("100p", " µF", 1e-4),
    ("10uF", " µF", 10), ("1m", " µF", 1000), ("22μ", " µF", 22),
    ("2 mOhm", " mΩ", 2), ("2mohm", " mΩ", 2), ("1", " mΩ", 1), ("1 ohm", " mΩ", 1000),
    ("0.1Ω", " mΩ", 100), ("1k", " mΩ", 1e6), ("1 kOhm", " mΩ", 1e6), ("1 meg", " mΩ", 1e9),
    ("1.2 MHz", " kHz", 1200), ("500 Hz", " kHz", 0.5), ("2M", " kHz", 2000),
    ("3.3 V", " mV", 3300), ("250 mA", " A", 0.25), ("1.5nH", " nH", 1.5), ("1 µH", " nH", 1000),
    ("35 µm", " mm", 0.035), ("35um", " mm", 0.035),
    # The field's own unit left after the typed prefix.
    ("10n µF", " µF", 0.01), ("1k mΩ", " mΩ", 1e6),
    # Resistor-code style.
    ("4k7", " mΩ", 4.7e6), ("4R7", " mΩ", 4700), ("4m7", " mΩ", 4.7), ("2n2", " µF", 2.2e-3),
])
def test_parse_value(text, suffix, value):
    assert parse_value(text, suffix) == pytest.approx(value)


@pytest.mark.parametrize("text, suffix", [
    ("", " µF"), ("abc", " µF"), ("10x", " µF"), ("10 nH", " µF"), ("4R7", " µF"),
    ("10m", " %"), ("5 k", " A/µs"), ("1m", ""), ("10 MV", " mΩ"),
])
def test_parse_value_rejects(text, suffix):
    assert parse_value(text, suffix) is None


def test_typed_prefix_sets_value():
    s = _spin(0, 1e6, 1.0, 4, " µF", 0.1)
    s.lineEdit().setText("10n")
    s.interpretText()
    assert s.value() == pytest.approx(0.01)
    s.lineEdit().setText("4.7 nF")
    s.interpretText()
    assert s.value() == pytest.approx(0.0047)


def test_shown_decimals_grow_only_when_needed():
    s = _spin(0, 1e6, 3.3, 4, " µF", 0.1)
    assert s.text() == "3.3000 µF"
    s.setValue(1e-5)            # 10 pF
    assert s.text() == "0.00001 µF"
    assert s.value() == pytest.approx(1e-5)
    whole = _spin(0, 100, 90, 0, " %", 1)
    assert whole.text() == "90 %"


def test_validate_states():
    s = _spin(0, 1000, 1.0, 3, " mΩ", 1)
    assert s.validate("2 mOhm", 0)[0] == QValidator.Acceptable
    assert s.validate("2 mO", 0)[0] == QValidator.Intermediate     # still typing "mOhm"
    assert s.validate("5k", 0)[0] == QValidator.Intermediate       # above the range
    assert s.validate("1?", 0)[0] == QValidator.Invalid


def test_bad_text_keeps_value():
    s = _spin(0, 1e6, 1.0, 4, " µF", 0.1)
    s.lineEdit().setText("10x")
    s.interpretText()
    assert s.value() == pytest.approx(1.0)


def test_optional_field_still_reads_not_set():
    s = _optional_volts()
    assert s.text() == "not set"
    assert s.validate("not set", 0)[0] == QValidator.Acceptable
    s.lineEdit().setText("1.8 V")
    s.interpretText()
    assert s.value() == pytest.approx(1.8)
    s.lineEdit().setText("900m")
    s.interpretText()
    assert s.value() == pytest.approx(0.9)


def test_decimal_comma_locale():
    s = _spin(0, 1e6, 1.0, 4, " µF", 0.1)
    s.setLocale(QLocale(QLocale.German, QLocale.Germany))
    assert s.text() == "1,0000 µF"
    s.lineEdit().setText("4,7n")
    s.interpretText()
    assert s.value() == pytest.approx(0.0047)
    s.lineEdit().setText("2.2u")            # a point still reads as a decimal point
    s.interpretText()
    assert s.value() == pytest.approx(2.2)
