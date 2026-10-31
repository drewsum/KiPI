"""The Edge boundaries dialog and its row in the Thermal tab (#221)."""

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QDialog  # noqa: E402

from kipi.dc.gui.edge_boundaries import EdgeBoundaryDialog  # noqa: E402
from kipi.dc.thermal.model import EdgeBoundary  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def plate():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dc"))
    from test_thermal import plate as p
    return p()


def event(dlg, x, y):
    return SimpleNamespace(inaxes=dlg.ax, button=1, xdata=x, ydata=y)


def test_clicking_an_edge_holds_its_side_at_ambient(app):
    dlg = EdgeBoundaryDialog(plate(), [], 30.0)
    dlg._on_press(event(dlg, 0.2, 15))
    dlg._on_release(event(dlg, 0.2, 15))
    (e,) = dlg.boundaries()
    assert e.name == "Edge 1" and e.temp_c == 30.0 and e.length_mm() == pytest.approx(40)
    assert dlg.table.rowCount() == 1


def test_dragging_takes_the_stretch_dragged_over(app):
    dlg = EdgeBoundaryDialog(plate(), [], 25.0)
    dlg._on_press(event(dlg, 0, 10))
    dlg._on_motion(event(dlg, 0, 30))
    dlg._on_release(event(dlg, 0, 30))
    assert dlg.boundaries()[0].length_mm() == pytest.approx(20)


def test_a_press_away_from_the_edge_adds_nothing(app):
    dlg = EdgeBoundaryDialog(plate(), [], 25.0)
    dlg._on_press(event(dlg, 20, 20))
    dlg._on_release(event(dlg, 20, 20))
    assert dlg.boundaries() == []


def test_editing_works_on_a_copy_and_the_table_sets_values(app):
    edges = [EdgeBoundary("frame", 50.0, 0.0, [[0, 0], [0, 40]])]
    dlg = EdgeBoundaryDialog(plate(), edges, 25.0)
    dlg.table.cellWidget(0, 1).setValue(70.0)
    dlg.table.cellWidget(0, 2).setValue(4.0)
    dlg.table.item(0, 0).setText("cold wall")
    assert edges[0].temp_c == 50.0                          # the caller's are untouched
    (e,) = dlg.boundaries()
    assert (e.name, e.temp_c, e.width_mm) == ("cold wall", 70.0, 4.0)
    dlg.btn_whole.click()
    assert dlg.boundaries()[1].length_mm() == pytest.approx(160)
    dlg.table.selectRow(0)
    dlg.btn_delete.click()
    assert [b.name for b in dlg.boundaries()] == ["Edge 2"]
    dlg.btn_clear.click()
    assert dlg.boundaries() == []


def test_the_thermal_tab_shows_and_edits_the_boundaries(app, monkeypatch):
    from kipi.dc.gui import edge_boundaries
    from kipi.dc.gui.pi_panel import PIPanel
    from kipi.dc.pi.model import PISetup
    setup = PISetup()
    panel = PIPanel()
    panel.set_board(plate(), setup)
    assert "convection alone" in panel.lbl_edges.text()
    assert not panel.btn_edges.isEnabled()                  # thermal is off
    panel.chk_thermal.setChecked(True)
    assert panel.btn_edges.isEnabled()

    class Accept(EdgeBoundaryDialog):
        def exec(self):
            self.add([[0, 0], [0, 40]])
            return QDialog.Accepted

    monkeypatch.setattr(edge_boundaries, "EdgeBoundaryDialog", Accept)
    panel.btn_edges.click()
    assert [e.name for e in setup.thermal.edges] == ["Edge 1"]
    assert panel.lbl_edges.text() == "Edge 1 25 °C"
