"""Resistor values read from footprint value fields."""

import pytest

from kipi.core.model.board import Footprint
from kipi.core.model.parts import parse_resistance, resistor_ohms
from synthetic import make_board


@pytest.mark.parametrize("text, ohms", [
    ("0.005", 0.005), ("5m", 0.005), ("5mR", 0.005), ("5 mΩ", 0.005), ("10 mohm", 0.01),
    ("10mOhms", 0.01), ("100", 100), ("100R", 100), ("100Ω", 100), ("4.7k", 4700),
    ("4k7", 4700), ("0R1", 0.1), ("R005", 0.005), ("1M", 1e6), ("1M5", 1.5e6), ("0", 0.0),
    ("10m 1% 0603", 0.01), ("0.01/1W", 0.01), ("2.2 kΩ", 2200),
])
def test_parses_resistor_values(text, ohms):
    assert parse_resistance(text) == pytest.approx(ohms)


@pytest.mark.parametrize("text", ["", "DNP", "10uF", "10mm", "1.5k2", "R", "BLM18"])
def test_rejects_non_resistances(text):
    assert parse_resistance(text) is None


def test_only_resistor_refs_are_read():
    snap = make_board()
    snap.footprints += [Footprint("f3", "R7", "10m", (0, 0), "top"),
                        Footprint("f4", "L1", "10m", (0, 0), "top"),
                        Footprint("f5", "RN1", "10m", (0, 0), "top")]
    assert resistor_ohms(snap, "R7") == pytest.approx(0.01)
    assert resistor_ohms(snap, "L1") is None
    assert resistor_ohms(snap, "RN1") is None
    assert resistor_ohms(snap, "R99") is None
    assert resistor_ohms(None, "R7") is None
