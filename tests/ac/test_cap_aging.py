"""Capacitor aging (#109): a model with a dielectric loses its aging rate per decade of
hours operated past 1000 hours, when Capacitor aging is ticked in the sweep settings, on
top of any DC bias."""

import math
import os

import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.checks import validate_ac
from kipi.ac.html_report import write_ac_report
from kipi.ac.model import AGING_PCT_PER_DECADE, AGING_REF_HOURS, CapModel
from kipi.ac.solver import solve_ac
from kipi.core.model.parts import parse_dielectric
from kipi.dc.pi.model import PISetup

# A small board with a coarse mesh: these tests are about the capacitor models, and the
# field solve on a bigger one takes ~20 s without PARDISO.
SMALL = dict(max_edge_mm=2.0, min_edge_mm=0.15, grading=0.6)


def test_aging_is_logarithmic_in_hours():
    m = CapModel("X7R", 10e-6, dielectric="X7R", aging_pct_per_decade=2.0)
    assert m.aged(None) == 1.0 and m.aged(AGING_REF_HOURS) == 1.0 and m.aged(10.0) == 1.0
    assert m.aged(1e4) == pytest.approx(0.98)                 # one decade past 1000 h
    assert m.aged(1e5) == pytest.approx(0.96)
    assert m.capacitance_at(0.0, 1e5) == pytest.approx(9.6e-6)
    assert CapModel("none", 1e-6, aging_pct_per_decade=5.0).aged(1e6) == 1.0   # no dielectric
    assert CapModel("c0g", 1e-9, dielectric="C0G").aged(1e6) == 1.0
    assert CapModel("worn", 1e-6, dielectric="Y5V", aging_pct_per_decade=40).aged(1e9) == 0.0


def test_aging_and_dc_bias_combine():
    m = CapModel("X5R", 10e-6, rated_v=6.3, kept_at_rated_pct=40,
                 dielectric="X5R", aging_pct_per_decade=3.0)
    assert m.capacitance_at(6.3, 1e4) == pytest.approx(10e-6 * 0.97 * 0.4)
    assert m.capacitance_at(6.3) == pytest.approx(4e-6)


def test_typical_rates():
    assert AGING_PCT_PER_DECADE["C0G"] == 0.0
    assert AGING_PCT_PER_DECADE["X7R"] < AGING_PCT_PER_DECADE["X5R"] < AGING_PCT_PER_DECADE["Y5V"]


@pytest.mark.parametrize("value, diel", [
    ("10uF 6.3V X5R", "X5R"), ("1u_x7r", "X7R"), ("100p NP0", "C0G"), ("22p C0G 50V", "C0G"),
    ("10u 16V", ""), ("COG", "C0G"), ("4u7 Y5V", "Y5V"),
])
def test_dielectric_from_the_value(value, diel):
    assert parse_dielectric(value) == diel


def test_aging_fields_are_saved_and_old_setups_load():
    setup = PISetup()
    setup.caps.append(CapModel("A", 1e-6, 1e-9, 1e-3, ["C1"], dielectric="X7R",
                               aging_pct_per_decade=2.5))
    setup.ac.aging, setup.ac.aging_hours = True, 5e4
    back = PISetup.from_dict(setup.to_dict())
    assert back.caps[0].dielectric == "X7R" and back.caps[0].aging_pct_per_decade == 2.5
    assert back.ac.aging and back.ac.aging_hours == 5e4
    d = setup.to_dict()
    for k in ("dielectric", "aging_pct_per_decade"):
        del d["caps"][0][k]
    del d["ac"]["aging"], d["ac"]["aging_hours"]
    old = PISetup.from_dict(d)
    assert old.caps[0].dielectric == "" and not old.ac.aging and old.ac.aging_hours == 87600


def test_hours_must_be_set_when_aging():
    snap = plane_pair()
    setup = plane_setup(snap, aging=True, aging_hours=0.0)
    assert any("hours" in e for e in validate_ac(setup, "VRM1", "GND"))
    setup.ac.aging = False
    assert not any("hours" in e for e in validate_ac(setup, "VRM1", "GND"))


@pytest.fixture(scope="module")
def board():
    return plane_pair(20.0, caps=[("C1", "10u X7R", (10.0, 10.0)), ("C2", "10u", (14.0, 14.0))])


