"""Copper temperature limits after an electro-thermal solve: the hottest solved copper on
each layer, graded against a maximum temperature and a maximum rise above ambient, in the
setup file, the CSV export and the Thermal results tab."""

import os

import pytest

from kipi.dc.pi.model import PISetup
from kipi.dc.pi.report import export_csv, layer_temps, thermal_passed
from kipi.dc.thermal.cosim import solve_electrothermal
from kipi.dc.thermal.model import ThermalSettings
from test_tree import board, setup


def test_limits_grade_a_temperature():
    t = ThermalSettings(ambient_c=25.0)
    assert not t.limits_set() and t.temp_passed(200.0, 25.0) is None
    t.max_temp_c = 100.0
    assert t.temp_passed(100.0, 25.0) is True and t.temp_passed(100.1, 25.0) is False
    t.max_rise_c = 20.0                                  # both must hold
    assert t.temp_passed(45.0, 25.0) is True and t.temp_passed(46.0, 25.0) is False
    t.max_temp_c = None
    assert t.temp_passed(90.0, 75.0) is True             # the rise is over the solve's ambient


def test_limits_round_trip_through_the_setup_file():
    s = setup("ldo")
    s.thermal = ThermalSettings(enabled=True, max_temp_c=105.0, max_rise_c=30.0)
    back = PISetup.from_dict(s.to_dict()).thermal
    assert (back.max_temp_c, back.max_rise_c) == (105.0, 30.0)
    old = s.to_dict()
    del old["thermal"]["max_temp_c"], old["thermal"]["max_rise_c"]
    assert not PISetup.from_dict(old).thermal.limits_set()   # older setups: no limits


@pytest.fixture(scope="module")
def hot():
    """An electro-thermal solve warm enough to grade: about 60 °C on this small board."""
    snap = board()
    s = setup("smps", eff=0.99, ground=True)
    s.thermal = ThermalSettings(enabled=True, iterations=2, grid_mm=0.5)
    return snap, s, solve_electrothermal(snap, s, s.thermal)


def test_hottest_copper_per_layer(hot):
    snap, s, tree = hot
    rows = layer_temps(tree, snap)
    assert [r.layer for r in rows] == ["F.Cu", "B.Cu"]
    th = tree.thermal
    for r in rows:
        assert 25.0 < r.t_max <= th.t_max + 1e-9
        assert r.rise == pytest.approx(r.t_max - 25.0) and r.passed is None
        assert r.group in tree.groups
        assert th.sampler()([[r.x, r.y]])[0] == pytest.approx(r.t_max)
    assert thermal_passed(rows) is None
    peak = max(r.t_max for r in rows)
    lim = ThermalSettings(max_temp_c=(25.0 + peak) / 2)  # between ambient and the peak
    graded = layer_temps(tree, snap, lim)
    assert thermal_passed(graded) is False
    assert any(r.passed is False for r in graded)
    lim = ThermalSettings(max_rise_c=peak - 25.0 + 1.0)
    assert thermal_passed(layer_temps(tree, snap, lim)) is True


def test_no_thermal_rows_without_a_thermal_solve():
    from kipi.dc.pi.tree import solve_tree
    tree = solve_tree(board(), setup("ldo"))
    assert layer_temps(tree, board()) == []


def test_csv_export_has_the_copper_temperature(hot, tmp_path):
    snap, s, tree = hot
    paths = export_csv(tree, snap, tmp_path, "b", thermal_limits=ThermalSettings(max_rise_c=1e-3))
    path = tmp_path / "b_copper_temperature.csv"
    assert path in paths
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("layer,t_max_C,rise_C")
    assert len(lines) == 3 and all(ln.endswith(",FAIL") for ln in lines[1:])


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_thermal_results_tab(app, hot):
    from kipi.dc.gui.pi_panel import PIPanel
    from kipi.dc.pi.tree import solve_tree
    snap, s, tree = hot
    panel = PIPanel()
    panel.set_board(snap, s, "test")
    tabs = panel.res_tabs
    titles = lambda: [tabs.tabText(i) for i in range(tabs.count())]
    assert "Thermal" not in titles()

    panel.show_results(tree, next(iter(tree.groups)))
    assert titles()[-2:] == ["Thermal", "Notes"]
    t = panel.tbl_temps
    assert [t.item(r, 0).text() for r in range(t.rowCount())] == ["F.Cu", "B.Cu"]
    assert t.item(0, 6).text() == "" and "No temperature limits" in panel.lbl_thermal.text()

    panel.spin_max_rise.setValue(0.1)                    # graded straight away
    assert s.thermal.max_rise_c == pytest.approx(0.1)
    assert {t.item(r, 6).text() for r in range(t.rowCount())} == {"FAIL"}
    assert "FAIL" in panel.lbl_thermal.text()
    panel.spin_max_rise.setValue(500)
    assert {t.item(r, 6).text() for r in range(t.rowCount())} == {"PASS"}
    panel.spin_max_rise.setValue(0)                      # "none"
    assert s.thermal.max_rise_c is None

    t.cellDoubleClicked.emit(1, 0)                       # B.Cu: its group, temperature view
    assert panel.cmb_group.currentText() == t.item(1, 5).text()
    assert panel.cmb_field.currentData() == "temperature"

    cold = solve_tree(snap, s)                           # no thermal solve: no tab
    panel.show_results(cold, next(iter(cold.groups)))
    assert "Thermal" not in titles()
    panel.close()
