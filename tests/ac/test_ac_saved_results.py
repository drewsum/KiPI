"""AC results files: every solved rail is saved and opens again without solving."""

import os

import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.report import RESULTS_SUFFIX, load_results, save_results
from kipi.ac.solver.sweep import ACResult, ProbeCurve
from kipi.dc.pi import report as dc_report
from kipi.dc.pi.model import PinRef, Vrm


def _result(rail, mag):
    f = np.logspace(3, 8, 11)
    return ACResult(rail=rail, rail_nets=["+3V3"], ground="GND", freqs=f,
                    probes=[ProbeCurve("U1", np.full(f.size, mag + 1e-4j), target_ohm=0.01,
                                       target_hz=1e7)],
                    field_freqs=f[::2], caps=["C1"], warnings=["w"], solve_s=1.5)


def _setup():
    snap = plane_pair()
    s = plane_setup(snap)
    s.vrms.append(Vrm("VRM2", PinRef("U1", "+3V3", ["1"]), 3.3))
    return snap, s


def test_round_trip_keeps_every_rail(tmp_path):
    snap, s = _setup()
    saved = {"VRM1": _result("VRM1", 0.001), "VRM2": _result("VRM2", 0.002)}
    path = save_results(tmp_path / f"b{RESULTS_SUFFIX}", saved, snap.board_name, s)
    data = load_results(path)
    assert data["board"] == snap.board_name and data["kind"] == "ac"
    assert set(data["results"]) == {"VRM1", "VRM2"}
    back = data["results"]["VRM2"]
    np.testing.assert_array_equal(back.freqs, saved["VRM2"].freqs)
    np.testing.assert_array_equal(back.probes[0].z, saved["VRM2"].probes[0].z)
    assert back.probes[0].target_ohm == 0.01 and back.caps == ["C1"]
    assert back.warnings == ["w"] and back.solve_s == 1.5
    assert [v.name for v in data["setup"].vrms] == ["VRM1", "VRM2"]


def test_report_link_is_dropped_if_the_report_is_gone(tmp_path):
    there, gone = _result("VRM1", 0.001), _result("VRM2", 0.002)
    (tmp_path / "r.html").write_text("x")
    there.report_path, gone.report_path = tmp_path / "r.html", tmp_path / "deleted.html"
    data = load_results(save_results(tmp_path / "a", {"VRM1": there, "VRM2": gone}, "b", None))
    assert data["results"]["VRM1"].report_path == tmp_path / "r.html"
    assert data["results"]["VRM2"].report_path is None


def test_dc_and_ac_files_are_told_apart(tmp_path):
    from tree_boards import board, setup
    from kipi.dc.pi.tree import solve_tree
    snap, s = board(), setup()
    dc = dc_report.save_results(tmp_path / "r.kipi-results", solve_tree(snap, s), {}, snap, s)
    ac = save_results(tmp_path / f"r{RESULTS_SUFFIX}", {"VRM1": _result("VRM1", 1e-3)}, "b", s)
    with pytest.raises(ValueError, match="DC mode"):
        load_results(dc)
    with pytest.raises(ValueError, match="AC mode"):
        dc_report.load_results(ac)
    assert dc_report.load_results(dc)["kind"] == "dc"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from kipi.ac.gui.panel import ACPanel
    snap, s = _setup()
    p = ACPanel()
    p.set_board(snap, s, "test")
    p.set_nets(["+3V3", "GND"])
    yield p
    p.close()


def _rail(p, name):
    p.cmb_rail.setCurrentIndex(p.cmb_rail.findData(name))


def test_save_is_offered_once_a_rail_is_solved(panel):
    assert not panel.btn_save.isEnabled()
    panel.show_result(_result("VRM2", 0.002))            # not the rail shown
    assert panel.btn_save.isEnabled() and panel.result is None
    panel.clear_results()
    assert not panel.btn_save.isEnabled()


def test_save_writes_every_rail(panel, tmp_path, monkeypatch):
    panel.show_result(_result("VRM1", 0.001))
    panel.show_result(_result("VRM2", 0.002))
    out = tmp_path / f"x{RESULTS_SUFFIX}"
    monkeypatch.setattr("qtpy.QtWidgets.QFileDialog.getSaveFileName",
                        lambda *a, **k: (str(out), ""))
    saved = []
    panel.results_saved.connect(saved.append)
    panel.btn_save.click()
    assert saved == [str(out)]
    assert set(load_results(out)["results"]) == {"VRM1", "VRM2"}


def test_opened_results_show_the_selected_rail(panel):
    one, two = _result("VRM1", 0.001), _result("VRM2", 0.002)
    assert panel.load_results({"VRM1": one, "VRM2": two}) == []
    assert panel.result is one and panel.btn_save.isEnabled()
    _rail(panel, "VRM2")
    assert panel.result is two


def test_opened_results_select_a_solved_rail(panel):
    two = _result("VRM2", 0.002)
    left_out = panel.load_results({"VRM2": two, "OLD": _result("OLD", 0.003)})
    assert left_out == ["OLD"]
    assert panel.cmb_rail.currentData() == "VRM2" and panel.result is two
    assert "OLD" not in panel._results


def test_opened_results_replace_the_ones_held(panel):
    panel.show_result(_result("VRM1", 0.001))
    two = _result("VRM2", 0.002)
    panel.load_results({"VRM2": two})
    assert panel.result is two
    _rail(panel, "VRM1")
    assert panel.result is None


@pytest.fixture
def window(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    yield w
    w.close()


def test_open_results_in_ac_mode_opens_ac_results(window, tmp_path, monkeypatch):
    snap, s = _setup()
    window.snapshot = snap
    window.ac_panel.set_board(snap, s, "test")
    window.ac_panel.set_nets(["+3V3", "GND"])
    window.set_mode("ac")
    path = save_results(tmp_path / f"x{RESULTS_SUFFIX}", {"VRM1": _result("VRM1", 0.001)},
                        snap.board_name, s)
    asked = []

    def ask(parent, title, start, filt):
        asked.append(filt)
        return str(path), ""
    monkeypatch.setattr("qtpy.QtWidgets.QFileDialog.getOpenFileName", ask)
    window.open_results()
    assert RESULTS_SUFFIX in asked[0]
    assert window.ac_panel.result.rail == "VRM1"
    assert window.plot.result is window.ac_panel.result
    assert window._tree is None                          # DC results untouched
