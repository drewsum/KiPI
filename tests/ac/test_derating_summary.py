"""The AC results and report say which capacitor deratings the sweep applied, and how the
derated capacitance moved each model's self-resonance (#180)."""

import os

import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.bias import apply_derating
from kipi.ac.html_report import _srf_text, write_ac_report
from kipi.ac.model import CapModel
from kipi.ac.solver import solve_ac
from kipi.ac.solver.fem import PortDef
from kipi.dc.pi.model import PinRef, PISetup, Vrm

VRM = Vrm("VRM1", PinRef("U1", "+3V3", ["1"]), 3.3)


def _rows(*models, **ac):
    setup = PISetup()
    for k, v in ac.items():
        setattr(setup.ac, k, v)
    ports = [PortDef(m.name, "cap", [], [], m) for m in models]
    _out, _notes, rows = apply_derating(ports, setup, VRM, {})
    return {what: (on, how) for what, on, how in rows}


def test_each_derating_says_whether_it_was_applied_and_why_not():
    rated = CapModel("C1", 10e-6, 1e-9, 0.01, ["C1"], rated_v=6.3)
    rows = _rows(rated, aging=True, mfg_tolerance=False)
    assert list(rows) == ["DC bias", "Manufacturing tolerance", "Aging",
                          "Dielectric temperature"]
    assert rows["DC bias"] == (True, "1 part(s), at the VRM1 target voltage, 3.3 V (no DC "
                                     "solve of this rail)")
    assert rows["Manufacturing tolerance"] == (False, "not ticked")
    assert rows["Aging"] == (False, "ticked, but no model here has a dielectric that ages")
    assert rows["Dielectric temperature"][0] is False
    assert "not modeled" in rows["Dielectric temperature"][1]


def test_applied_deratings_count_their_parts():
    x7r = CapModel("C1", 10e-6, 1e-9, 0.01, ["C1"], dielectric="X7R",
                   aging_pct_per_decade=2.0, mfg_tolerance_pct=10.0)
    rows = _rows(x7r, dc_bias=False, aging=True, aging_hours=87600.0, mfg_tolerance=True)
    assert rows["DC bias"] == (False, "not ticked")
    assert rows["Manufacturing tolerance"] == (True, "1 part(s), at the low end of their 10 % "
                                                     "tolerance")
    assert rows["Aging"] == (True, "1 part(s), 87600 hours operated (10 years)")
    off = _rows(x7r, mfg_tolerance=True)
    assert off["DC bias"] == (False, "ticked, but no model here has a voltage rating")


def test_the_derated_self_resonance_rises_as_the_capacitance_falls():
    m = CapModel("C1", 1e-6, 1e-9, 0.01)
    assert _srf_text(m, []) == ""
    assert _srf_text(m, [(3.3, 0.25e-6)]).endswith("(+100%)")      # a quarter of C: 2 x f
    assert " to " in _srf_text(m, [(3.3, 0.25e-6), (1.0, 1e-6)])


def test_the_report_lists_the_deratings_and_the_derated_srf(tmp_path):
    snap = plane_pair(20.0, caps=[("C1", "10u", (10.0, 10.0))])
    setup = plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e8, points_per_decade=5,
                        field_points_per_decade=2)
    setup.caps.append(CapModel("10u", 10e-6, 0.5e-9, 5e-3, ["C1"], rated_v=6.3))
    r = solve_ac(snap, setup, "VRM1", "GND")
    assert [(what, on) for what, on, _how in r.deratings][:2] == [
        ("DC bias", True), ("Manufacturing tolerance", False)]
    html = write_ac_report(tmp_path / "r.html", r, snap, setup).read_text(encoding="utf-8")
    assert "SRF derated" in html and "<h3>Capacitor derating</h3>" in html
    assert "not ticked" in html


def test_the_sweep_tab_groups_the_deratings():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.ac.gui.panel import ACPanel
    app = QApplication.instance() or QApplication([])   # noqa: F841
    p = ACPanel()
    try:
        for w in (p.chk_bias, p.chk_mfg, p.chk_aging, p.spin_hours, p.cmb_temp, p.spin_temp):
            assert p.g_derate.isAncestorOf(w)
        assert p.g_derate.title() == "Capacitor derating"
    finally:
        p.close()
