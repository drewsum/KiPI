"""Graphics in tooltips (#116): a tooltip about a visual engineering idea (a capacitor's
ESL, a VRM's load line) shows a small drawing of it under its text.

The drawings live in one place, ``kipi/gui/graphics/``, as SVG templates. Colours in a
template are tokens filled from the window's palette when the tooltip is made, so a drawing
matches the GUI, on a background a shade darker than the window:

* ``{bg}`` the background, ``{fg}`` text and lines, ``{muted}`` axes and guides,
  ``{grid}`` faint fills, ``{accent}`` the palette's highlight (or, where that is a grey,
  as in some dark themes, an orange that stands out), ``{warn}`` a warning tint.
* Any other token names a part of the drawing a tooltip can point at: drawn in
  ``{accent}`` when the tooltip names it in ``highlight``, else in ``{fg}``. One drawing
  serves several fields that way (the capacitor model's C, ESL and ESR).

Each drawing is rendered once per palette to a PNG in KiPI's temporary folder, at twice
its size for sharp high-DPI screens, and the tooltip refers to it. Where it cannot be
rendered (no SVG support in this Qt), the tooltip is its text alone.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from qtpy.QtCore import QByteArray, Qt
from qtpy.QtGui import QColor, QGuiApplication, QImage, QPainter, QPalette

log = logging.getLogger("kipi")

GRAPHICS = Path(__file__).with_name("graphics")
SCALE = 2                                    # rendered at 2x, shown at 1x
_TOKEN = re.compile(r"\{(\w+)\}")
_SIZE = re.compile(r'<svg[^>]*\bwidth="(\d+)"[^>]*\bheight="(\d+)"')
_rendered: Dict[Tuple[str, Tuple[str, ...], str], Optional[Tuple[Path, int, int]]] = {}


def names() -> list:
    """Every drawing in the graphics folder."""
    return sorted(p.stem for p in GRAPHICS.glob("*.svg"))


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def accent_color(pal: QPalette) -> QColor:
    """The palette's highlight where it is a colour that stands out from the background;
    else an orange, lighter on a dark background."""
    hl, bg = pal.highlight().color(), pal.window().color()
    if hl.hsvSaturation() >= 90 and abs(hl.lightness() - bg.lightness()) >= 40:
        return hl
    return QColor(240, 160, 80) if bg.lightness() < 128 else QColor(200, 100, 30)


def palette_colors(pal: Optional[QPalette] = None) -> Dict[str, str]:
    """The base tokens, from ``pal`` (default: the application's palette)."""
    if pal is None:
        pal = QGuiApplication.palette()
    bg = pal.window().color().darker(112)
    fg = pal.windowText().color()
    return {"bg": bg.name(), "fg": fg.name(), "muted": _mix(bg, fg, 0.55).name(),
            "grid": _mix(bg, fg, 0.15).name(), "accent": accent_color(pal).name(),
            "warn": _mix(bg, QColor(220, 90, 60), 0.35).name()}


def fill(svg: str, colors: Dict[str, str], highlight: Iterable[str] = ()) -> str:
    """``svg`` with its colour tokens replaced."""
    lit = set(highlight)

    def sub(m):
        key = m.group(1)
        if key in colors:
            return colors[key]
        return colors["accent"] if key in lit else colors["fg"]
    return _TOKEN.sub(sub, svg)


def _render_png(svg: str, path: Path) -> Optional[Tuple[int, int]]:
    """Render ``svg`` to ``path``; its size in pixels at 1x, or None if Qt cannot."""
    m = _SIZE.search(svg)
    if m is None:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    img = QImage(w * SCALE, h * SCALE, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    try:
        from qtpy.QtSvg import QSvgRenderer
    except ImportError:                       # no QtSvg module: the image plugin may do
        loaded = QImage.fromData(QByteArray(svg.encode("utf-8")), "SVG")
        if loaded.isNull():
            return None
        img = loaded.scaled(w * SCALE, h * SCALE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    else:
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not renderer.isValid():
            return None
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p)
        p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    return (w, h) if img.save(str(path), "PNG") else None


def image(name: str, highlight: Iterable[str] = (),
          pal: Optional[QPalette] = None) -> Optional[Tuple[Path, int, int]]:
    """The drawing ``name`` as a PNG in this palette: (path, width, height), or None."""
    colors = palette_colors(pal)
    key = (name, tuple(sorted(highlight)), repr(sorted(colors.items())))
    if key in _rendered:
        return _rendered[key]
    out = None
    try:
        svg = fill((GRAPHICS / f"{name}.svg").read_text(encoding="utf-8"), colors, highlight)
        digest = hashlib.sha1(svg.encode("utf-8")).hexdigest()[:12]
        path = Path(tempfile.gettempdir()) / "kipi" / "tooltip-art" / f"{name}-{digest}.png"
        m = _SIZE.search(svg)
        if path.is_file() and m is not None:
            out = (path, int(m.group(1)), int(m.group(2)))
        else:
            size = _render_png(svg, path)
            out = (path, *size) if size else None
    except OSError as e:
        log.debug("Tooltip drawing %s not shown: %s", name, e)
    _rendered[key] = out
    return out


def art_tip(text: str, name: str, *highlight: str) -> str:
    """Tooltip ``text`` with the drawing ``name`` under it, the parts named in
    ``highlight`` picked out; ``text`` alone when the drawing cannot be shown."""
    shown = image(name, highlight)
    if shown is None:
        return text
    path, w, h = shown
    body = html.escape(text, quote=False).replace("\n", "<br>")
    return (f'<table cellspacing="0" cellpadding="0" width="{max(w, 280)}"><tr><td>{body}'
            f'</td></tr><tr><td style="padding-top: 6px;"><img src="{path.as_uri()}" '
            f'width="{w}" height="{h}"></td></tr></table>')
