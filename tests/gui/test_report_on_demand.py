"""Reports are written on demand (#185): the Report button writes one of the shown results,
after a solve or with saved results opened, and opens it."""

import os
from pathlib import Path

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("matplotlib")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.core.progress import NULL  # noqa: E402
from kipi.dc.pi.tree import solve_tree  # noqa: E402
from kipi.dc.viz.results import build_result_scene  # noqa: E402
from tree_boards import board, setup  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_the_dc_report_button_writes_and_opens_a_report(app, tmp_path, opened_in_browser):
    from kipi.gui.main_window import MainWindow
    snap, s = board(), setup("ldo", iq=0.01, ground=True)
    tree = solve_tree(snap, s)
    scenes = {n: build_result_scene(snap, g.result) for n, g in tree.groups.items()}
    w = MainWindow()
    w._run = lambda fn, done, name, **k: done(fn(NULL))      # no background thread here
    try:
        w.snapshot, w._setup_path = snap, tmp_path / "board.kipi-setup.yml"
        w.solve_panel.set_board(snap, s)
        opt = w.solve_panel.report_option
        assert not opt.btn.isEnabled()                      # nothing to report yet
        w._set_tree(tree, scenes)
        w.solve_panel.show_results(tree, next(iter(tree.groups)))
        assert opt.btn.isEnabled()
        opt.btn.click()
        written, = tmp_path.glob("*_kipi_report_*.html")
        assert tree.report_path == written
        assert [Path(p) for p in opened_in_browser] == [written]
        assert w.solve_panel.btn_report.isVisibleTo(w.solve_panel)
    finally:
        w.close()
