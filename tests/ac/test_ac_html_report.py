# Portions Copyright (c) 2026 Tomachie LLC.
"""The AC HTML report: written from a real solve, self-contained, with every section; and
the AC panel's HTML report option."""

import os
from datetime import datetime

import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.html_report import report_name, write_ac_report
from kipi.ac.model import CapModel
from kipi.ac.solver import solve_ac
from kipi.dc.pi.model import PISetup


@pytest.fixture(scope="module")
def solved():
    snap = plane_pair(caps=[("C1", "100n", (30, 30)), ("C2", "100n", (40, 10)),
                            ("C3", "10u", (10, 40))])
    setup = plane_setup(snap, f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20,
                        field_points_per_decade=4)
    setup.caps = [CapModel("100n", 100e-9, 0.4e-9, 0.02, ["C1", "C2"])]   # C3 has none
    setup.probes[0].target_ohm, setup.probes[0].target_hz = 0.05, 50e6
    return snap, setup, solve_ac(snap, setup, "VRM1", "GND")


def test_report_has_every_section(solved, tmp_path):
    snap, setup, result = solved
    path = write_ac_report(tmp_path / "r.html", result, snap, setup)
    text = path.read_text(encoding="utf-8")
    for anchor in ("probes", "impedance", "map", "caps", "model", "notes", "user-notes"):
        assert f"id='{anchor}'" in text
    assert "Plotly.newPlot" in text and "<script src=" not in text     # works offline
    assert "contenteditable='true'" in text and "href='#user-notes'" in text    # #128
    assert text.rindex("<h2") == text.index("<h2 id='user-notes'")     # the last section
    assert "100n (one part)" in text                                   # model curve
    assert "C3" in text and "no model" in text                         # left out, mapped
    import re
    ids = re.findall(r"""id=['"]([^'"]+)['"]""", text)
    assert len(ids) == len(set(ids))      # else Plotly draws a plot into a heading
    assert "phase" not in text.lower().split("<h2 id='impedance'>")[1].split("<h2")[0]
    verdict = "PASS" if result.probes[0].passed(result.freqs) else "FAIL"
    assert f"class=\"{verdict.lower()}\">{verdict}<" in text


def test_report_without_targets_or_caps(tmp_path):
    snap = plane_pair()
    setup = plane_setup(snap, f_start_hz=1e6, f_stop_hz=1e7, points_per_decade=10,
                        field_points_per_decade=2)
    result = solve_ac(snap, setup, "VRM1", "GND")
    text = write_ac_report(tmp_path / "r.html", result, snap, setup).read_text("utf-8")
    assert "<p>Overall:" not in text and 'id="zplot"' in text


def test_report_name_is_safe_for_files():
    when = datetime(2026, 10, 15, 14, 30, 12)
    assert report_name("board", "+3.3V", when) == "board_+3.3V_ac_report_2026-10-15_143012.html"
    assert report_name("board", "A/B: core", when) == \
        "board_A_B_core_ac_report_2026-10-15_143012.html"
    assert report_name("board", "///", when) == "board_rail_ac_report_2026-10-15_143012.html"


def test_report_format_is_saved(tmp_path):
    setup = PISetup()
    setup.ac.report_format = "pdf"
    path = tmp_path / "s.kipi-setup.yml"
    setup.save(path)
    assert PISetup.load(path).ac.report_format == "pdf"


def test_panel_option_and_report_button(solved, tmp_path):
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.ac.gui.panel import ACPanel
    QApplication.instance() or QApplication([])
    snap, setup, result = solved
    p = ACPanel()
    p.set_board(snap, setup)
    p.set_nets(["+3V3", "GND"])
    assert not p.report_option.btn.isEnabled()
    result.report_path = None
    p.show_result(result)
    assert p.btn_report.isHidden() and p.report_option.btn.isEnabled()
    p.set_report(result, tmp_path / "r.html")         # a report written on demand (#185)
    assert not p.btn_report.isHidden() and result.report_path == tmp_path / "r.html"
    p.show_result(result)
    assert not p.btn_report.isHidden()
    p.close()
    result.report_path = None
    assert np.all(np.isfinite(result.probes[0].mag))
