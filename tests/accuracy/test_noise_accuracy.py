"""Coupled Noise on the coupled_noise validation board (#69): each victim's dominant
mechanism and their ranking, and a scan that finds them unaided."""

import pytest

from kipi.noise.solver import solve_noise
from validation_boards import open_board


@pytest.fixture(scope="module")
def solved():
    snap, setup = open_board("coupled_noise")
    return {v.name: v for v in solve_noise(snap, setup, "U1").victims}


def test_each_victim_is_coupled_the_way_its_route_says(solved):
    assert solved["SIG_SW"].dominant == "capacitive"
    assert solved["SIG_LOOP"].dominant == "inductive"
    assert set(solved["SIG_LOOP"].peak_by) == {"capacitive", "inductive", "common"}


def test_the_plane_shields_the_buried_victim(solved):
    assert solved["SIG_BURIED"].peak_v < 0.05 * solved["SIG_LOOP"].peak_v


def test_the_ranking(solved):
    peaks = [solved[n].peak_v for n in ("SIG_LOOP", "SIG_SW", "SIG_BURIED")]
    assert peaks == sorted(peaks, reverse=True)


def test_a_scan_finds_the_victims_unaided():
    snap, setup = open_board("coupled_noise")
    setup.victims = []
    setup.noise.scan = True
    setup.noise.scan_top_n = 2
    setup.noise.board_model = False
    r = solve_noise(snap, setup, "U1")
    nets = [row.net for row in r.scan]
    assert set(nets) == {"SIG_SW", "SIG_LOOP", "SIG_BURIED"}
    assert nets[0] == "SIG_LOOP" and nets[-1] == "SIG_BURIED"
    assert [v.name for v in r.victims] == nets[:2]
    assert all(v.scanned for v in r.victims)


def test_one_victim_alone():
    snap, setup = open_board("coupled_noise")
    setup.noise.victims = "SIG_SW"
    setup.noise.board_model = False
    r = solve_noise(snap, setup, "U1")
    assert [v.name for v in r.victims] == ["SIG_SW"]
    setup.noise.victims = "none"
    assert solve_noise(snap, setup, "U1").victims == []
