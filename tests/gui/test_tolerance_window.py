"""The Tolerance window (#167): opened beside Power tree, it shows the loads picked (with a
tolerance) under their VRM, takes new values from the results into its own tolerance file,
and writes one load's report."""

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
from kipi.dc.pi.model import Load, PinRef  # noqa: E402
from tree_boards import board, setup  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tolerance"))
from test_tolerance_stack import _ac, _probe  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_the_window_keeps_results_in_its_own_file(app, no_3d, tmp_path, opened_in_browser,
                                                  monkeypatch):
    from qtpy.QtWidgets import QFileDialog, QMessageBox
    from kipi.gui.main_window import MainWindow
    from kipi.tolerance.gui.table import NEW_BG
    from kipi.tolerance.model import ToleranceTable
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
        v = w.tolerance_view
        v.open_default("tree.kicad_pcb", str(tmp_path))       # none there yet
        w._sync_tolerance()
        w._saved = copy.deepcopy(s)                          # as if just loaded
        w._saved.nets = w._checked_nets()
        w.show_tolerance()
        win = w.tolerance_window
        assert win.isVisible() and win.isWindow() and win.parent() is w
        assert win.windowFlags() & Qt.WindowMaximizeButtonHint
        assert win.windowFlags() & Qt.WindowMinimizeButtonHint
        t = v.table
        # A new table shows no loads until they are picked; only U2 has a tolerance.
        assert t.rowCount() == 0 and "under Loads" in v.lbl.text()
        lst = v.load_list
        assert v.load_list.isVisible() and lst.count() == 1
        assert lst.item(0).data(Qt.UserRole) == "U2" and lst.item(0).checkState() == Qt.Unchecked
        v.btn_all.click()                                     # Select all
        assert v.data.loads == ["U2"] and lst.item(0).checkState() == Qt.Checked
        v.btn_none.click()                                    # Select none
        assert v.data.loads == [] and t.rowCount() == 0
        v.load_filter.setText("nothing like it")
        assert lst.item(0).isHidden()
        v.btn_all.click()                                     # only the loads listed
        assert v.data.loads == []
        v.load_filter.setText("")
        lst.item(0).setCheckState(Qt.Checked)                 # a tick, as by a click
        assert v.unsaved() and win.windowTitle().endswith("tree.kipi-tolerance.yml *")
        assert [r is None for r in v._rows] == [True, False]
        assert t.item(0, 0).text().startswith("U1: +3V3")
        assert t.item(1, 2).text() == "Run DC" and t.item(1, 9).text() == "incomplete"

        w._set_tree(solve_tree(snap, s), {})
        assert t.item(1, 2).background().color() == NEW_BG
        t.cellClicked.emit(1, 2)                              # accept the DC drop
        assert v.data.entries["U2"].dc.source == "DC solve"
        assert t.item(1, 2).background().color() != NEW_BG
        assert not w._unsaved()                               # the setup is untouched

        v.add_ac(_ac(), "AC solve", "now")
        assert t.item(1, 6).background().color() == NEW_BG
        assert v.btn_accept.isEnabled()
        v.btn_accept.click()
        assert v.data.entries["U2"].transient is not None and not v.btn_accept.isEnabled()
        assert t.item(1, 5).text() == "—"                     # an LDO rail: no switcher

        v.cmb_unit.setCurrentIndex(v.cmb_unit.findData("V"))
        assert t.horizontalHeaderItem(2).text() == "DC drop (V)"
        v.chk_bw.setChecked(True)
        assert v.data.bw_limit

        v.btn_save.click()
        path = tmp_path / "tree.kipi-tolerance.yml"
        assert ToleranceTable.load(path) == v.data and not v.unsaved()
        assert not win.windowTitle().endswith("*")
        assert "tolerance" not in (tmp_path / "tree.kipi-setup.yml").read_text(
            encoding="utf-8") if (tmp_path / "tree.kipi-setup.yml").exists() else True

        # Save as elsewhere, then open the first file again.
        other = tmp_path / "rev_b.kipi-tolerance.yml"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(other), ""))
        v.set_loads([])
        assert v.save_as() and v.path == other and ToleranceTable.load(other).loads == []
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
        assert v.open_file() and v.path == path and v.data.loads == ["U2"]
        assert lst.item(0).checkState() == Qt.Checked         # the pane follows the file

        # Closing asks about a table not saved, and Cancel keeps the window open.
        v.set_loads([])
        asked = []
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: asked.append(a[2]) or QMessageBox.Cancel)
        assert not w._keep_or_drop_changes() and "tolerance" in asked[0]
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
        assert w._keep_or_drop_changes()

        v.set_loads(["U2"])
        assert not v.report_option.btn.isEnabled()
        v.select_load("U2")
        v.report_option.btn.click()
        written, = tmp_path.glob("*_tolerance_U2_*.html")
        assert [Path(p) for p in opened_in_browser] == [written]
    finally:
        v.data = copy.deepcopy(v._saved)                      # nothing to ask on close
        w.close()


def test_the_board_s_tolerance_file_opens_with_it(app, tmp_path):
    from kipi.tolerance.gui.table import ToleranceView
    from kipi.tolerance.model import ToleranceTable
    ToleranceTable(loads=["U2"]).save(tmp_path / "tree.kipi-tolerance.yml")
    v = ToleranceView()
    v.open_default("tree.kicad_pcb", str(tmp_path))
    assert v.data.loads == ["U2"] and not v.unsaved()
    (tmp_path / "tree.kipi-tolerance.yml").write_text("[not, a, table", encoding="utf-8")
    logged = []
    v.log = lambda text, level=0: logged.append(text)
    v.open_default("tree.kicad_pcb", str(tmp_path))       # unreadable: an empty table
    assert v.data == ToleranceTable() and "Could not read" in logged[0]


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
