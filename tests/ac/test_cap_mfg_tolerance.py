# Portions Copyright (c) 2026 Tomachie LLC.
"""Capacitor manufacturing tolerance: a model with one is solved at the low end of it,
the worst case for a PDN, when Capacitor manufacturing tolerance is ticked in the sweep
settings, on top of any DC bias, aging and temperature."""

import os

import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.html_report import write_ac_report
from kipi.ac.model import CapModel
from kipi.ac.solver import solve_ac
from kipi.dc.pi.model import PISetup

SMALL = dict(max_edge_mm=2.0, min_edge_mm=0.15, grading=0.6)


def test_the_tolerance_is_subtracted_from_the_nominal():
    m = CapModel("K", 10e-6, mfg_tolerance_pct=10.0)
    assert m.within_tolerance(False) == 1.0              # not modeled: as entered
    assert m.within_tolerance() == pytest.approx(0.9)
    assert m.capacitance_at(0.0, mfg_tolerance=True) == pytest.approx(9e-6)
    assert m.capacitance_at(0.0) == pytest.approx(10e-6)
    assert CapModel("none", 1e-6).within_tolerance() == 1.0          # no tolerance set
    assert CapModel("all", 1e-6, mfg_tolerance_pct=100.0).within_tolerance() == 0.0


def test_the_tolerance_combines_with_bias_and_aging():
    m = CapModel("X5R", 10e-6, rated_v=6.3, kept_at_rated_pct=40, dielectric="X5R",
                 aging_pct_per_decade=3.0, mfg_tolerance_pct=20.0)
    assert m.capacitance_at(6.3, 1e4, mfg_tolerance=True) == pytest.approx(
        10e-6 * 0.8 * 0.97 * 0.4)
    assert m.capacitance_at(6.3, 1e4) == pytest.approx(10e-6 * 0.97 * 0.4)


def test_the_tolerance_is_saved_and_old_setups_load():
    setup = PISetup()
    setup.caps.append(CapModel("A", 1e-6, 1e-9, 1e-3, ["C1"], mfg_tolerance_pct=10.0))
    setup.ac.mfg_tolerance = True
    back = PISetup.from_dict(setup.to_dict())
    assert back.caps[0].mfg_tolerance_pct == 10.0 and back.ac.mfg_tolerance
    d = setup.to_dict()
    del d["caps"][0]["mfg_tolerance_pct"], d["ac"]["mfg_tolerance"]
    old = PISetup.from_dict(d)
    assert old.caps[0].mfg_tolerance_pct is None and not old.ac.mfg_tolerance


@pytest.fixture(scope="module")
def board():
    return plane_pair(20.0, caps=[("C1", "10u", (10.0, 10.0)), ("C2", "10u", (14.0, 14.0))])


def _setup(snap, **ac):
    setup = plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e8, points_per_decade=5,
                        field_points_per_decade=2, **SMALL, **ac)
    setup.caps.append(CapModel("±10 %", 10e-6, 0.5e-9, 5e-3, ["C1"], mfg_tolerance_pct=10.0))
    setup.caps.append(CapModel("no tolerance", 10e-6, 0.5e-9, 5e-3, ["C2"]))
    return setup


def test_solve_derates_only_models_with_a_tolerance(board, tmp_path):
    setup = _setup(board, mfg_tolerance=True)
    r = solve_ac(board, setup, "VRM1", "GND")
    assert r.cap_bias == {"C1": (None, pytest.approx(9e-6))}
    assert "1 at the low end of their 10 % manufacturing tolerance" in r.bias_note
    assert "DC bias" not in r.bias_note
    html = write_ac_report(tmp_path / "r.html", r, board, setup).read_text(encoding="utf-8")
    assert "C derated" in html and "9 µF (90%)" in html
    setup.ac.mfg_tolerance = False
    assert solve_ac(board, setup, "VRM1", "GND").cap_bias == {}


def test_solve_with_the_tolerance_and_dc_bias(board):
    setup = _setup(board, mfg_tolerance=True)
    m = setup.caps[0]
    m.rated_v = 6.3
    v, c = solve_ac(board, setup, "VRM1", "GND").cap_bias["C1"]
    assert v == 3.3 and c == pytest.approx(m.capacitance_at(3.3, mfg_tolerance=True))
    assert c == pytest.approx(m.capacitance_at(3.3) * 0.9)
    note = solve_ac(board, setup, "VRM1", "GND").bias_note
    assert "DC bias" in note and "manufacturing tolerance" in note


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
    p.set_board(plane_pair(caps=[("C1", "10uF 6.3V X5R", (25.0, 25.0))]), PISetup())
    p.set_nets(["+3V3", "GND"])
    yield p
    p.close()


def test_the_field_is_optional_and_edits_the_model(panel):
    panel.cap_list._add()
    ed, m = panel.cap_list.editor, panel.setup.caps[-1]
    assert m.mfg_tolerance_pct is None                   # optional: nothing until set
    assert ed.mfg_tol.value() == 0 and ed.mfg_tol.text() == "not set"
    ed.mfg_tol.setValue(10.0)
    assert m.mfg_tolerance_pct == 10.0 and "±10 %" in panel._describe_cap(m)
    ed.mfg_tol.setValue(0.0)
    assert m.mfg_tolerance_pct is None


def test_the_sweep_switch_stores_the_setting(panel):
    assert not panel.chk_mfg.isChecked() and not panel.setup.ac.mfg_tolerance
    panel.chk_mfg.setChecked(True)
    assert panel.setup.ac.mfg_tolerance
    panel.setup.ac.mfg_tolerance = False                 # a setup opened with it off
    panel._load_ac_settings()
    assert not panel.chk_mfg.isChecked()
