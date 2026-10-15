"""The Power tree window (#58): a block diagram of the setup's VRMs, rails, series links,
loads and capacitors, drawn from :func:`kipi.dc.pi.diagram.build_diagram`.

VRM types are told apart by colour, by a tag (SOURCE / LDO / SMPS) and by outline: a
source has a double border, an LDO a plain one and a switcher a dashed one, so they read
apart in print and for colour-blind users too.
"""

from __future__ import annotations

from typing import Dict

from qtpy.QtCore import QPointF, QRectF, Qt
from qtpy.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen
from qtpy.QtWidgets import (
    QDialog, QFileDialog, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem,
    QGraphicsView, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from kipi.dc.pi.diagram import Block, Diagram, build_diagram

FILL = {"rail": "#d9dde3", "link": "#fbe7b5", "load": "#f6c4c0", "cap": "#e2d4f3"}
VRM_FILL = {"source": "#bfe3c0", "ldo": "#ffd29e", "smps": "#b9d7f5"}
VRM_TAG = {"source": "SOURCE", "ldo": "LDO", "smps": "SMPS"}
EDGE = {"rail": "#59616b", "link": "#a07818", "load": "#b04a42", "cap": "#7a5aa8",
        "vrm": "#3d6e9e"}
TEXT = QColor("#1b1f24")
BACKGROUND = "#fafbfc"
PAD_X, PAD_Y, LINE_GAP = 10.0, 6.0, 2.0
COL_GAP, ROW_GAP, MIN_W = 70.0, 16.0, 120.0


class _View(QGraphicsView):
    """Drag to pan, Ctrl + wheel (or plain wheel with no scroll bars) to zoom."""

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.TextAntialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
            e.accept()
        else:
            super().wheelEvent(e)

    def fit(self):
        r = self.scene().itemsBoundingRect().adjusted(-20, -20, 20, 20)
        self.fitInView(r, Qt.KeepAspectRatio)
        if self.transform().m11() > 1.0:           # don't blow small trees up
            self.resetTransform()
            self.centerOn(r.center())


class PowerTreeWindow(QDialog):
    """Non-modal window: call :meth:`show_tree` with the setup (and board) to draw it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KiPI — Power tree")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1100, 650)
        self._setup = None
        self._snapshot = None
        self.diagram: Diagram = Diagram()
        self.scene = QGraphicsScene(self)
        self.view = _View(self.scene, self)
        # A light canvas in dark mode too: the blocks' pastel fills need it, and a saved
        # image looks the same.
        self.view.setBackgroundBrush(QBrush(QColor(BACKGROUND)))

        self.lbl_notes = QLabel(self)
        self.lbl_notes.setWordWrap(True)
        btn_fit = QPushButton("Fit", self)
        btn_fit.setToolTip("Fit the whole tree in the window (Ctrl + wheel zooms)")
        btn_fit.clicked.connect(self.view.fit)
        btn_refresh = QPushButton("Refresh", self)
        btn_refresh.setToolTip("Redraw from the current setup")
        btn_refresh.clicked.connect(self.refresh)
        btn_save = QPushButton("Save image…", self)
        btn_save.setToolTip("Save the diagram as a PNG")
        btn_save.clicked.connect(self.save_image)
        bar = QHBoxLayout()
        bar.addWidget(self.lbl_notes, 1)
        for b in (btn_fit, btn_refresh, btn_save):
            bar.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addLayout(bar)

    # ------------------------------------------------------------------ data
    def show_tree(self, setup, snapshot=None):
        self._setup, self._snapshot = setup, snapshot
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()
        self.view.fit()

    def set_source(self, setup, snapshot=None):
        """Redraw for a changed setup or board, keeping the zoom."""
        self._setup, self._snapshot = setup, snapshot
        self.refresh()

    def refresh(self):
        if self._setup is None:
            return
        self.diagram = build_diagram(self._setup, self._snapshot)
        self._draw()
        notes = list(self.diagram.notes)
        if not self.diagram.blocks:
            notes.insert(0, "Nothing to draw yet: add VRMs, loads, series links or capacitor "
                            "models in the setup.")
        self.lbl_notes.setText("  ".join(notes))

    # --------------------------------------------------------------- drawing
    def _draw(self):
        self.scene.clear()
        font = QFont(self.font())
        bold = QFont(font)
        bold.setBold(True)
        small = QFont(font)
        small.setPointSizeF(max(6.0, font.pointSizeF() * 0.8))
        small.setBold(True)
        fm, fmb, fms = QFontMetricsF(font), QFontMetricsF(bold), QFontMetricsF(small)

        sizes: Dict[str, tuple] = {}
        for b in self.diagram.blocks.values():
            tag = VRM_TAG.get(b.vrm_kind, "") if b.kind == "vrm" else ""
            w = max([fmb.horizontalAdvance(b.title)] + [fm.horizontalAdvance(t) for t in b.lines]
                    + [fms.horizontalAdvance(tag) if tag else 0])
            h = fmb.height() + len(b.lines) * (fm.height() + LINE_GAP)
            if tag:
                h += fms.height() + LINE_GAP
            sizes[b.id] = (max(MIN_W, w + 2 * PAD_X), h + 2 * PAD_Y)

        rects: Dict[str, QRectF] = {}
        x = 0.0
        for col in self.diagram.columns():
            if not col:
                continue
            width = max(sizes[b.id][0] for b in col)
            total = sum(sizes[b.id][1] for b in col) + ROW_GAP * (len(col) - 1)
            y = -total / 2
            for b in col:
                w, h = sizes[b.id]
                rects[b.id] = QRectF(x + (width - w) / 2, y, w, h)
                y += h + ROW_GAP
            x += width + COL_GAP

        for a in self.diagram.arrows:
            # Coloured by the block at the other end from the rail.
            src, dst = self.diagram.blocks[a.src], self.diagram.blocks[a.dst]
            self._arrow(rects[a.src], rects[a.dst], a.dashed,
                        EDGE[dst.kind if src.kind == "rail" else src.kind])
        for b in self.diagram.blocks.values():
            self._block(b, rects[b.id], font, bold, small, fm, fmb, fms)
        self._legend(font, small, fm)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40))

    def _block(self, b: Block, r: QRectF, font, bold, small, fm, fmb, fms):
        fill = VRM_FILL.get(b.vrm_kind) if b.kind == "vrm" else FILL[b.kind]
        path = QPainterPath()
        radius = r.height() / 2 if b.kind == "rail" else 6.0
        path.addRoundedRect(r, min(radius, 14.0), min(radius, 14.0))
        item = QGraphicsPathItem(path)
        item.setBrush(QBrush(QColor(fill)))
        pen = QPen(QColor(EDGE.get(b.kind, "#555")), 1.4)
        if b.kind == "vrm" and b.vrm_kind == "smps":
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(2.0)
        item.setPen(pen)
        item.setToolTip(b.tip or b.title)
        item.setZValue(1)
        self.scene.addItem(item)
        if b.kind == "vrm" and b.vrm_kind == "source":       # double border
            inner = QPainterPath()
            inner.addRoundedRect(r.adjusted(3, 3, -3, -3), 4, 4)
            it = QGraphicsPathItem(inner, item)
            it.setPen(QPen(QColor(EDGE["vrm"]), 1.0))

        y = r.top() + PAD_Y
        if b.kind == "vrm":
            y = self._text(VRM_TAG.get(b.vrm_kind, ""), small, fms, r, y, item,
                           QColor(EDGE["vrm"]))
        y = self._text(b.title, bold, fmb, r, y, item)
        for t in b.lines:
            y = self._text(t, font, fm, r, y, item)

    def _text(self, text, font, fm, r: QRectF, y, parent, color=TEXT):
        t = QGraphicsSimpleTextItem(text, parent)
        t.setFont(font)
        t.setBrush(QBrush(color))
        t.setPos(r.center().x() - fm.horizontalAdvance(text) / 2, y)
        if parent is None:
            t.setZValue(3)
            self.scene.addItem(t)
        return y + fm.height() + LINE_GAP

    def _arrow(self, a: QRectF, b: QRectF, dashed: bool, color: str):
        p0 = QPointF(a.right(), a.center().y())
        p1 = QPointF(b.left(), b.center().y())
        dx = max(20.0, (p1.x() - p0.x()) / 2)
        path = QPainterPath(p0)
        path.cubicTo(QPointF(p0.x() + dx, p0.y()), QPointF(p1.x() - dx, p1.y()), p1)
        pen = QPen(QColor(color), 1.5)
        if dashed:
            pen.setStyle(Qt.DashLine)
        item = self.scene.addPath(path, pen)
        item.setZValue(0)
        if not dashed:
            head = QPainterPath(p1)
            head.lineTo(p1.x() - 9, p1.y() - 4.5)
            head.lineTo(p1.x() - 9, p1.y() + 4.5)
            head.closeSubpath()
            h = self.scene.addPath(head, QPen(QColor(color), 1), QBrush(QColor(color)))
            h.setZValue(2)

    def _legend(self, font, small, fm):
        """Key along the top: what each colour and outline means."""
        items = [("SOURCE", "source"), ("LDO", "ldo"), ("SMPS", "smps"),
                 ("Rail", "rail"), ("Series link", "link"), ("Load", "load"),
                 ("Capacitors", "cap")]
        bounds = self.scene.itemsBoundingRect()
        x, y = bounds.left(), bounds.top() - fm.height() - 30
        for label, key in items:
            w = fm.horizontalAdvance(label) + 16
            r = QRectF(x, y, w, fm.height() + 8)
            vrm = key in VRM_FILL
            self._block(Block(f"legend:{key}", "vrm" if vrm else key, "", vrm_kind=key if vrm
                              else "", tip=""), r, font, font, small, fm, fm, fm)
            if not vrm:
                self._text(label, font, fm, r, r.top() + 4, None)
            x += w + 8

    # ------------------------------------------------------------------ save
    def save_image(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save power tree", "power_tree.png",
                                              "PNG (*.png)")
        if path:
            self.save_png(path)

    def save_png(self, path: str, scale: float = 2.0):
        r = self.scene.itemsBoundingRect().adjusted(-20, -20, 20, 20)
        img = QImage(int(r.width() * scale), int(r.height() * scale), QImage.Format_ARGB32)
        img.fill(QColor(BACKGROUND))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        self.scene.render(p, QRectF(0, 0, img.width(), img.height()), r)
        p.end()
        img.save(path)
