"""Print a report's HTML to a PDF file (#119), with Qt's rich text engine.

PySide6 is already one of KiPI's packages, so this needs nothing more; a browser engine
(QtWebEngine) would be far bigger and is not in PySide6-Essentials. Qt's rich text takes
tables, headings, colors and images, but no scripts and only a little CSS, so the reports
make a static version of themselves for it: matplotlib images in place of the Plotly plots,
plain headings in place of the collapsible sections, and the stylesheet below.

Where the HTML has :data:`NOTES_MARKER`, the PDF gets a box with a fillable text field over
it: the User notes, typed and saved in any PDF viewer that fills forms. Qt cannot make form
fields, so the field is added to Qt's file afterwards as an incremental update (new objects
and a cross-reference section appended; Qt's own bytes are left as they are). Should that
fail, the PDF keeps the empty box, to write in on paper.

A heading the page ends under starts the next page instead, with what it heads.

Safe on a worker thread (QTextDocument and QPdfWriter need no window), but the application
must exist: Qt needs it for fonts.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

PAGE_MARGIN_MM = 12
NOTES_MARKER = "[[kipi-user-notes]]"     # alone in a table cell: that table is the notes box
NOTES_NAME = "User notes"
NOTES_IMAGE = "kipi-notes-box"
NOTES_HEIGHT_PT = 110                    # about eight lines at 10 pt
NOTES_INSET_PT = 2                       # the field inside the box's border
NOTES_FONT_PT = 10
# What Qt's rich text understands of the reports' style.
CSS = """
body { font-family: "Segoe UI", Helvetica, Arial, sans-serif; color: #222; font-size: 9pt; }
h1 { font-size: 17pt; margin-bottom: 0; }
h2 { font-size: 13pt; margin-top: 18px; color: #333; }
h3 { font-size: 10.5pt; margin-top: 12px; }
.meta { color: #666; }
table { border-collapse: collapse; margin-top: 4px; margin-bottom: 10px; }
th, td { border: 1px solid #ccc; padding: 2px 4px; text-align: right; font-size: 7.5pt; }
th { background-color: #f0f0f0; }
.pass { color: #1a8f2c; font-weight: bold; } .fail { color: #c62828; font-weight: bold; }
.unjudged { color: #b26a00; font-weight: bold; }
.note { color: #666; font-size: 8pt; }
table.maps td { border: none; padding: 2px; }
table.notes td { border: 1px solid #999; text-align: left; }
"""
# Qt sizes a table to its contents; the full page width spreads a wide one's columns out
# instead of breaking their words.
_TABLE = re.compile(r"<table(?! class='maps')")
_HEADING = re.compile(r"<h[1-6]\b")
_PAGE_BREAK = " style='page-break-before: always'"
_DATA_IMAGE = re.compile(r"""src=(['"])data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)\1""")


class PdfError(RuntimeError):
    """The PDF could not be made here (no Qt application, or Qt failed)."""


def write_pdf(html: str, path, title: str = "") -> Path:
    """Lay out ``html`` on pages (Letter where the locale measures in inches, else A4) and
    write it to ``path``. Raises :class:`PdfError` whatever goes wrong."""
    path = Path(path)
    try:
        from qtpy.QtGui import QGuiApplication
        running = QGuiApplication.instance() is not None
    except Exception as e:  # noqa: BLE001 - no Qt at all
        raise PdfError(f"Qt is not available ({e})") from e
    if not running:
        raise PdfError("PDF reports need KiPI's window (Qt) to be running")
    try:
        notes = _print(html, path, title)
    except Exception as e:  # noqa: BLE001 - any Qt failure: the caller falls back
        raise PdfError(f"{type(e).__name__}: {e}") from e
    if not path.is_file() or path.stat().st_size == 0:
        raise PdfError(f"Qt could not write {path}")
    if notes is not None:
        try:
            add_text_field(path, *notes, NOTES_NAME)
        except Exception as e:  # noqa: BLE001 - the box stays, to write in on paper
            log.warning("The PDF report's notes box is not fillable: %s", e)
    return path


def _print(html: str, path: Path, title: str) -> Optional[Tuple[int, tuple]]:
    """Print ``html`` page by page. Returns where the notes box landed (page index and its
    rectangle in PDF points), None without one."""
    from qtpy.QtCore import QRectF, QSizeF, QUrl
    from qtpy.QtGui import QImage, QPageLayout, QPainter, QTextDocument

    doc = QTextDocument()
    doc.setDefaultStyleSheet(CSS)
    # The images go in as resources: not every Qt version reads data: URLs in rich text.
    images = []

    def resource(m):
        img = QImage.fromData(base64.b64decode(m.group(3)), m.group(2).upper())
        name = f"kipi-image:{len(images)}"
        images.append((name, img))
        return f"src='{name}'"

    html = _TABLE.sub("<table width='100%'", _DATA_IMAGE.sub(resource, html))
    for name, img in images:
        doc.addResource(QTextDocument.ImageResource, QUrl(name), img)
    # The notes box: the marker's one-cell table, held open by a blank image (sized in
    # 96 dpi pixels; the layout scales images to the writer). It goes in with the HTML:
    # editing the document once it is laid out makes Qt drop headings further down.
    blank = QImage(1, 1, QImage.Format_ARGB32)
    blank.fill(0)
    doc.addResource(QTextDocument.ImageResource, QUrl(NOTES_IMAGE), blank)
    html = html.replace(NOTES_MARKER, f"<img src='{NOTES_IMAGE}' width='1' "
                                      f"height='{round(NOTES_HEIGHT_PT * 96 / 72)}'>")
    writer = _writer(path, title)

    # Laid out on the writer itself, in its pixels, one page at a time (what print() does,
    # but here the notes box's place on the page is known).
    res = writer.resolution()
    doc.documentLayout().setPaintDevice(writer)
    w, h = writer.width(), writer.height()
    doc.setPageSize(QSizeF(w, h))

    def load(text):
        doc.setHtml(text)
        doc.setDocumentMargin(0)           # the page margins are the writer's

    # A heading the page ends under goes over with what it heads: one page break at a
    # time, the first such heading on, each laid out again (a break moves all after it).
    breaks = set()
    load(html)
    while _heading_count(doc) == len(_HEADING.findall(html)):  # each one the HTML's
        i = _stranded_heading(doc, h)
        if i is None or i in breaks:
            break
        breaks.add(i)
        load(_break_before(html, breaks))
    html = _break_before(html, breaks)
    layout = doc.documentLayout()
    notes = None
    table = _notes_table(doc)
    if table is not None:
        box = layout.frameBoundingRect(table)
        if box.bottom() > (box.top() // h + 1) * h:           # split by a page break
            load(html.replace("<table width='100%' class='notes'",
                                     "<table width='100%' class='notes' "
                                     "style='page-break-before: always'"))
            table = _notes_table(doc)
            box = layout.frameBoundingRect(table)
        page = int(box.top() // h)
        paint = writer.pageLayout().paintRect(QPageLayout.Point)
        full = writer.pageLayout().fullRect(QPageLayout.Point)
        pt = 72.0 / res
        x0 = paint.left() + box.left() * pt + NOTES_INSET_PT
        x1 = paint.left() + box.right() * pt - NOTES_INSET_PT
        top = paint.top() + (box.top() - page * h) * pt + NOTES_INSET_PT
        bottom = paint.top() + (box.bottom() - page * h) * pt - NOTES_INSET_PT
        notes = (page, (x0, full.height() - bottom, x1, full.height() - top))

    painter = QPainter(writer)
    try:
        for i in range(doc.pageCount()):
            if i:
                writer.newPage()
            painter.save()
            painter.translate(0, -i * h)
            doc.drawContents(painter, QRectF(0, i * h, w, h))
            painter.restore()
    finally:
        painter.end()
    del writer                      # finishes the file
    return notes


def _writer(path, title=""):
    """The PDF writer: Letter where the locale measures in inches, else A4."""
    from qtpy.QtCore import QLocale, QMarginsF
    from qtpy.QtGui import QPageLayout, QPageSize, QPdfWriter
    writer = QPdfWriter(str(path))
    writer.setTitle(title)
    writer.setCreator("KiPI")
    us = QLocale().measurementSystem() == QLocale.ImperialUSSystem
    writer.setPageSize(QPageSize(QPageSize.Letter if us else QPageSize.A4))
    writer.setPageMargins(QMarginsF(*[PAGE_MARGIN_MM] * 4), QPageLayout.Millimeter)
    return writer


def _line(layout, block):
    """The top and bottom of ``block``'s first line where it was laid out, in
    document pixels: a line a page break pushed down counts where it landed."""
    lines = block.layout()
    if lines.lineCount() == 0:
        return None
    line = lines.lineAt(0)
    top = layout.blockBoundingRect(block).top() + line.y()
    return top, top + line.height()


def _stranded_heading(doc, h):
    """The number (in the document's order) of the first heading left at the foot of a
    page: on another page than the first line of what it heads, or for the notes box, its
    whole box. Headings one after another go together, by the first. None if no heading
    is."""
    from qtpy.QtGui import QTextCursor
    layout = doc.documentLayout()
    notes = _notes_table(doc)
    count, run = 0, None                   # run: the first of the headings just passed
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().headingLevel():
            line = _line(layout, block)
            if run is None and line is not None:
                run = (count, int(line[0] // h))
            count += 1
        elif run is not None and block.text().strip():
            table = QTextCursor(block).currentTable()
            if notes is not None and table is not None \
                    and table.firstPosition() == notes.firstPosition():
                end = layout.frameBoundingRect(table).bottom()
            else:
                line = _line(layout, block)
                end = line[1] if line else None
            if end is not None and int((end - 1) // h) != run[1]:
                return run[0]
            run = None
        block = block.next()
    return None


def _heading_count(doc):
    count, block = 0, doc.begin()
    while block.isValid():
        count += bool(block.blockFormat().headingLevel())
        block = block.next()
    return count


def _break_before(html, which):
    """``html`` with a page break before each heading numbered in ``which``."""
    count = iter(range(1 << 30))
    return _HEADING.sub(lambda m: m.group(0) + (_PAGE_BREAK if next(count) in which else ""),
                        html)


def _notes_table(doc):
    """The table holding the notes box's blank image, None without one."""
    from qtpy.QtGui import QTextCursor
    cur = QTextCursor(doc)
    while True:
        cur = doc.find("\ufffc", cur)                  # an image (or other object)
        if cur.isNull():
            return None
        fmt = cur.charFormat()
        if fmt.isImageFormat() and fmt.toImageFormat().name() == NOTES_IMAGE:
            return cur.currentTable()


# ---------------------------------------------------------------------------
# The fillable notes field
# ---------------------------------------------------------------------------

def _pdf_string(text: str) -> bytes:
    return b"(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") \
        .encode("latin-1", "replace") + b")"


def add_text_field(path, page: int, rect: tuple, name: str) -> None:
    """Add a multi-line text field over ``rect`` (PDF points, x0 y0 x1 y1) on ``page`` of
    the PDF at ``path``, as an incremental update. For a file as Qt writes it: a classic
    cross-reference table, a flat page tree and no form yet; anything else raises."""
    path = Path(path)
    data = path.read_bytes()
    starts = {}
    for m in re.finditer(rb"(?m)^(\d+) 0 obj\b", data):
        starts[int(m.group(1))] = m.end()          # the last definition of each object

    def body(n: int) -> bytes:
        if n not in starts:
            raise ValueError(f"object {n} not found")
        return data[starts[n]:data.index(b"endobj", starts[n])].strip()

    tail = data[data.rindex(b"trailer"):]
    size = int(re.search(rb"/Size (\d+)", tail).group(1))
    root = int(re.search(rb"/Root (\d+) 0 R", tail).group(1))
    prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    catalog = body(root)
    if b"/AcroForm" in catalog:
        raise ValueError("the PDF has a form already")
    pages = int(re.search(rb"/Pages (\d+) 0 R", catalog).group(1))
    kids = re.search(rb"/Kids\s*\[(.*?)\]", body(pages), re.S)
    refs = [int(k) for k in re.findall(rb"(\d+) 0 R", kids.group(1))]
    page_obj = refs[page]
    page_dict = body(page_obj)
    if not re.search(rb"/Type\s*/Page\b", page_dict):
        raise ValueError("nested page tree")

    field, font, appearance = size, size + 1, size + 2
    ref = f"{field} 0 R".encode()
    x0, y0, x1, y1 = rect
    objects: List[Tuple[int, bytes]] = []
    annots = re.search(rb"/Annots\s+(\d+) 0 R", page_dict)
    if annots:                                    # Qt: an array object of its own
        arr = body(int(annots.group(1)))
        objects.append((int(annots.group(1)), arr[:arr.rindex(b"]")] + b" " + ref + b"]"))
    elif b"/Annots" in page_dict:
        page_dict = re.sub(rb"/Annots\s*\[", b"/Annots [" + ref + b" ", page_dict, count=1)
        objects.append((page_obj, page_dict))
    else:
        objects.append((page_obj, page_dict[:page_dict.rindex(b">>")] + b"/Annots [" + ref
                        + b"]\n>>"))
    da = f"/Helv {NOTES_FONT_PT} Tf 0 g".encode()
    objects.append((root, catalog[:catalog.rindex(b">>")]
                    + b"/AcroForm << /Fields [" + ref + b"] /NeedAppearances true /DA ("
                    + da + b") /DR << /Font << /Helv " + f"{font} 0 R".encode()
                    + b" >> >> >>\n>>"))
    objects.append((field, b"<< /Type /Annot /Subtype /Widget /FT /Tx /Ff 4096 /F 4 /T "
                    + _pdf_string(name) + b" /TU " + _pdf_string(name) + b" /V () /DA ("
                    + da + b") /P " + f"{page_obj} 0 R".encode()
                    + f" /Rect [{x0:.2f} {y0:.2f} {x1:.2f} {y1:.2f}]".encode()
                    + b" /AP << /N " + f"{appearance} 0 R".encode() + b" >> >>"))
    objects.append((font, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                          b"/Encoding /WinAnsiEncoding >>"))
    objects.append((appearance, f"<< /Type /XObject /Subtype /Form /BBox [0 0 {x1 - x0:.2f} "
                                f"{y1 - y0:.2f}] /Length 0 >>\nstream\n\nendstream".encode()))

    out = bytearray(data if data.endswith(b"\n") else data + b"\n")
    offsets = []
    for n, text in objects:
        offsets.append((n, len(out)))
        out += f"{n} 0 obj\n".encode() + text + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 1\n0000000000 65535 f \n"     # the free list's head, as readers expect
    for n, off in sorted(offsets):
        out += f"{n} 1\n{off:010d} 00000 n \n".encode()
    extra = b"".join(m.group(0) + b" " for m in [re.search(rb"/Info \d+ 0 R", tail),
                                                  re.search(rb"/ID\s*\[[^\]]*\]", tail)] if m)
    out += (f"trailer\n<< /Size {size + 3} /Root {root} 0 R /Prev {prev} ".encode() + extra
            + f">>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(bytes(out))