def _setup(snap, **ac):
    setup = plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e8, points_per_decade=5,
                        field_points_per_decade=2, **SMALL, **ac)
    setup.caps.append(CapModel("X7R", 10e-6, 0.5e-9, 5e-3, ["C1"], dielectric="X7R",
                               aging_pct_per_decade=2.0))
    setup.caps.append(CapModel("polymer", 10e-6, 0.5e-9, 5e-3, ["C2"]))
    return setup


def test_solve_ages_only_models_with_a_dielectric(board, tmp_path):
    setup = _setup(board, aging=True, aging_hours=1e5)
    r = solve_ac(board, setup, "VRM1", "GND")
    assert r.cap_bias == {"C1": (None, pytest.approx(9.6e-6))}
    assert "1 aged 100000 hours" in r.bias_note and "DC bias" not in r.bias_note
    html = write_ac_report(tmp_path / "r.html", r, board, setup).read_text(encoding="utf-8")
    assert "C derated" in html and "9.6 µF (96%)" in html
    setup.ac.aging = False
    assert solve_ac(board, setup, "VRM1", "GND").cap_bias == {}


def test_solve_with_aging_and_dc_bias(board):
    setup = _setup(board, aging=True, aging_hours=1e4)
    m = setup.caps[0]
    m.rated_v = 6.3
    v, c = solve_ac(board, setup, "VRM1", "GND").cap_bias["C1"]
    assert v == 3.3 and c == pytest.approx(m.capacitance_at(3.3) * 0.98)
    assert math.isclose(c, m.capacitance_at(3.3, 1e4))


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
    p.set_board(plane_pair(caps=[("C1", "10uF 6.3V X5R", (25.0, 25.0)),
                                 ("C2", "100p NP0", (30.0, 30.0))]), PISetup(), "test")
    p.set_nets(["+3V3", "GND"])
    yield p
    p.close()


def test_models_from_values_read_the_dielectric(panel):
    panel._models_from_values()
    by_name = {m.name: m for m in panel.setup.caps}
    assert by_name["10uF 6.3V X5R"].dielectric == "X5R"
    assert by_name["10uF 6.3V X5R"].aging_pct_per_decade == AGING_PCT_PER_DECADE["X5R"]
    assert by_name["100p NP0"].dielectric == "C0G"


def test_picking_a_dielectric_fills_its_rate(panel):
    panel.cap_list._add()
    ed = panel.cap_list.editor
    m = panel.setup.caps[-1]
    assert not ed.aging.isEnabled()
    ed.dielectric.setCurrentIndex(ed.dielectric.findData("Y5V"))
    assert m.dielectric == "Y5V" and m.aging_pct_per_decade == AGING_PCT_PER_DECADE["Y5V"]
    assert ed.aging.isEnabled()
    ed.aging.setValue(6.0)                              # the datasheet's rate
    assert m.aging_pct_per_decade == 6.0 and "Y5V" in panel._describe_cap(m)
    ed.dielectric.setCurrentIndex(0)
    assert m.dielectric == "" and not ed.aging.isEnabled()


def test_the_sweep_switch_and_hours(panel):
    assert not panel.spin_hours.isEnabled()
    panel.chk_aging.setChecked(True)
    panel.spin_hours.setValue(20000)
    assert panel.setup.ac.aging and panel.setup.ac.aging_hours == 20000
    assert panel.spin_hours.isEnabled()


def test_the_hours_slider_shows_only_while_aging(panel):
    assert panel.w_hours.isHidden()
    panel.chk_aging.setChecked(True)
    assert not panel.w_hours.isHidden()
    panel.sld_hours.setValue(panel.sld_hours.maximum())
    assert panel.setup.ac.aging_hours == 1e6
    panel.sld_hours.setValue(panel.sld_hours.minimum())
    assert panel.setup.ac.aging_hours == 1000
    panel.sld_hours.setValue(panel.sld_hours.maximum() // 2)          # 10^4.5 h
    assert panel.setup.ac.aging_hours == 31600
    panel.spin_hours.setValue(87600)                                  # typed: slider follows
    assert panel.sld_hours.value() == round((4.9425041 - 3) / 3 * 300)
    assert panel.lbl_years.text() == "10 years"
    panel.chk_aging.setChecked(False)
    assert panel.w_hours.isHidden() and not panel.spin_hours.isEnabled()
