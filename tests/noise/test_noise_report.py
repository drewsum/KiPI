# Portions Copyright (c) 2026 Tomachie LLC.
"""The Coupled Noise report, the victims a solve covers, and the scan's controls (#69)."""

import os

import pytest

from kipi.noise.html_report import report_name, write_noise_report
from kipi.noise.solver import solve_noise
from validation_boards import open_board

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def board():
    snap, setup = open_board("coupled_noise")
    setup.victims = []                       # the scan finds them
    setup.noise.board_model = False
    setup.noise.scan = True
    return snap, setup, solve_noise(snap, setup, "U1")


def test_the_report_has_every_section(board, tmp_path):
    snap, setup, r = board
    path = write_noise_report(tmp_path / report_name("cn", "U1"), r, snap, setup)
    text = path.read_text(encoding="utf-8")
    for section in ("id='victims'", "id='aggressor'", "id='waveforms'", "id='noise'",
                    "id='scan'", "id='emi'", "id='notes'"):
        assert section in text, section
    assert "SIG_LOOP" in text and "data:image/png" in text
    assert "hotspots" in text and "CISPR" in text and "Radiated estimate" in text
    assert report_name("cn", "U1 / buck").startswith("cn_U1_buck_noise_report_")
    # #222: the tables come after every plot, and the plots are shown at half their pixels
    last_plot = text.rindex("data:image/png")
    for table in ("id='victims'", "id='hot-loops'", "id='scan'", "id='hotspots'"):
        assert last_plot < text.index(table) < text.index("id='user-notes'"), table
    assert "<img src='data:image/png;base64," in text and "' width='" in text


def test_the_pdf_plots_fit_the_page(board, tmp_path, monkeypatch):
    """Qt's rich text takes no max-width: without a width, a plot is wider than the page
    and cut off on the right."""
    import re

    from qtpy.QtWidgets import QApplication

    from kipi.core import pdf
    QApplication.instance() or QApplication([])
    got = []
    real = pdf.write_pdf
    monkeypatch.setattr(pdf, "write_pdf", lambda html, path, title="", **kw: (
        got.append(html), real(html, path, title, **kw))[1])
    snap, setup, r = board
    path = write_noise_report(tmp_path / "r.pdf", r, snap, setup)
    assert path.stat().st_size > 0
    imgs = re.findall(r"<img src='data:image/png[^>]*>", got[0])
    assert imgs and all("width='680'" in m for m in imgs)


@pytest.fixture
def panel():
    from qtpy.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from kipi.noise.gui.panel import NoisePanel
    snap, setup = open_board("coupled_noise")
    p = NoisePanel()
    p.set_board(snap, setup)
    yield p
    p.close()


def test_the_victims_choice_and_scan_reach_the_setup(panel):
    items = [panel.cmb_victims.itemData(i) for i in range(panel.cmb_victims.count())]
    assert items == ["all", "SIG_SW", "SIG_LOOP", "SIG_BURIED", "none"]
    panel.cmb_victims.setCurrentIndex(items.index("SIG_LOOP"))
    assert panel.setup.noise.victims == "SIG_LOOP"
    panel.chk_scan.setChecked(True)
    assert panel.setup.noise.scan
    panel.spin_top.setValue(3)
    assert panel.setup.noise.scan_top_n == 3
    panel.victim_list.lst.setCurrentRow(0)
    panel.victim_list._remove()                    # a removed victim leaves the choice
    assert panel.cmb_victims.count() == 4


def test_scanned_victims_are_marked_in_the_table(panel, board):
    _, _, r = board
    picked = []
    panel.victim_selected.connect(picked.append)
    panel.show_result(r)
    names = [panel.tbl_victims.item(i, 0).text() for i in range(panel.tbl_victims.rowCount())]
    assert "SIG_LOOP (scan)" in names
    assert picked and not picked[-1].endswith("(scan)")
