"""The via table: changing the current limit only re-marks PASS / FAIL, and stays fast."""

import os
import time
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QHeaderView  # noqa: E402

from kipi.dc.solver.post import ViaResult  # noqa: E402
from test_multiphase import board  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _panel(app, n):
    from kipi.dc.gui.pi_panel import PIPanel
    vias = [ViaResult(f"v{i}", "via", (float(i), 0.0), 0.3, 0.001 * i, 1e-6) for i in range(n)]
    p = PIPanel()
    p.snapshot = board()
    p.res_tabs.setCurrentWidget(p.tbl_vias.parentWidget())
    p.show()
    p._tree = NS(groups={"G": NS(name="G", result=NS(fields=NS(vias=vias)))})
    p._fill_vias()
    app.processEvents()
    return p


def _status(p):
    return [p.tbl_vias.item(r, 7).text() for r in range(p.tbl_vias.rowCount())]


def test_limit_marks_rows_and_none_clears_them(app):
    p = _panel(app, 10)                       # 0..9 mA, listed highest first
    assert _status(p) == [""] * 10
    p.spin_via_limit.setValue(4.5)            # mA
    assert _status(p) == ["FAIL"] * 5 + ["PASS"] * 5
    assert p.setup.via_current_limit_a == pytest.approx(4.5e-3)
    p.spin_via_limit.setValue(0)              # "none"
    assert _status(p) == [""] * 10
    assert p.setup.via_current_limit_a is None
    p.close()


def test_limit_change_keeps_rows_and_column_sizing(app):
    p = _panel(app, 2000)
    ids = [p.tbl_vias.item(r, 0).data(0x0100) for r in range(p.tbl_vias.rowCount())]
    t0 = time.perf_counter()
    for k in range(5):
        p.spin_via_limit.setValue(100 + 10 * k)
        app.processEvents()
    assert time.perf_counter() - t0 < 5.0
    assert [p.tbl_vias.item(r, 0).data(0x0100) for r in range(p.tbl_vias.rowCount())] == ids
    header = p.tbl_vias.horizontalHeader()
    assert all(header.sectionResizeMode(c) == QHeaderView.ResizeToContents
               for c in range(header.count()))
    p.close()
