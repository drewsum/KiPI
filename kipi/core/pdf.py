"""Print a report's HTML to a PDF file (#119), with Qt's rich text engine.

PySide6 is already one of KiPI's packages, so this needs nothing more; a browser engine
(QtWebEngine) would be far bigger and is not in PySide6-Essentials. Qt's rich text takes
tables, headings, colors and images, but no scripts and only a little CSS, so the reports
make a static version of themselves for it: matplotlib images in place of the Plotly plots,
plain headings in place of the collapsible sections, and the stylesheet below.

Safe on a worker thread (QTextDocument and QPdfWriter need no window), but the application
must exist: Qt needs it for fonts.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

PAGE_MARGIN_MM = 12
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
"""
# Qt sizes a table to its contents; the full page width spreads a wide one's columns out
# instead of breaking their words.
_TABLE = re.compile(r"<table(?! class='maps')")
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
        _print(html, path, title)
    except Exception as e:  # noqa: BLE001 - any Qt failure: the caller falls back
        raise PdfError(f"{type(e).__name__}: {e}") from e
    if not path.is_file() or path.stat().st_size == 0:
        raise PdfError(f"Qt could not write {path}")
    return path


def _print(html: str, path: Path, title: str) -> None:
    from qtpy.QtCore import QLocale, QMarginsF, QUrl
    from qtpy.QtGui import QImage, QPageLayout, QPageSize, QPdfWriter, QTextDocument

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
    doc.setHtml(html)
    writer = QPdfWriter(str(path))
    writer.setTitle(title)
    writer.setCreator("KiPI")
    us = QLocale().measurementSystem() == QLocale.ImperialUSSystem
    writer.setPageSize(QPageSize(QPageSize.Letter if us else QPageSize.A4))
    writer.setPageMargins(QMarginsF(*[PAGE_MARGIN_MM] * 4), QPageLayout.Millimeter)
    doc.print_(writer)
    del writer                      # finishes the file
