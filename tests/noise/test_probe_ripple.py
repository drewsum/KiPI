"""The aggressor's ripple at AC probes (#157): the board model's voltage between each
chosen probe's rail and ground pins."""

import numpy as np

from kipi.ac.model import Probe
from kipi.dc.pi.model import PinRef
from kipi.noise.model import Aggressor, Victim, aggressor_from_dict
from kipi.noise.solver import solve_noise
from noise_boards import buck_board
from test_board_model import _setup


def _probes(setup):
    setup.probes = [
        # At the inductor's output pin against the power stage's ground: the board
        # model's own output ripple.
        Probe("AT_L1", PinRef("L1", "VOUT", ["2"]), PinRef("U1", "GND", ["2"])),
        Probe("LOAD", PinRef("U2", "VOUT", ["1"]), PinRef("U2", "GND", ["2"])),
        Probe("VIN", PinRef("C1", "VIN", ["1"]), PinRef("C1", "GND", ["2"])),
        Probe("OFF_RAIL", PinRef("U3", "SIG", ["1"]), PinRef("U3", "GND", ["2"])),
    ]


def test_the_ripple_at_probes():
    snap = buck_board(victim_gap=0.2)
    setup = _setup(snap, [Victim("SIG")])
    _probes(setup)
    plain = solve_noise(snap, setup, "U1 buck")
    setup.aggressors[0].ripple_probes = ["AT_L1", "LOAD", "VIN", "OFF_RAIL", "GONE"]
    r = solve_noise(snap, setup, "U1 buck")
    wf = r.waveforms
    assert set(wf.probe_ripple) == {"AT_L1", "LOAD", "VIN"}
    assert np.allclose(wf.probe_ripple["AT_L1"].c, wf.output_ripple.c)
    n = wf.samples_per_period()
    load = np.ptp(wf.probe_ripple["LOAD"].samples(n))
    assert 0 < load < 2 * np.ptp(wf.output_ripple.samples(n))
    # The input rail's ripple is the larger: a buck draws pulses from it.
    assert np.ptp(wf.probe_ripple["VIN"].samples(n)) > load
    assert any("Probe OFF_RAIL" in w and "not one of the aggressor's rails" in w
               for w in r.warnings)
    assert any("No AC probe named GONE" in w for w in r.warnings)
    assert any(x.startswith("Ripple at probe LOAD:") and "mV p-p" in x
               for x in r.summary_lines())
    # The probes change nothing else: the victims' ground coupling is as before (but for
    # the mesh, refined at the probes' pads).
    a, b = r.victims[0].receiver_v["common"].c, plain.victims[0].receiver_v["common"].c
    assert np.allclose(a, b, rtol=0.02, atol=0.02 * np.abs(b).max())
    assert not plain.waveforms.probe_ripple


def test_probe_ripple_needs_the_board_model():
    snap = buck_board()
    setup = _setup(snap, [], board_model=False)
    _probes(setup)
    setup.aggressors[0].ripple_probes = ["LOAD"]
    r = solve_noise(snap, setup, "U1 buck")
    assert not r.waveforms.probe_ripple
    assert any("needs the board model" in w for w in r.warnings)


def test_the_choice_is_kept():
    a = Aggressor("A", ripple_probes=["LOAD"])
    import dataclasses
    d = dataclasses.asdict(a)
    assert aggressor_from_dict(d, lambda x: None).ripple_probes == ["LOAD"]
    assert aggressor_from_dict({"name": "B"}, lambda x: None).ripple_probes == []


def test_the_report_shows_them(tmp_path):
    from kipi.noise.html_report import write_noise_report
    snap = buck_board()
    setup = _setup(snap, [])
    _probes(setup)
    setup.aggressors[0].ripple_probes = ["LOAD"]
    r = solve_noise(snap, setup, "U1 buck")
    path = write_noise_report(tmp_path / "noise.html", r, snap, setup)
    text = path.read_text(encoding="utf-8")
    assert "Ripple (mV p-p)" in text and "Ripple at probe LOAD" in text
