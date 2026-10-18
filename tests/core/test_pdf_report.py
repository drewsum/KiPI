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


def _notes_field(data: bytes):
    """The notes field the PDF ends with, checked as a reader finds it: through the last
    cross-reference section (each offset at its object), the catalog's form and page 1."""
    import re
    xref = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    table = data[xref:data.index(b"trailer", xref)]
    offsets = {int(n): int(o) for n, o in re.findall(rb"(\d+) 1\n(\d{10}) 00000 n", table)}
    for n, off in offsets.items():
        assert data[off:].startswith(f"{n} 0 obj".encode())

    def obj(n):
        start = offsets.get(n)
        if start is None:                                  # unchanged: Qt's own
            start = re.search(rf"(?m)^{n} 0 obj".encode(), data).start()
        return data[start:data.index(b"endobj", start)]

    trailer = data[data.rindex(b"trailer"):]
    assert b"/Prev " in trailer
    catalog = obj(int(re.search(rb"/Root (\d+) 0 R", trailer).group(1)))
    field = int(re.search(rb"/AcroForm << /Fields \[(\d+) 0 R\]", catalog).group(1))
    text = obj(field)
    pages = obj(int(re.search(rb"/Pages (\d+) 0 R", catalog).group(1)))
    kids = re.findall(rb"(\d+) 0 R", re.search(rb"/Kids\s*\[([^\]]*)\]", pages).group(1))
    page = re.search(rb"/P (\d+) 0 R", text).group(1)
    assert page in kids
    annots = re.search(rb"/Annots (\d+) 0 R", obj(int(page)))
    assert f"{field} 0 R".encode() in obj(int(annots.group(1)))
    assert b"/FT /Tx" in text and b"/Ff 4096" in text and b"/T (User notes)" in text
    return [float(x) for x in re.search(rb"/Rect \[([^\]]+)\]", text).group(1).split()]


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
    # The User notes: a box after every result, with a fillable field over it.
    assert html.rindex("<h2") == html.index("<h2 id='user-notes'")
    assert "Click in the box" not in html
    x0, y0, x1, y1 = _notes_field(data)
    assert x1 - x0 > 400 and y1 - y0 == pytest.approx(pdf.NOTES_HEIGHT_PT, abs=12)


def test_ac_report_as_pdf(ac, tmp_path, printed):
    result, snap, s = ac
    path = write_ac_report(tmp_path / "r.pdf", result, snap, s)
    assert path.read_bytes().startswith(b"%PDF")
    _notes_field(path.read_bytes())
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


def test_another_reader_fills_the_notes_and_keeps_them(dc, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    tree, scenes, snap, s = dc
    path = write_report(tmp_path / "r.pdf", tree, scenes, snap, s)
    reader = pypdf.PdfReader(path, strict=True)
    assert list(reader.get_fields()) == ["User notes"]
    writer = pypdf.PdfWriter(clone_from=reader)
    writer.update_page_form_field_values(writer.pages[0], {"User notes": "Checked (A.B.)"})
    writer.write(tmp_path / "filled.pdf")
    filled = pypdf.PdfReader(tmp_path / "filled.pdf").get_fields()
    assert filled["User notes"]["/V"] == "Checked (A.B.)"


def test_the_box_stays_when_the_field_cannot_be_added(tmp_path, monkeypatch):
    def broken(*a):
        raise ValueError("odd PDF")

    monkeypatch.setattr(pdf, "add_text_field", broken)
    html = f"<h2>User notes</h2><table class='notes'><tr><td>{pdf.NOTES_MARKER}</td></tr></table>"
    data = pdf.write_pdf(html, tmp_path / "r.pdf").read_bytes()
    assert data.startswith(b"%PDF") and b"/AcroForm" not in data


def test_no_marker_no_field(tmp_path):
    data = pdf.write_pdf("<p>no notes</p>", tmp_path / "r.pdf").read_bytes()
    assert b"/AcroForm" not in data


def test_a_box_that_would_split_starts_a_page(tmp_path):
    box = f"<table class='notes'><tr><td>{pdf.NOTES_MARKER}</td></tr></table>"
    for n in range(30, 80, 4):                 # some of these land across a page break
        path = pdf.write_pdf("<p>line</p>" * n + box, tmp_path / f"r{n}.pdf")
        x0, y0, x1, y1 = _notes_field(path.read_bytes())
        assert y1 - y0 == pytest.approx(pdf.NOTES_HEIGHT_PT, abs=12)    # whole, on one page


@pytest.mark.parametrize("annots", ["", "/Annots [7 0 R]"])
def test_the_field_joins_any_page(tmp_path, annots):
    """Pages with no annotations, or a direct array of them, take the field as well."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] {annots} >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += b"xref\n0 4\n0000000000 65535 f \n" + b"".join(
        f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path = tmp_path / "p.pdf"
    path.write_bytes(out)
    pdf.add_text_field(path, 0, (10, 10, 200, 100), "User notes")
    data = path.read_bytes()
    assert data.startswith(out)                      # the original bytes kept
    page = data[data.rindex(b"3 0 obj"):]
    assert b"/Annots [4 0 R" in page and (b"7 0 R" in page) == bool(annots)
    assert b"/AcroForm << /Fields [4 0 R]" in data and f"/Prev {xref}".encode() in data
