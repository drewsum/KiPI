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

Files can go in the PDF as well (#133): the DC report's interactive 3D views, as a web page
of their own that PDF viewers list as an attachment. They are added the same way, and
where that fails they are written beside the PDF instead. A link to :func:`file_link` in
the HTML opens the attached file: Qt writes it as a link, which is made to open the file
(Acrobat's JavaScript, run by Acrobat, Foxit and PDF-XChange), with a paperclip beside it
that opens the file in viewers without that (Firefox, Okular).

A heading the page ends under starts the next page instead, with what it heads.

Safe on a worker thread (QTextDocument and QPdfWriter need no window), but the application
must exist: Qt needs it for fonts.
"""

from __future__ import annotations

import base64
import datetime as _dt
import logging
import mimetypes
import re
import zlib
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

PAGE_MARGIN_MM = 12
NOTES_MARKER = "[[kipi-user-notes]]"     # alone in a table cell: that table is the notes box
NOTES_NAME = "User notes"
NOTES_IMAGE = "kipi-notes-box"
NOTES_HEIGHT_PT = 110                    # about eight lines at 10 pt
NOTES_INSET_PT = 2                       # the field inside the box's border
NOTES_FONT_PT = 10
FILE_LINK = "kipi-attachment:"           # an <a href> to an attached file, by its name
CLIP_W_PT, CLIP_H_PT = 9, 15             # the paperclip beside such a link
CLIP_GAP_PT = 3
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
a { color: #1a5fb4; text-decoration: underline; }
"""
# Qt sizes a table to its contents; the full page width spreads a wide one's columns out
# instead of breaking their words.
_TABLE = re.compile(r"<table(?! class='maps')")
_HEADING = re.compile(r"<h[1-6]\b")
_PAGE_BREAK = " style='page-break-before: always'"
_DATA_IMAGE = re.compile(r"""src=(['"])data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)\1""")


class PdfError(RuntimeError):
    """The PDF could not be made here (no Qt application, or Qt failed)."""


def write_pdf(html: str, path, title: str = "",
              attachments: Sequence[Tuple[str, bytes, str]] = ()) -> Path:
    """Lay out ``html`` on pages (Letter where the locale measures in inches, else A4) and
    write it to ``path``, with ``attachments`` ((file name, content, description) each) in
    it. Raises :class:`PdfError` whatever goes wrong with the pages."""
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
    if attachments:
        try:
            attach_files(path, attachments)
        except Exception as e:  # noqa: BLE001 - the files go beside the PDF instead
            log.warning("Could not attach files to the PDF report (%s); saving them beside "
                        "it", e)
            for name, content, _ in attachments:
                try:
                    path.with_name(name).write_bytes(content)
                except OSError as e:
                    log.warning("Could not save %s: %s", name, e)
            try:
                link_files_beside(path)
            except Exception as e:  # noqa: BLE001 - the links stay, opening nothing
                log.warning("The PDF report's links to %s open nothing: %s",
                            ", ".join(a[0] for a in attachments), e)
    return path


def file_link(name: str) -> str:
    """The href of a link to the attached file ``name``."""
    return FILE_LINK + name


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


class _Update:
    """An incremental update to a PDF as Qt writes it (a classic cross-reference table and a
    flat page tree): objects appended, each new or replacing the last definition of its
    number, with a cross-reference section of their own. The bytes before are kept."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = data = self.path.read_bytes()
        self.starts = {}
        for m in re.finditer(rb"(?m)^(\d+) 0 obj\b", data):
            self.starts[int(m.group(1))] = m.end()     # the last definition of each object
        self.tail = data[data.rindex(b"trailer"):]
        self.size = int(re.search(rb"/Size (\d+)", self.tail).group(1))
        self.root = int(re.search(rb"/Root (\d+) 0 R", self.tail).group(1))
        self.prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
        self.objects: List[Tuple[int, bytes]] = []

    def body(self, n: int) -> bytes:
        if n not in self.starts:
            raise ValueError(f"object {n} not found")
        return self.data[self.starts[n]:self.data.index(b"endobj", self.starts[n])].strip()

    def new(self) -> int:
        """The number of a new object."""
        self.size += 1
        return self.size - 1

    def put(self, n: int, text: bytes) -> None:
        self.objects.append((n, text))

    def write(self) -> None:
        data = self.data
        out = bytearray(data if data.endswith(b"\n") else data + b"\n")
        offsets = []
        for n, text in self.objects:
            offsets.append((n, len(out)))
            out += f"{n} 0 obj\n".encode() + text + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 1\n0000000000 65535 f \n"     # the free list's head, as readers expect
        for n, off in sorted(offsets):
            out += f"{n} 1\n{off:010d} 00000 n \n".encode()
        tail = self.tail
        extra = b"".join(m.group(0) + b" " for m in [re.search(rb"/Info \d+ 0 R", tail),
                                                      re.search(rb"/ID\s*\[[^\]]*\]", tail)]
                         if m)
        out += (f"trailer\n<< /Size {self.size} /Root {self.root} 0 R /Prev {self.prev} "
                .encode() + extra + f">>\nstartxref\n{xref}\n%%EOF\n".encode())
        self.path.write_bytes(bytes(out))


def _pages(u: _Update, catalog: bytes) -> List[int]:
    """The page objects, in order, of a flat page tree."""
    pages = int(re.search(rb"/Pages (\d+) 0 R", catalog).group(1))
    kids = re.search(rb"/Kids\s*\[(.*?)\]", u.body(pages), re.S)
    return [int(k) for k in re.findall(rb"(\d+) 0 R", kids.group(1))]


def _annots(u: _Update, page_obj: int) -> List[int]:
    """The annotations on a page."""
    page_dict = u.body(page_obj)
    m = re.search(rb"/Annots\s+(\d+) 0 R", page_dict)
    arr = u.body(int(m.group(1))) if m else (
        re.search(rb"/Annots\s*\[(.*?)\]", page_dict, re.S) or [b"", b""])[1]
    return [int(k) for k in re.findall(rb"(\d+) 0 R", arr)]


def _add_annots(u: _Update, page_obj: int, new: List[int]) -> None:
    """Put the annotations ``new`` on a page."""
    refs = b" ".join(f"{n} 0 R".encode() for n in new)
    page_dict = u.body(page_obj)
    annots = re.search(rb"/Annots\s+(\d+) 0 R", page_dict)
    if annots:                                    # Qt: an array object of its own
        arr = u.body(int(annots.group(1)))
        u.put(int(annots.group(1)), arr[:arr.rindex(b"]")] + b" " + refs + b"]")
    elif b"/Annots" in page_dict:
        u.put(page_obj, re.sub(rb"/Annots\s*\[", b"/Annots [" + refs + b" ", page_dict,
                               count=1))
    else:
        u.put(page_obj, page_dict[:page_dict.rindex(b">>")] + b"/Annots [" + refs + b"]\n>>")


def _file_links(u: _Update, catalog: bytes):
    """(page, link annotation, file name, its rectangle) for each of Qt's links to
    :func:`file_link`."""
    out = []
    for page_obj in _pages(u, catalog):
        for n in _annots(u, page_obj):
            body = u.body(n)
            m = re.search(rb"/URI\s*\(" + re.escape(FILE_LINK.encode()) + rb"([^)]*)\)", body)
            if m and b"/Link" in body:
                rect = re.search(rb"/Rect\s*\[([^\]]*)\]", body).group(1)
                out.append((page_obj, n, m.group(1).decode("latin-1"),
                            [float(x) for x in rect.split()]))
    return out


def _rect(r) -> bytes:
    return ("/Rect [" + " ".join(f"{x:.2f}" for x in r) + "]").encode()


def _paperclip(u: _Update) -> int:
    """The appearance of the paperclip beside a link to a file: one stroked wire."""
    ap = u.new()

    def up(x0, x1, y):                            # a half circle over (x0, y) to (x1, y)
        h = (x1 - x0) * 2 / 3
        return f"{x0} {y + h:.2f} {x1} {y + h:.2f} {x1} {y} c "

    def down(x0, x1, y):
        h = (x0 - x1) * 2 / 3
        return f"{x0} {y - h:.2f} {x1} {y - h:.2f} {x1} {y} c "

    path = ("0.10 0.37 0.71 RG 1.1 w 1 J 1 j 4.5 5.5 m 4.5 11.5 l " + up(4.5, 6.5, 11.5)
            + "6.5 3.5 l " + down(6.5, 2.5, 3.5) + "2.5 12 l " + up(2.5, 8.5, 12)
            + "8.5 6 l S").encode()
    u.put(ap, f"<< /Type /XObject /Subtype /Form /BBox [0 0 {CLIP_W_PT} {CLIP_H_PT}] "
              f"/Length {len(path)} >>\nstream\n".encode() + path + b"\nendstream")
    return ap


def link_files_beside(path) -> None:
    """Make the PDF's links to :func:`file_link` open the files of those names beside it
    (where they could not be attached)."""
    u = _Update(path)
    for _page, n, name, rect in _file_links(u, u.body(u.root)):
        u.put(n, b"<< /Type /Annot /Subtype /Link " + _rect(rect) + b" /Border [0 0 0] "
                 b"/A << /S /Launch /F " + _pdf_string(name) + b" >> >>")
    if u.objects:
        u.write()


def add_text_field(path, page: int, rect: tuple, name: str) -> None:
    """Add a multi-line text field over ``rect`` (PDF points, x0 y0 x1 y1) on ``page`` of
    the PDF at ``path``, as an incremental update. For a file as Qt writes it: a classic
    cross-reference table, a flat page tree and no form yet; anything else raises."""
    u = _Update(path)
    root = u.root
    catalog = u.body(root)
    if b"/AcroForm" in catalog:
        raise ValueError("the PDF has a form already")
    refs = _pages(u, catalog)
    page_obj = refs[page]
    if not re.search(rb"/Type\s*/Page\b", u.body(page_obj)):
        raise ValueError("nested page tree")

    field, font, appearance = u.new(), u.new(), u.new()
    ref = f"{field} 0 R".encode()
    x0, y0, x1, y1 = rect
    _add_annots(u, page_obj, [field])
    da = f"/Helv {NOTES_FONT_PT} Tf 0 g".encode()
    u.put(root, catalog[:catalog.rindex(b">>")]
          + b"/AcroForm << /Fields [" + ref + b"] /NeedAppearances true /DA ("
          + da + b") /DR << /Font << /Helv " + f"{font} 0 R".encode()
          + b" >> >> >>\n>>")
    u.put(field, b"<< /Type /Annot /Subtype /Widget /FT /Tx /Ff 4096 /F 4 /T "
          + _pdf_string(name) + b" /TU " + _pdf_string(name) + b" /V () /DA ("
          + da + b") /P " + f"{page_obj} 0 R".encode()
          + f" /Rect [{x0:.2f} {y0:.2f} {x1:.2f} {y1:.2f}]".encode()
          + b" /AP << /N " + f"{appearance} 0 R".encode() + b" >> >>")
    u.put(font, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                b"/Encoding /WinAnsiEncoding >>")
    u.put(appearance, f"<< /Type /XObject /Subtype /Form /BBox [0 0 {x1 - x0:.2f} "
                      f"{y1 - y0:.2f}] /Length 0 >>\nstream\n\nendstream".encode())
    u.write()


# ---------------------------------------------------------------------------
# Attached files
# ---------------------------------------------------------------------------

def _pdf_text(text: str) -> bytes:
    """A PDF text string any character fits in (UTF-16 with its byte order mark)."""
    return b"<" + ("﻿" + text).encode("utf-16-be").hex().upper().encode() + b">"


def attach_files(path, files: Sequence[Tuple[str, bytes, str]]) -> None:
    """Embed ``files`` ((name, content, description) each) in the PDF at ``path``, as an
    incremental update: the catalog's EmbeddedFiles, which PDF viewers list in their
    attachments panel, opened on that panel (/PageMode /UseAttachments). For a file as Qt
    writes it, with no attachments or page mode yet; anything else raises."""
    u = _Update(path)
    catalog = u.body(u.root)
    if b"/PageMode" in catalog:
        raise ValueError("the PDF has a page mode already")
    when = _dt.datetime.now().strftime("D:%Y%m%d%H%M%S")
    entries = []
    specs = {}
    for name, content, desc in files:
        mime = (mimetypes.guess_type(name)[0] or "application/octet-stream").replace("/", "#2F")
        plain = name.encode("ascii", "replace").decode().replace("?", "_")  # /UF has it all
        packed = zlib.compress(content, 9)
        stream, spec = u.new(), u.new()
        u.put(stream, f"<< /Type /EmbeddedFile /Subtype /{mime} /Filter /FlateDecode "
                      f"/Length {len(packed)} /Params << /Size {len(content)} /ModDate "
                      f"({when}) >> >>\nstream\n".encode() + packed + b"\nendstream")
        u.put(spec, b"<< /Type /Filespec /F " + _pdf_string(plain) + b" /UF " + _pdf_text(name)
              + b" /Desc " + _pdf_text(desc) + f" /EF << /F {stream} 0 R /UF {stream} 0 R >>"
              .encode() + b" /AFRelationship /Supplement >>")
        entries.append((_pdf_string(plain), f" {spec} 0 R".encode()))
        specs[name] = (plain, spec, desc)
    tree = b"<< /Names [" + b" ".join(k + v for k, v in sorted(entries)) + b"] >>"  # by name
    names = re.search(rb"/Names\s+(\d+) 0 R", catalog)
    if names:                                     # Qt: a dictionary object of its own
        n = int(names.group(1))
        old = u.body(n)
        if b"/EmbeddedFiles" in old:
            raise ValueError("the PDF has attachments already")
        u.put(n, old[:old.rindex(b">>")] + b"/EmbeddedFiles " + tree + b"\n>>")
        extra = b""
    elif b"/Names" in catalog:
        raise ValueError("the PDF's names are not an object of their own")
    else:
        n = u.new()
        u.put(n, b"<< /EmbeddedFiles " + tree + b" >>")
        extra = f"/Names {n} 0 R ".encode()
    u.put(u.root, catalog[:catalog.rindex(b">>")] + extra + b"/PageMode /UseAttachments\n>>")
    # Links to the files: opened by Acrobat's JavaScript (saved to a temporary file and
    # opened, as the attachments panel does), with a paperclip after each, which viewers
    # without it open the file from.
    clip, clips = None, {}
    for page_obj, n, name, (x0, y0, x1, y1) in _file_links(u, catalog):
        if name not in specs:
            continue
        plain, spec, desc = specs[name]
        js = f'this.exportDataObject({{cName: "{plain}", nLaunch: 2}});'
        u.put(n, b"<< /Type /Annot /Subtype /Link " + _rect((x0, y0, x1, y1))
                 + b" /Border [0 0 0] /A << /S /JavaScript /JS " + _pdf_string(js) + b" >> >>")
        clip = clip or _paperclip(u)
        mid = (y0 + y1) / 2
        a = u.new()
        u.put(a, b"<< /Type /Annot /Subtype /FileAttachment "
                 + _rect((x1 + CLIP_GAP_PT, mid - CLIP_H_PT / 2, x1 + CLIP_GAP_PT + CLIP_W_PT,
                          mid + CLIP_H_PT / 2))
                 + f" /FS {spec} 0 R /Name /Paperclip /AP << /N {clip} 0 R >> ".encode()
                 + b"/Contents " + _pdf_text(f"{name}: {desc}") + b" >>")
        clips.setdefault(page_obj, []).append(a)
    for page_obj, new in clips.items():
        _add_annots(u, page_obj, new)
    u.write()
