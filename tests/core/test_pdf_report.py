"""PDF reports (#119): the DC and AC reports printed without what needs a browser, the
setting that picks the format, the panels' option, and HTML where a PDF cannot be made. The
DC report's 3D views go in it as an attached web page (#133)."""

import os
import re
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
    monkeypatch.setattr(pdf, "write_pdf", lambda html, path, title="", **kw: (
        got.append(html), real(html, path, title, **kw))[1])
    return got


def _objects(data: bytes):
    """Each object's last definition, as a reader finds it: through the last
    cross-reference section (each offset at its object), then the ones before."""
    import re
    xref = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    table = data[xref:data.index(b"trailer", xref)]
    offsets = {int(n): int(o) for n, o in re.findall(rb"(\d+) 1\n(\d{10}) 00000 n", table)}
    for n, off in offsets.items():
        assert data[off:].startswith(f"{n} 0 obj".encode())

    def obj(n):
        start = offsets.get(n)
        if start is None:                                  # an earlier section's
            start = [m.start() for m in re.finditer(rf"(?m)^{n} 0 obj".encode(), data)][-1]
        return data[start:data.index(b"endobj", start)]

    return obj


def _attached(data: bytes):
    """{name: content} of the files the PDF ends with, found from its catalog."""
    import re
    import zlib
    obj = _objects(data)
    trailer = data[data.rindex(b"trailer"):]
    catalog = obj(int(re.search(rb"/Root (\d+) 0 R", trailer).group(1)))
    assert b"/PageMode /UseAttachments" in catalog
    names = obj(int(re.search(rb"/Names (\d+) 0 R", catalog).group(1)))
    tree = re.search(rb"/EmbeddedFiles << /Names \[(.*?)\] >>", names, re.S).group(1)
    out = {}
    for name, spec in re.findall(rb"\(([^)]*)\) (\d+) 0 R", tree):
        stream = obj(int(re.search(rb"/EF << /F (\d+) 0 R", obj(int(spec))).group(1)))
        assert b"/Type /EmbeddedFile" in stream and b"/FlateDecode" in stream
        length = int(re.search(rb"/Length (\d+)", stream).group(1))
        start = stream.index(b"stream\n") + 7
        out[name.decode()] = zlib.decompress(stream[start:start + length])
    return out


def _page_annots(data: bytes):
    """[[annotation, ...] per page] the PDF ends with, found from its catalog."""
    import re
    obj = _objects(data)
    trailer = data[data.rindex(b"trailer"):]
    catalog = obj(int(re.search(rb"/Root (\d+) 0 R", trailer).group(1)))
    pages = obj(int(re.search(rb"/Pages (\d+) 0 R", catalog).group(1)))
    out = []
    for kid in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids\s*\[([^\]]*)\]", pages).group(1)):
        page = obj(int(kid))
        m = re.search(rb"/Annots (\d+) 0 R", page)
        arr = obj(int(m.group(1))) if m else (
            re.search(rb"/Annots\s*\[(.*?)\]", page, re.S) or [b"", b""])[1]
        out.append([obj(int(n)) for n in re.findall(rb"(\d+) 0 R", arr)])
    return out


def _notes_field(data: bytes):
    """The notes field the PDF ends with, checked as a reader finds it: through the
    catalog's form and page 1."""
    import re
    obj = _objects(data)
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
    # Qt reads every heading as one, so a heading at a page's foot can be moved.
    from qtpy.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setHtml(html)
    assert pdf._heading_count(doc) == len(pdf._HEADING.findall(html)) > 3


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
    # The 3D views: a web page of their own in the PDF, named in it.
    views = _attached(data)
    assert list(views) == ["r_3d.html"] and "r_3d.html" in html
    page = views["r_3d.html"].decode("utf-8")
    assert page.count("Plotly.newPlot") == len(tree.groups) and "from r.pdf" in page
    for name in tree.groups:
        assert f"<h2>{name}</h2>" in page
    assert not (tmp_path / "r_3d.html").exists()
    # Linked from the top and from each group, each link with a paperclip beside it.
    assert "<b>3D Results View:</b>" in html
    assert html.count(pdf.file_link("r_3d.html")) == len(tree.groups) + 1
    annots = [a for page in _page_annots(data) for a in page]
    assert sum(b"exportDataObject" in a for a in annots) == len(tree.groups) + 1
    assert sum(b"/Subtype /FileAttachment" in a for a in annots) == len(tree.groups) + 1
    # The User notes: a box after every result, with a fillable field over it.
    assert html.rindex("<h2") == html.index("<h2 id='user-notes'")
    assert "Click in the box" not in html
    x0, y0, x1, y1 = _notes_field(data)
    assert x1 - x0 > 400 and y1 - y0 == pytest.approx(pdf.NOTES_HEIGHT_PT, abs=12)


