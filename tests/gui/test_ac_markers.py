"""PI Models markers in AC mode (#84): the VRMs, series links, probes and modelled
capacitors an impedance sweep uses, marked on their pads. The DC loads are left out — the
probes, where the impedance is looked into, take their place."""

import numpy as np
import pytest

from kipi.ac.model import CapModel, Probe
from kipi.ac.viz.markers import ac_markers
from kipi.dc.pi.model import DcLink, PinRef
from kipi.dc.viz.markers import LIFT_MM, pi_markers
from phase_boards import T, board, two_phase
from shapely.geometry import box
from tree_boards import B, pad

TOP = 1.5 + 2 * T + LIFT_MM
BOTTOM_CU_TOP = T + LIFT_MM


def _by_name(markers):
    return {m.name: m for m in markers}


def _setup(**kw):
    setup = two_phase(**kw)
    setup.probes.append(Probe("U2 core", PinRef("U2", "VOUT", ["1"]),
                              PinRef("U2", "GND", ["2"])))
    return setup


def test_the_probes_are_marked_in_place_of_the_loads():
    snap, setup = board(), _setup(ground=True)
    m = _by_name(ac_markers(snap, setup, {"VOUT", "GND"}))
    assert set(m) == {"VRM U1", "VRM J1", "Probe U2 core"}
    assert "Load U2" in _by_name(pi_markers(snap, setup, {"VOUT", "GND"}))   # DC mode's
    probe = m["Probe U2 core"]
    assert probe.kind == "probe" and probe.item == "U2 core"
    np.testing.assert_allclose(probe.power, [[5.5, 0.5, TOP]])          # the supply pins
    np.testing.assert_allclose(probe.ground, [[5.5, 0.5, BOTTOM_CU_TOP]])
    np.testing.assert_allclose(m["VRM U1"].power, [[0.5, 0.5, TOP], [19.5, 0.5, TOP]])


def test_a_probe_off_the_nets_shown_is_not_marked():
    snap, setup = board(), _setup()
    assert "Probe U2 core" in _by_name(ac_markers(snap, setup, {"VOUT"}))
    assert "Probe U2 core" not in _by_name(ac_markers(snap, setup, {"VIN"}))
    assert ac_markers(snap, setup, set()) == []
    assert ac_markers(None, setup, {"VOUT"}) == [] and ac_markers(snap, None, {"VOUT"}) == []
    assert not [p for p in ac_markers(snap, two_phase(), {"VOUT"}) if p.kind == "probe"]


def test_series_links_and_modelled_capacitors_are_marked_too():
    snap, setup = board(), _setup()
    setup.links.append(DcLink("FB1", PinRef("L1", "VOUT", ["2"]),
                              PinRef("Q1", "VIN", ["1"]), 0.01))
    snap.pads.append(pad("c1_1", "VOUT", "C1", "1", box(2, 0, 3, 1)))
    snap.pads.append(pad("c1_2", "GND", "C1", "2", box(2, 0, 3, 1), B))
    setup.caps.append(CapModel("10u", 10e-6, 1e-9, 0.005, ["C1"]))
    names = [m.name for m in ac_markers(snap, setup, {"VOUT", "GND"})]
    assert "Link FB1" in names and names[-1] == "C1"            # capacitors come last
    link = _by_name(ac_markers(snap, setup, {"VOUT", "VIN"}))["Link FB1"]
    assert link.kind == "link" and len(link.power) == 2 and len(link.ground) == 0
    cap = _by_name(ac_markers(snap, setup, {"VOUT", "GND"}))["C1"]
    np.testing.assert_allclose(cap.power, [[2.5, 0.5, TOP]])
    np.testing.assert_allclose(cap.ground, [[2.5, 0.5, BOTTOM_CU_TOP]])


def test_a_probe_without_pins_is_skipped():
    """A probe being edited may not name its pads yet."""
    snap, setup = board(), two_phase()
    setup.probes.append(Probe("new", PinRef("", ""), PinRef("", "")))
    assert [m.name for m in ac_markers(snap, setup, {"VOUT", "GND"})] == \
           [m.name for m in ac_markers(snap, two_phase(), {"VOUT", "GND"})]


@pytest.mark.parametrize("nets", [{"VOUT"}, {"VOUT", "GND"}, {"VIN", "GND"}])
def test_every_marker_has_a_pad_on_a_shown_net(nets):
    snap, setup = board(), _setup(ground=True)
    for m in ac_markers(snap, setup, nets):
        assert len(m.power) or len(m.ground)
