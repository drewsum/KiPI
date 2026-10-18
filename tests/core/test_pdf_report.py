"""PDF reports (#119): the DC and AC reports printed without what needs a browser, the
setting that picks the format, the panels' option, and HTML where a PDF cannot be made."""

import os
from datetime import datetime

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("matplotlib")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from ac_boards import plane_pair, plane_setup  # noqa: E402
from kipi.ac.html_report import report_name as ac_report_name  # noqa: E402
from kipi.ac.html_report import write_ac_report  # noqa: E402
from kipi.ac.model import CapModel  # noqa: E402
from kipi.ac.solver import solve_ac  # noqa: E402
from kipi.core import pdf  # noqa: E402
from kipi.dc.pi.model import PISetup  # noqa: E402
from kipi.dc.pi.tree import solve_tree  # noqa: E402
from kipi.dc.report.html import report_name, write_report  # noqa: E402
from kipi.dc.viz.results import build_result_scene  # noqa: E402
from tree_boards import board, setup  # noqa: E402

WHEN = datetime(2026, 10, 15, 9, 5, 7)


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def dc():
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    tree = solve_tree(snap, s)
    scenes = {n: build_result_scene(snap, g.result) for n, g in tree.groups.items()}
    return tree, scenes, snap, s


@pytest.fixture(scope="module")
def ac():
    snap = plane_pair(caps=[("C1", "100n", (30, 30)), ("C3", "10u", (10, 40))])
    s = plane_setup(snap, f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=10,
                    field_points_per_decade=3)
    s.caps = [CapModel("100n", 100e-9, 0.4e-9, 0.02, ["C1"])]
    s.probes[0].target_ohm, s.probes[0].target_hz = 0.05, 50e6
    return solve_ac(snap, s, "VRM1", "GND"), snap, s


@pytest.fixture
def printed(monkeypatch):
    """The HTML each report hands to the PDF writer, which still writes the file."""
    got = []
    real = pdf.write_pdf
    monkeypatch.setattr(pdf, "write_pdf", lambda html, path, title="": (
        got.append(html), real(html, path, title))[1])
    return got


def _static(html):
    assert "<script" not in html and "plotly" not in html.lower()
    assert "contenteditable" not in html and "<details" not in html and "<nav>" not in html


def test_dc_report_as_pdf(dc, tmp_path, printed):
    tree, scenes, snap, s = dc
    path = write_report(tmp_path / "r.pdf", tree, scenes, snap, s)
    data = path.read_bytes()
    assert data.startswith(b"%PDF") and data.count(b"/Subtype /Image") >= 2 * len(tree.groups)
    html, = printed
    _static(html)
    for l in tree.loads:
        assert l.name in html
    assert "id='pins'" in html and "id='loads'" in html
    assert html.count("data:image/png;base64,") >= 2 * len(tree.groups)
    assert not (tmp_path / "r.html").exists()


def test_ac_report_as_pdf(ac, tmp_path, printed):
    result, snap, s = ac
    path = write_ac_report(tmp_path / "r.pdf", result, snap, s)
    assert path.read_bytes().startswith(b"%PDF")
    html, = printed
    _static(html)
    for anchor in ("probes", "impedance", "map", "caps", "model"):
        assert f"id='{anchor}'" in html
    assert html.count("data:image/png;base64,") == 2       # impedance plot and board map
    assert "C3" in html and "no model" in html


def test_the_html_reports_are_unchanged(dc, ac, tmp_path):
    tree, scenes, snap, s = dc
    text = write_report(tmp_path / "r.html", tree, scenes, snap, s).read_text("utf-8")
    assert text.count("Plotly.newPlot") == len(tree.groups) and "contenteditable" in text
    result, asnap, a = ac
    text = write_ac_report(tmp_path / "a.html", result, asnap, a).read_text("utf-8")
    assert "Plotly.newPlot" in text and "contenteditable" in text


def test_report_names_end_in_the_format():
    assert report_name("board", WHEN, fmt="pdf") == "board_kipi_report_2026-10-15_090507.pdf"
    assert ac_report_name("board", "+3.3V", WHEN, fmt="pdf") == \
        "board_+3.3V_ac_report_2026-10-15_090507.pdf"


def test_the_format_is_saved(tmp_path):
    s = PISetup(html_report=True, report_format="pdf")
    s.ac.report_format = "pdf"
    path = s.save(tmp_path / "s.kipi-setup.yml")
    back = PISetup.load(path)
    assert back.report_format == "pdf" and back.ac.report_format == "pdf"
    d = s.to_dict()
    d["report_format"], d["ac"]["report_format"] = "docx", None
    back = PISetup.from_dict(d)
    assert back.report_format == "html" and back.ac.report_format == "html"
    del d["report_format"], d["ac"]["report_format"]           # a setup from before #119
    back = PISetup.from_dict(d)
    assert back.report_format == "html" and back.ac.report_format == "html"


def test_html_where_a_pdf_cannot_be_made(tmp_path):
    from kipi.core.progress import ProgressReporter
    from kipi.gui.main_window import write_report_file

    def write(path):
        if path.suffix == ".pdf":
            raise pdf.PdfError("no Qt")
        path.write_text("<html></html>")
        return path

    warned = []
    progress = ProgressReporter()
    progress.warn = warned.append
    out = write_report_file(write, tmp_path / "r.pdf", progress)
    assert out == tmp_path / "r.html" and out.is_file()
    assert "no Qt" in warned[0]


def test_qt_failures_become_pdf_errors(tmp_path, monkeypatch):
    def broken(*a):
        raise ValueError("bad page")

    monkeypatch.setattr(pdf, "_print", broken)
    with pytest.raises(pdf.PdfError, match="bad page"):
        pdf.write_pdf("<p>x</p>", tmp_path / "r.pdf")
    monkeypatch.setattr(pdf, "_print", lambda *a: None)            # wrote nothing
    with pytest.raises(pdf.PdfError, match="could not write"):
        pdf.write_pdf("<p>x</p>", tmp_path / "r.pdf")


@pytest.mark.parametrize("mode", ["dc", "ac"])
def test_panel_report_option(mode, ac, dc):
    if mode == "dc":
        from kipi.dc.gui.pi_panel import PIPanel
        tree, scenes, snap, s = dc
        s.html_report, s.report_format = True, "pdf"
        p = PIPanel()
        p.set_board(snap, s, "test")
        settings = s
    else:
        from kipi.ac.gui.panel import ACPanel
        result, snap, s = ac
        s.ac.html_report, s.ac.report_format = True, "pdf"
        p = ACPanel()
        p.set_board(snap, s, "test")
        p.set_nets(["+3V3", "GND"])
        settings = s.ac
    opt = p.report_option
    assert opt.chk.isChecked() and opt.fmt() == "pdf" and opt.cmb.isEnabled()
    opt.cmb.setCurrentIndex(opt.cmb.findData("html"))
    assert settings.report_format == "html"
    opt.chk.setChecked(False)
    assert not settings.html_report and not opt.cmb.isEnabled()
