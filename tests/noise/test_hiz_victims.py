"""Undriven (high-impedance) victims, the scan's default type (#158)."""

import pytest

from kipi.noise.model import (
    CUSTOM, VICTIM_PRESETS, NoiseSettings, scan_victim, settings_from_dict,
)
from kipi.noise.solver import solve_noise
from validation_boards import open_board


def test_a_scanned_net_is_undriven_unless_the_scan_says_otherwise():
    s = NoiseSettings()
    assert s.scan_preset == "hiz"
    v = scan_victim(s, "SIG")
    p = VICTIM_PRESETS["hiz"]
    assert (v.preset, v.driver_ohm, v.receiver_ohm) == ("hiz", p.driver_ohm, p.receiver_ohm)
    assert [st.driver_ohm for st in v.states()] == [p.driver_ohm] * 2
    s.scan_preset = "cmos"
    assert scan_victim(s, "SIG").driver_ohm == VICTIM_PRESETS["cmos"].driver_ohm
    s.scan_preset, s.scan_driver_ohm = CUSTOM, 12.0
    v = scan_victim(s, "SIG")
    assert (v.preset, v.driver_ohm) == (CUSTOM, 12.0)
    # An older setup file, with only a scan driver resistance, scans undriven.
    assert settings_from_dict({"scan_driver_ohm": 47.0}).scan_preset == "hiz"


def _scan(preset):
    snap, setup = open_board("coupled_noise")
    keep = next(v for v in setup.victims if v.name == "SIG_SW")
    setup.victims = [keep]
    setup.noise.scan = True
    setup.noise.scan_top_n = 3
    setup.noise.board_model = False
    setup.noise.scan_preset = preset
    return keep, solve_noise(snap, setup, "U1")


@pytest.fixture(scope="module")
def scans():
    return {p: _scan(p) for p in ("hiz", "cmos")}


def test_scanned_nets_are_solved_undriven_and_a_victim_keeps_its_type(scans):
    keep, r = scans["hiz"]
    by = {v.name: v for v in r.victims}
    assert set(by) == {"SIG_SW", "SIG_LOOP", "SIG_BURIED"}
    assert not by["SIG_SW"].scanned and by["SIG_LOOP"].scanned
    assert "SIG_SW" not in {row.net for row in r.scan}    # a victim is not scanned again
    assert keep.preset != "hiz"


def test_an_undriven_net_picks_up_more_by_capacitance_but_never_more_than_the_swing(scans):
    hiz = {row.net: row for row in scans["hiz"][1].scan}
    cmos = {row.net: row for row in scans["cmos"][1].scan}
    swing = scans["hiz"][1].operating.vin
    for net in hiz:
        assert hiz[net].capacitive_v > cmos[net].capacitive_v
        assert hiz[net].capacitive_v < swing
        assert hiz[net].inductive_v == pytest.approx(cmos[net].inductive_v)
