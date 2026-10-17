"""AC solves at a temperature (#77): the copper's resistivity and the capacitance of models
with a dielectric follow a fixed board temperature, or the copper temperatures of the last
DC thermal co-simulation."""

import os
from types import SimpleNamespace

import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.html_report import write_ac_report
from kipi.ac.model import TEMP_CURVES, CapModel
from kipi.ac.solver import solve_ac
from kipi.dc.pi.model import PISetup

# A small board with a coarse mesh: these tests are about the capacitor models, and the
# field solve on a bigger one takes ~20 s without PARDISO.
SMALL = dict(max_edge_mm=2.0, min_edge_mm=0.15, grading=0.6)


def test_temperature_curves():
    x7r = CapModel("X7R", 10e-6, dielectric="X7R")
    assert x7r.at_temperature(None) == 1.0 and x7r.at_temperature(25.0) == 1.0
    assert x7r.at_temperature(125.0) == pytest.approx(0.88)
    assert x7r.at_temperature(105.0) == pytest.approx(0.92)       # between the points
    assert x7r.at_temperature(200.0) == pytest.approx(0.88)       # held past the end
    assert CapModel("C0G", 1e-9, dielectric="C0G").at_temperature(125.0) == 1.0
    assert CapModel("none", 1e-6).at_temperature(125.0) == 1.0
    assert CapModel("y", 1e-6, dielectric="Y5V").at_temperature(85.0) == pytest.approx(0.25)
    for name, curve in TEMP_CURVES.items():                      # rated at 25 °C
        ts, pct = zip(*curve)
        assert np.interp(25.0, ts, pct) == 0.0, name


def test_temperature_aging_and_bias_combine():
    m = CapModel("X7R", 10e-6, dc_bias=True, rated_v=6.3, kept_at_rated_pct=40,
                 dielectric="X7R", aging_pct_per_decade=2.0)
    assert m.capacitance_at(6.3, 1e4, 125.0) == pytest.approx(10e-6 * 0.4 * 0.98 * 0.88)


def test_settings_are_saved_and_old_setups_load():
    setup = PISetup()
    setup.ac.temperature, setup.ac.temperature_c = "thermal", 60.0
    back = PISetup.from_dict(setup.to_dict())
    assert back.ac.temperature == "thermal" and back.ac.temperature_c == 60.0
    d = setup.to_dict()
    del d["ac"]["temperature"], d["ac"]["temperature_c"]
    assert PISetup.from_dict(d).ac.temperature == "none"


@pytest.fixture(scope="module")
def board():
    return plane_pair(20.0, caps=[("C1", "10u X7R", (4.0, 14.0)),
                                  ("C2", "10u X7R", (14.0, 14.0))])


def _setup(snap, **ac):
    setup = plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e8, points_per_decade=5,
                        field_points_per_decade=2, loss_tangent=0.0, **SMALL, **ac)
    setup.caps.append(CapModel("X7R", 10e-6, 0.5e-9, 5e-3, ["C1", "C2"], dielectric="X7R"))
    return setup


def _thermal(t_of_x):
    """A stand-in for a DC result with a thermal co-simulation: T rises along x."""
    def sampler():
        return lambda xy: t_of_x(np.atleast_2d(xy)[:, 0])
    return SimpleNamespace(groups={}, thermal=SimpleNamespace(sampler=sampler))


def test_not_modeled_changes_nothing(board):
    r = solve_ac(board, _setup(board), "VRM1", "GND")
    assert r.cap_bias == {} and r.stats.t_copper is None


