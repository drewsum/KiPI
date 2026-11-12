"""The AC panel keeps each rail's last results: changing the Rail selection shows that rail's
results again, or clears the view if it has not been solved."""

import os

import numpy as np
import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from ac_boards import plane_pair, plane_setup  # noqa: E402
from kipi.ac.solver.sweep import ACResult, ProbeCurve  # noqa: E402
from kipi.dc.pi.model import PinRef, Vrm  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _result(rail, mag):
    f = np.logspace(3, 8, 11)
    return ACResult(rail=rail, rail_nets=["+3V3"], ground="GND", freqs=f,
                    probes=[ProbeCurve("U1", np.full(f.size, mag, complex))], field_freqs=f)


@pytest.fixture
def panel(app):
    from kipi.ac.gui.panel import ACPanel
    snap = plane_pair()
    setup = plane_setup(snap)
    setup.vrms.append(Vrm("VRM2", PinRef("U1", "+3V3", ["1"]), 3.3))
    p = ACPanel()
    p.set_board(snap, setup)
    p.set_nets(["+3V3", "GND"])
    shown = []
    p.result_selected.connect(shown.append)
    p.shown = shown
    yield p
    p.close()


def _rail(p, name):
    p.cmb_rail.setCurrentIndex(p.cmb_rail.findData(name))


def _cell(p):
    return p.tbl_probes.item(0, 2).text() if p.tbl_probes.rowCount() else None


def test_rail_selection_restores_its_results(panel):
    p = panel
    assert p.cmb_rail.currentData() == "VRM1"
    one = _result("VRM1", 0.001)
    p.show_result(one)
    assert p.result is one and p.shown[-1] is one and _cell(p) == "1"

    _rail(p, "VRM2")                                     # not solved: nothing shown
    assert p.result is None and p.shown[-1] is None and _cell(p) is None
    assert not p.btn_export.isEnabled()
    two = _result("VRM2", 0.002)
    p.show_result(two)
    assert _cell(p) == "2"

    _rail(p, "VRM1")                                     # back: its earlier results
    assert p.result is one and p.shown[-1] is one and _cell(p) == "1"
    assert p.btn_export.isEnabled()
    _rail(p, "VRM2")
    assert p.result is two


def test_a_solve_for_another_rail_is_kept_not_shown(panel):
    p = panel
    one = _result("VRM1", 0.001)
    p.show_result(one)
    p.show_result(_result("VRM2", 0.002))                # finished after the rail changed
    assert p.result is one and _cell(p) == "1"
    assert "VRM2" in p.txt_notes.text()
    _rail(p, "VRM2")
    assert _cell(p) == "2"


def test_removed_rail_forgets_its_results(panel):
    p = panel
    p.show_result(_result("VRM1", 0.001))
    del p.setup.vrms[0]                                  # VRM1 deleted (e.g. in the DC panel)
    p.reload()
    assert p.cmb_rail.currentData() == "VRM2"
    assert p.result is None and p.shown[-1] is None
    assert "VRM1" not in p._results


def test_new_board_forgets_every_rail(panel):
    p = panel
    p.show_result(_result("VRM1", 0.001))
    p.set_board(p.snapshot, p.setup)
    _rail(p, "VRM2")
    _rail(p, "VRM1")
    assert p.result is None
