"""The Tolerance window (#167): opened beside Power tree, it shows each load with a
tolerance under its VRM, takes new values from the results into the setup, and writes one
load's report."""

import copy
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("matplotlib")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.core.progress import NULL  # noqa: E402
from kipi.dc.pi.tree import solve_tree  # noqa: E402
from tree_boards import board, setup  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tolerance"))
from test_tolerance_stack import _ac, _probe  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_the_window_takes_results_into_the_setup(app, no_3d, tmp_path, opened_in_browser):
    from kipi.dc.pi.model import PISetup, Load, PinRef
    from kipi.gui.main_window import MainWindow
    from kipi.tolerance.gui.table import NEW_BG
    snap, s = board(), setup()
    s.loads.append(Load("NOTOL", PinRef("U2", "+3V3", ["1"]), 0.0))   # no tolerance: hidden
    _probe(s)
    w = MainWindow()
    w._run = lambda fn, done, name, **k: done(fn(NULL))
    try:
        from qtpy.QtWidgets import QToolBar
        main, = [b for b in w.findChildren(QToolBar) if b.windowTitle() == "Main"]
        names = [a.text() for a in main.actions()]
        assert names.index("Tolerance") == names.index("Power tree") + 1
        w.snapshot, w._setup_path = snap, tmp_path / "tree.kipi-setup.yml"
        for panel in w._panels().values():
            panel.set_board(snap, s)
        w._sync_tolerance()
        w._saved = copy.deepcopy(s)                          # as if just loaded
        w._saved.nets = w._checked_nets()
        w.show_tolerance()
        win = w.tolerance_window
        assert win.isVisible() and win.isWindow() and win.parent() is w
        assert win.windowFlags() & Qt.WindowMaximizeButtonHint
        assert win.windowFlags() & Qt.WindowMinimizeButtonHint
        v = w.tolerance_view
        t = v.table
        # One VRM header, one load (NOTOL has no tolerance).
        assert [r is None for r in v._rows] == [True, False]
        assert t.item(0, 0).text().startswith("U1: +3V3")
        assert t.item(1, 2).text() == "Run DC" and t.item(1, 9).text() == "incomplete"

        w._set_tree(solve_tree(snap, s), {})
        assert t.item(1, 2).background().color() == NEW_BG
        assert not w._unsaved()
        t.cellClicked.emit(1, 2)                              # accept the DC drop
        assert s.tolerance.entries["U2"].dc.source == "DC solve"
        assert t.item(1, 2).background().color() != NEW_BG
        assert w._unsaved()

        v.add_ac(_ac(), "AC solve", "now")
        assert t.item(1, 6).background().color() == NEW_BG
        assert v.btn_accept.isEnabled()
        v.btn_accept.click()
        assert s.tolerance.entries["U2"].transient is not None and not v.btn_accept.isEnabled()
        assert t.item(1, 5).text() == "—"                     # an LDO rail: no switcher

        v.cmb_unit.setCurrentIndex(v.cmb_unit.findData("V"))
        assert t.horizontalHeaderItem(2).text() == "DC drop (V)"
        v.chk_bw.setChecked(True)
        assert s.tolerance.bw_limit

        w._save_setup()
        back = PISetup.load(w._setup_path)
        assert back.tolerance == s.tolerance and not w._unsaved()

        assert not v.report_option.btn.isEnabled()
        v.select_load("U2")
        v.report_option.btn.click()
        written, = tmp_path.glob("*_tolerance_U2_*.html")
        assert [Path(p) for p in opened_in_browser] == [written]
    finally:
        w.close()


def test_the_diagram_sits_above_the_table_and_shrinks_to_fit(app):
    from kipi.tolerance.gui.table import ToleranceWindow
    win = ToleranceWindow()
    v = win.view
    try:
        b = v.banner
        assert b.available()
        win.resize(1250, 640)
        win.show()
        app.processEvents()
        assert b.isVisible() and b.geometry().bottom() < v.table.geometry().top()
        assert b.height() == b.heightForWidth(b.width()) == 192          # full size
        # It asks for no width of its own, and is as tall as the width it is given needs
        # (the offscreen platform's top bar, without fonts, is too wide to shrink to).
        assert b.minimumSizeHint().width() == 0
        assert b.heightForWidth(450) == 96 and b.heightForWidth(2000) == 192
        v.chk_diagram.setChecked(False)
        assert not b.isVisible()
    finally:
        win.close()


def test_without_svg_support_the_window_has_no_diagram(app, monkeypatch):
    from kipi.gui import tooltip_art
    from kipi.tolerance.gui.table import ToleranceView
    monkeypatch.setattr(tooltip_art, "image", lambda *a, **k: None)
    v = ToleranceView()
    assert not v.banner.available() and v.banner.isHidden() and v.chk_diagram.isHidden()