def test_ac_report_as_pdf(ac, tmp_path, printed):
    result, snap, s = ac
    path = write_ac_report(tmp_path / "r.pdf", result, snap, s)
    assert path.read_bytes().startswith(b"%PDF") and b"/EmbeddedFiles" not in path.read_bytes()
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
    assert "attached" not in text and not (tmp_path / "r_3d.html").exists()
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
    page, = [p for p in writer.pages if "/Annots" in p and any(
        a.get_object().get("/T") == "User notes" for a in p["/Annots"])]   # the last page
    writer.update_page_form_field_values(page, {"User notes": "Checked (A.B.)"})
    writer.write(tmp_path / "filled.pdf")
    filled = pypdf.PdfReader(tmp_path / "filled.pdf").get_fields()
    assert filled["User notes"]["/V"] == "Checked (A.B.)"


def test_another_reader_finds_the_attachment(dc, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    tree, scenes, snap, s = dc
    path = write_report(tmp_path / "r.pdf", tree, scenes, snap, s)
    reader = pypdf.PdfReader(path, strict=True)
    content, = reader.attachments["r_3d.html"]
    assert b"Plotly.newPlot" in content
    assert list(reader.get_fields()) == ["User notes"]


def test_attachments_go_beside_where_they_cannot_go_in(tmp_path, monkeypatch):
    def broken(*a):
        raise ValueError("odd PDF")

    monkeypatch.setattr(pdf, "attach_files", broken)
    path = pdf.write_pdf("<p>x</p>", tmp_path / "r.pdf",
                         attachments=[("r_3d.html", b"<html>3D</html>", "views")])
    assert b"/EmbeddedFiles" not in path.read_bytes()
    assert (tmp_path / "r_3d.html").read_bytes() == b"<html>3D</html>"


def test_files_go_in_with_the_notes_field(tmp_path):
    box = f"<table class='notes'><tr><td>{pdf.NOTES_MARKER}</td></tr></table>"
    files = [("b é.html", "<p>ü</p>".encode(), "Views — 3D"), ("a.txt", b"x" * 5000, "a")]
    path = pdf.write_pdf("<p>x</p>" + box, tmp_path / "r.pdf", attachments=files)
    data = path.read_bytes()
    _notes_field(data)
    got = _attached(data)
    assert list(got) == ["a.txt", "b _.html"]            # sorted; the plain name an ASCII one
    assert got["a.txt"] == b"x" * 5000 and got["b _.html"] == "<p>ü</p>".encode()
    assert pdf._pdf_text("b é.html") in data             # the whole name, for the viewer
    assert b"/Subtype /text#2Fhtml" in data and b"/Subtype /text#2Fplain" in data


def test_a_link_opens_its_attachment(tmp_path):
    """A link to an attached file runs Acrobat's JavaScript to open it, with a paperclip
    beside it, on the same page, holding the file, for viewers without that."""
    box = f"<table class='notes'><tr><td>{pdf.NOTES_MARKER}</td></tr></table>"
    html = (f"<p>3D: <a href='{pdf.file_link('v.html')}'>v.html</a></p>"
            + "<p>line</p>" * 80 + f"<p>again <a href='{pdf.file_link('v.html')}'>v.html</a></p>"
            f"<p><a href='{pdf.file_link('gone.html')}'>not attached</a></p>" + box)
    path = pdf.write_pdf(html, tmp_path / "r.pdf",
                         attachments=[("v.html", b"<p>3D</p>", "views")])
    data = path.read_bytes()
    assert _attached(data) == {"v.html": b"<p>3D</p>"}
    _notes_field(data)
    pages = _page_annots(data)
    assert len(pages) > 1
    for page in pages[0], pages[-1]:
        link, = [a for a in page if b"exportDataObject" in a]
        assert b'cName: "v.html"' in link and b"nLaunch: 2" in link
        assert b"/URI" not in link
        clip, = [a for a in page if b"/Subtype /FileAttachment" in a]
        assert b"/Name /Paperclip" in clip and b"/AP << /N " in clip
        spec = int(re.search(rb"/FS (\d+) 0 R", clip).group(1))
        assert b"(v.html)" in _objects(data)(spec)
        lx0, ly0, lx1, ly1 = _rect(link)
        cx0, cy0, cx1, cy1 = _rect(clip)
        assert cx0 == pytest.approx(lx1 + pdf.CLIP_GAP_PT, abs=0.01)
        assert cy0 < (ly0 + ly1) / 2 < cy1                     # beside it, on its line
    # A link to a file not attached is left as Qt wrote it.
    assert sum(b"gone.html" in a for page in pages for a in page) == 1


def test_links_open_the_files_beside_where_they_cannot_go_in(tmp_path, monkeypatch):
    def broken(*a):
        raise ValueError("odd PDF")

    monkeypatch.setattr(pdf, "attach_files", broken)
    html = f"<p><a href='{pdf.file_link('r_3d.html')}'>r_3d.html</a></p>"
    data = pdf.write_pdf(html, tmp_path / "r.pdf",
                         attachments=[("r_3d.html", b"<html>3D</html>", "views")]).read_bytes()
    link, = [a for page in _page_annots(data) for a in page if b"/Subtype /Link" in a]
    assert b"/S /Launch /F (r_3d.html)" in link and b"/URI" not in link
    assert (tmp_path / "r_3d.html").is_file()


def _rect(annot: bytes):
    return [float(x) for x in re.search(rb"/Rect \[([^\]]+)\]", annot).group(1).split()]


@pytest.mark.parametrize("names", ["", "/Names 5 0 R"])
def test_files_join_any_catalog(tmp_path, names):
    """A catalog with no names, or Qt's names object, takes the attachments."""
    objs = [f"<< /Type /Catalog /Pages 2 0 R {names} >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>", "<< >>", "<< >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
        f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path = tmp_path / "p.pdf"
    path.write_bytes(out)
    pdf.attach_files(path, [("v.html", b"<p>3D</p>", "views")])
    data = path.read_bytes()
    assert data.startswith(out) and f"/Prev {xref}".encode() in data
    assert _attached(data) == {"v.html": b"<p>3D</p>"}
    with pytest.raises(ValueError, match="page mode"):
        pdf.attach_files(path, [("w.html", b"", "")])


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


def test_a_heading_goes_with_what_it_heads(tmp_path, monkeypatch):
    """A heading the page would end under starts the next page, with its table, or with
    the paragraph under a heading straight after it."""
    from qtpy.QtCore import QSizeF
    from qtpy.QtGui import QTextDocument
    laid = []
    real = pdf._break_before
    monkeypatch.setattr(pdf, "_break_before", lambda html, which: (
        laid.append(real(html, which)), laid[-1])[1])
    moved = 0
    for n in range(30, 80):
        laid.clear()
        pdf.write_pdf("<p>line</p>" * n + "<h2>Table</h2><table><tr><td>a</td></tr>"
                      "<tr><td>b</td></tr></table><h2>Two</h2><h3>headings</h3><p>text</p>",
                      tmp_path / f"r{n}.pdf")
        moved += "page-break-before" in laid[-1]
        writer = pdf._writer(tmp_path / "w.pdf")
        doc = QTextDocument()
        doc.setDefaultStyleSheet(pdf.CSS)
        doc.documentLayout().setPaintDevice(writer)
        doc.setPageSize(QSizeF(writer.width(), writer.height()))
        doc.setHtml(laid[-1].replace("<table", "<table width='100%'"))
        doc.setDocumentMargin(0)
        page, block = {}, doc.begin()
        while block.isValid():
            if block.text() in ("Table", "a", "Two", "headings", "text"):
                top, bottom = pdf._line(doc.documentLayout(), block)
                page[block.text()] = int((bottom - 1) // writer.height())
            block = block.next()
        assert page["Table"] == page["a"] and page["Two"] == page["headings"] == page["text"]
    assert moved                                # some of these needed a page break


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