def test_a_fixed_temperature_warms_copper_and_capacitors(board, tmp_path):
    cold = solve_ac(board, _setup(board), "VRM1", "GND")
    setup = _setup(board, temperature="fixed", temperature_c=125.0)
    hot = solve_ac(board, setup, "VRM1", "GND")
    assert hot.cap_bias["C1"] == (None, pytest.approx(10e-6 * 0.88))
    assert "2 at 125 °C" in hot.bias_note and hot.stats.t_copper == (125.0, 125.0)
    # Hotter copper: more loss. The plane pair's resistance at its series resonance is
    # mostly the ESR, so compare the real part above it, where the copper shows.
    f = hot.freqs
    k = np.argmin(abs(f - 1e8))
    assert hot.probes[0].z[k].real > cold.probes[0].z[k].real * 1.05
    html = write_ac_report(tmp_path / "r.html", hot, board, setup).read_text(encoding="utf-8")
    assert "125 °C (fixed)" in html


def test_temperatures_from_the_thermal_co_simulation(board):
    setup = _setup(board, temperature="thermal", temperature_c=40.0)
    dc = _thermal(lambda x: 25.0 + 5.0 * x)                     # 25 °C at x = 0, 125 at 20
    r = solve_ac(board, setup, "VRM1", "GND", dc_result=dc)
    m = setup.caps[0]
    c1, c2 = r.cap_bias["C1"][1], r.cap_bias["C2"][1]
    assert c1 == pytest.approx(m.capacitance_at(0.0, temp_c=25.0 + 5.0 * 4.6))   # pads' middle
    assert c2 == pytest.approx(m.capacitance_at(0.0, temp_c=25.0 + 5.0 * 14.6))
    lo, hi = r.stats.t_copper
    assert lo < 30.0 and hi > 120.0
    assert "thermal co-simulation" in r.bias_note
    assert not any("thermal co-simulation to take" in w for w in r.warnings)


def test_without_a_thermal_co_simulation_the_fixed_temperature_is_used(board):
    setup = _setup(board, temperature="thermal", temperature_c=85.0)
    r = solve_ac(board, setup, "VRM1", "GND", dc_result=SimpleNamespace(groups={}, thermal=None))
    assert any("No DC thermal co-simulation" in w and "85 °C" in w for w in r.warnings)
    assert r.stats.t_copper == (85.0, 85.0)
    assert r.cap_bias["C1"][1] == pytest.approx(setup.caps[0].capacitance_at(0.0, temp_c=85.0))


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

@pytest.fixture
def panel():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.ac.gui.panel import ACPanel
    app = QApplication.instance() or QApplication([])   # noqa: F841
    p = ACPanel()
    p.set_board(plane_pair(), PISetup(), "test")
    yield p
    p.close()


def test_the_temperature_controls(panel):
    assert panel.cmb_temp.currentData() == "none" and not panel.spin_temp.isEnabled()
    panel.cmb_temp.setCurrentIndex(panel.cmb_temp.findData("thermal"))
    panel.spin_temp.setValue(70.0)
    assert panel.setup.ac.temperature == "thermal" and panel.setup.ac.temperature_c == 70.0
    assert panel.spin_temp.isEnabled()
    setup = PISetup()
    setup.ac.temperature, setup.ac.temperature_c = "fixed", 90.0
    panel.set_board(plane_pair(), setup, "test")                 # a setup read from a file
    assert panel.cmb_temp.currentData() == "fixed" and panel.spin_temp.value() == 90.0


def test_copper_resistance_follows_its_temperature_coefficient():
    """Plane pair without capacitors: at low frequency the probe's resistance is the copper's
    DC resistance, which rises by the copper's TCR; with skin effect only by its square root."""
    snap = plane_pair(20.0)

    def real_z(t):
        setup = plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e9, points_per_decade=2,
                            field_points_per_decade=2, loss_tangent=0.0, temperature=t,
                            temperature_c=120.0, **SMALL)
        return solve_ac(snap, setup, "VRM1", "GND").probes[0].z.real
    ratio = real_z("fixed") / real_z("none")
    tcr = PISetup().settings.tcr_per_c
    assert ratio[0] == pytest.approx(1 + tcr * 100, rel=2e-3)
    assert ratio[-1] == pytest.approx(np.sqrt(1 + tcr * 100), rel=0.02)
