"""A cross-section of the stackup beside the Stackup window's table (#104): each layer a
band as thick as the layer, redrawn as thicknesses are typed.

The drawing is to scale, except that a layer too thin to see at the window's size (a
copper foil on a thick board) is drawn :data:`MIN_BAND_PX` tall; the label gives its real
thickness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from qtpy.QtCore import QPointF, QRectF, QSize, Qt, Signal
from qtpy.QtGui import QColor, QPainter, QPen
from qtpy.QtWidgets import QSizePolicy, QToolTip, QWidget

MIN_BAND_PX = 3.0
MARGIN = 10
BAR_W = 64
LEADER_W = 22               # from the bar to its label
COPPER = QColor(200, 120, 60)


@dataclass
class Band:
    """One layer: its label, thickness, fill colour and whether it is copper."""
    label: str
    mm: float
    # Fill colour, 0..1: a copper layer's, or a solder mask's or silkscreen's. None: from
    # the material.
    color: Optional[Tuple[float, float, float]] = None
    copper: bool = False
    material: str = ""


def band_heights(mm: Sequence[float], total_px: float,
                 min_px: float = MIN_BAND_PX) -> List[float]:
    """Pixel heights for layers ``mm`` thick in ``total_px``: to scale, except that none is
    under ``min_px``; the others share what is left in proportion."""
    n = len(mm)
    if n == 0:
        return []
    if total_px <= n * min_px:
        return [total_px / n] * n
    small: set = set()
    scale = 0.0
    while True:
        big = sum(t for i, t in enumerate(mm) if i not in small)
        scale = (total_px - len(small) * min_px) / big if big > 0 else 0.0
        new = {i for i, t in enumerate(mm) if i not in small and t * scale < min_px}
        if not new:
            break
        small |= new
    return [min_px if i in small else t * scale for i, t in enumerate(mm)]


def spread_labels(wanted: Sequence[float], gap: float, top: float,
                  bottom: float) -> List[float]:
    """Label centres as near ``wanted`` (top to bottom) as they can be while ``gap``
    apart, between ``top`` and ``bottom`` where they fit."""
    ys = list(wanted)
    for i in range(1, len(ys)):
        ys[i] = max(ys[i], ys[i - 1] + gap)
    if ys:
        ys[-1] = min(ys[-1], bottom)
    for i in range(len(ys) - 2, -1, -1):
        ys[i] = min(ys[i], ys[i + 1] - gap)
    if ys and ys[0] < top:                      # too many to fit: keep the top in view
        shift = top - ys[0]
        ys = [y + shift for y in ys]
    return ys


def dielectric_color(material: str) -> QColor:
    """A fill that suggests the material: polyimide amber, PTFE and ceramics pale, a metal
    core grey, the rest FR-4 green."""
    m = material.lower()
    if any(k in m for k in ("polyimide", "kapton")):
        return QColor(190, 140, 40)
    if any(k in m for k in ("ceramic", "alumina", "nitride")):
        return QColor(225, 220, 205)
    if "alumin" in m:                                   # an aluminum metal core
        return QColor(165, 170, 180)
    if any(k in m for k in ("ptfe", "teflon", "rogers", "ro4", "ro3", "duroid")):
        return QColor(205, 205, 195)
    return QColor(110, 140, 80)


def _xy(event) -> QPointF:
    return event.position() if hasattr(event, "position") else QPointF(event.pos())


def _global(event):
    return (event.globalPosition().toPoint() if hasattr(event, "globalPosition")
            else event.globalPos())


class StackupView(QWidget):
    """Draws the layers top to bottom. ``layer_clicked(index)`` tells which band was
    clicked, in the order the bands were given."""

    layer_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bands: List[Band] = []
        self._rects: List[QRectF] = []
        self._hover: Optional[int] = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setMinimumHeight(160)
        self.setToolTip("The stackup to scale, top to bottom. A layer too thin to see is drawn "
                        f"{MIN_BAND_PX:g} px tall; its label gives the real thickness. Click "
                        "a layer to edit it in the table.")

    def sizeHint(self) -> QSize:
        return QSize(MARGIN * 2 + BAR_W + LEADER_W + 170, 400)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def set_bands(self, bands: Sequence[Band]):
        self._bands = list(bands)
        self._hover = None
        self.update()

    def bands(self) -> List[Band]:
        return list(self._bands)

    # ------------------------------------------------------------------ layout
    def _layout(self) -> List[QRectF]:
        top, bottom = MARGIN, self.height() - MARGIN
        heights = band_heights([b.mm for b in self._bands], bottom - top)
        rects, y = [], float(top)
        for h in heights:
            rects.append(QRectF(MARGIN, y, BAR_W, h))
            y += h
        return rects

    def _labelled(self) -> List[int]:
        """The bands that get a label: all of them when they fit, else the copper."""
        room = self.height() - 2 * MARGIN
        gap = self.fontMetrics().height()
        if len(self._bands) * gap <= room:
            return list(range(len(self._bands)))
        return [i for i, b in enumerate(self._bands) if b.copper]

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        p.setPen(Qt.NoPen)
        p.setBrush(pal.window().color().darker(112))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        self._rects = self._layout()
        if not self._bands:
            p.setPen(pal.text().color())
            p.drawText(self.rect(), Qt.AlignCenter, "No stackup")
            p.end()
            return
        edge = pal.window().color().darker(160)
        for b, r in zip(self._bands, self._rects):
            fill = (QColor.fromRgbF(*b.color) if b.color
                    else COPPER if b.copper else dielectric_color(b.material))
            p.setBrush(fill)
            p.setPen(QPen(edge, 0.5) if r.height() > 4 else Qt.NoPen)
            p.drawRect(r)
        if self._hover is not None:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(pal.highlight().color(), 2))
            p.drawRect(self._rects[self._hover].adjusted(-1, -1, 1, 1))

        shown = self._labelled()
        fm = self.fontMetrics()
        x_bar = MARGIN + BAR_W
        x_text = x_bar + LEADER_W
        centres = [self._rects[i].center().y() for i in shown]
        ys = spread_labels(centres, fm.height(), MARGIN + fm.height() / 2,
                           self.height() - MARGIN - fm.height() / 2)
        text = pal.text().color()
        lead = QColor(text)
        lead.setAlpha(110)
        for i, y0, y in zip(shown, centres, ys):
            p.setPen(QPen(lead, 1))
            p.drawPolyline([QPointF(x_bar + 2, y0), QPointF(x_bar + LEADER_W / 2, y0),
                            QPointF(x_text - 3, y)])
            p.setPen(pal.highlight().color() if i == self._hover else text)
            p.drawText(QRectF(x_text, y - fm.height() / 2, self.width() - x_text - MARGIN,
                              fm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(self._bands[i].label, Qt.ElideRight,
                                     int(self.width() - x_text - MARGIN)))
        p.end()

    # ------------------------------------------------------------------ mouse
    def band_at(self, y: float) -> Optional[int]:
        rects = self._rects or self._layout()
        for i, r in enumerate(rects):
            if r.top() <= y < r.bottom():
                return i
        return None

    def mouseMoveEvent(self, event):
        i = self.band_at(_xy(event).y())
        if i != self._hover:
            self._hover = i
            self.update()
        if i is not None:
            QToolTip.showText(_global(event), self._bands[i].label, self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        i = self.band_at(_xy(event).y())
        if i is not None and event.button() == Qt.LeftButton:
            self.layer_clicked.emit(i)
        super().mousePressEvent(event)
