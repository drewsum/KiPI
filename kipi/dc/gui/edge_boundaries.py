"""The Edge boundaries dialog (#221): a top-down view of the board where the user marks
stretches of its edge to hold at a fixed temperature, as a conduction-cooled heatframe or
wedge lock clamping the board would. Clicking an edge takes its straight side; dragging
along it takes the stretch dragged over. Each boundary has a temperature and the width of
the strip it holds."""

from __future__ import annotations

import copy
from typing import List, Optional

import shapely
from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from kipi.core.model.board import BoardSnapshot
from kipi.dc.thermal.edges import edge_rings, forward, nearest_ring, side_at, span
from kipi.dc.thermal.model import EdgeBoundary, board_geometry
from kipi.gui.tooltip_art import art_tip
from kipi.gui.unit_spin import UnitSpinBox

COLUMNS = ["Name", "Temperature", "Strip width", "Length"]
HINT = ("Click the board edge to hold that side at a fixed temperature, or drag along it "
        "to take part of it. Without any boundary the board loses heat by convection "
        "alone.")
PICK_PX = 12                       # how near the edge a press must be
OUTLINE = "#aaa"
EDGE = "#4aa3ff"
CHOSEN = "#ffb347"


def _spin(lo, hi, value, decimals, suffix, step) -> UnitSpinBox:
    s = UnitSpinBox()
    s.setShownDecimals(decimals)
    s.setRange(lo, hi)
    s.setValue(value)
    s.setSuffix(suffix)
    s.setSingleStep(step)
    s.setKeyboardTracking(False)
    return s


class EdgeBoundaryDialog(QDialog):
    """Edit a copy of ``edges``; :meth:`boundaries` gives the result."""

    def __init__(self, snap: BoardSnapshot, edges: List[EdgeBoundary], ambient_c: float,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Board edge boundaries")
        self.resize(900, 620)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.snap, self.ambient_c = snap, ambient_c
        self.edges = [copy.deepcopy(e) for e in edges]
        self.board = board_geometry(snap)
        self.rings = edge_rings(snap)
        self._press = None                 # (ring, mm along it, xy) while dragging
        self._preview: Optional[list] = None
        self.figure = Figure(figsize=(6, 4), tight_layout=True)
        self.figure.patch.set_facecolor("#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.setToolTip(art_tip(HINT, "edge_boundary", "edge"))

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemSelectionChanged.connect(self._redraw)
        self.table.itemChanged.connect(self._renamed)
        self.btn_whole = QPushButton("Whole edge", self)
        self.btn_whole.setToolTip("Hold the board's whole outer edge at one temperature")
        self.btn_whole.clicked.connect(self._add_whole)
        self.btn_delete = QPushButton("Delete", self)
        self.btn_delete.setToolTip("Remove the selected boundary")
        self.btn_delete.clicked.connect(self._delete)
        self.btn_clear = QPushButton("Delete all", self)
        self.btn_clear.setToolTip("Remove every boundary: the board loses heat by "
                                  "convection alone")
        self.btn_clear.clicked.connect(self._clear)
        hint = QLabel(HINT, self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")

        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.canvas, 3)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        for b in (self.btn_whole, self.btn_delete, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row.addWidget(self.buttons)
        lay.addLayout(row)
        self._fill()

    def boundaries(self) -> List[EdgeBoundary]:
        return [copy.deepcopy(e) for e in self.edges]

    # ------------------------------------------------------------------ table
    def _fill(self, select: Optional[int] = None):
        t = self.table
        t.blockSignals(True)
        t.setRowCount(len(self.edges))
        for r, e in enumerate(self.edges):
            t.setItem(r, 0, QTableWidgetItem(e.name))
            temp = _spin(-55, 300, e.temp_c, 1, " °C", 1)
            temp.setToolTip("The temperature this stretch of edge is held at: the heatframe's "
                            "or cold wall's")
            temp.valueChanged.connect(lambda v, e=e: setattr(e, "temp_c", v))
            t.setCellWidget(r, 1, temp)
            width = _spin(0, 100, e.width_mm, 2, " mm", 0.5)
            width.setSpecialValueText("edge cells")
            width.setToolTip(art_tip(
                "How far in from the edge the board is held: the width the frame or wedge "
                "lock clamps. 'Edge cells': the thermal grid's cells along the edge alone.",
                "edge_boundary", "strip"))
            width.valueChanged.connect(lambda v, e=e: self._set_width(e, v))
            t.setCellWidget(r, 2, width)
            length = QTableWidgetItem(f"{e.length_mm():.1f} mm")
            length.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            t.setItem(r, 3, length)
        t.blockSignals(False)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        if select is not None and 0 <= select < len(self.edges):
            t.selectRow(select)
        self.btn_delete.setEnabled(bool(self.edges))
        self.btn_clear.setEnabled(bool(self.edges))
        self._redraw()

    def _renamed(self, item: QTableWidgetItem):
        if item.column() == 0 and item.row() < len(self.edges):
            self.edges[item.row()].name = item.text().strip() or f"Edge {item.row() + 1}"

    def _set_width(self, e: EdgeBoundary, v: float):
        e.width_mm = v
        self._redraw()

    def _selected(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def add(self, points: List[List[float]]):
        """A new boundary along ``points``, at ambient, selected."""
        names = {e.name for e in self.edges}
        k = len(self.edges) + 1
        while f"Edge {k}" in names:
            k += 1
        self.edges.append(EdgeBoundary(f"Edge {k}", self.ambient_c, 0.0, points))
        self._fill(len(self.edges) - 1)

    def _add_whole(self):
        if self.rings:
            self.add(forward(self.rings[0], 0.0, self.rings[0].length))

    def _delete(self):
        k = self._selected()
        if 0 <= k < len(self.edges):
            del self.edges[k]
            self._fill(min(k, len(self.edges) - 1))

    def _clear(self):
        self.edges = []
        self._fill()

    # ------------------------------------------------------------------ mouse
    def _tolerance_mm(self) -> float:
        (x0, _), (x1, _) = self.ax.transData.inverted().transform([(0, 0), (PICK_PX, 0)])
        return abs(x1 - x0)

    def _on_press(self, event):
        if event.inaxes is not self.ax or event.button != 1 or event.xdata is None:
            return
        k, dist = nearest_ring(self.rings, (event.xdata, event.ydata))
        if k < 0 or dist > self._tolerance_mm():
            return
        s = self.rings[k].project(shapely.Point(event.xdata, event.ydata))
        self._press = (k, s)
        self._preview = None

    def _on_motion(self, event):
        if self._press is None or event.xdata is None:
            return
        k, s0 = self._press
        s1 = self.rings[k].project(shapely.Point(event.xdata, event.ydata))
        if abs(s1 - s0) > self._tolerance_mm():
            self._preview = span(self.rings[k], s0, s1)
            self._redraw()

    def _on_release(self, event):
        if self._press is None:
            return
        k, s0 = self._press
        points = self._preview or side_at(self.rings[k], s0)
        self._press, self._preview = None, None
        self.add(points)

    # ------------------------------------------------------------------ drawing
    def _redraw(self, *_):
        ax = self.ax
        ax.clear()
        ax.set_facecolor("#1e1e1e")
        for s in ax.spines.values():
            s.set_color("#888")
        ax.tick_params(colors="#ccc", which="both")
        for r in self.rings:
            x, y = r.xy
            ax.plot(x, y, color=OUTLINE, lw=1.0, zorder=2)
        chosen = self._selected()
        for k, e in enumerate(self.edges):
            line = e.geometry()
            if line is None:
                continue
            color = CHOSEN if k == chosen else EDGE
            if e.width_mm > 0:
                strip = line.buffer(e.width_mm).intersection(self.board)
                for p in getattr(strip, "geoms", [strip]):
                    if p.geom_type == "Polygon" and not p.is_empty:
                        ax.fill(*p.exterior.xy, color=color, alpha=0.3, lw=0, zorder=1)
            if line.geom_type == "LineString":
                ax.plot(*line.xy, color=color, lw=4, solid_capstyle="round", zorder=3)
                mid = line.interpolate(0.5, normalized=True)
                ax.annotate(f"{e.name}\n{e.temp_c:g} °C", (mid.x, mid.y), xytext=(0, 10),
                            textcoords="offset points", ha="center", fontsize=8,
                            color=color, zorder=5,
                            bbox=dict(boxstyle="round", fc="#2a2a2a", ec=color, alpha=0.9))
        if self._preview:
            xs, ys = zip(*self._preview)
            ax.plot(xs, ys, color=CHOSEN, lw=3, ls="--", zorder=4)
        ax.set_aspect("equal", adjustable="box")
        minx, miny, maxx, maxy = self.board.bounds
        pad = 0.05 * max(maxx - minx, maxy - miny, 1.0)
        ax.set_xlim(minx - pad, maxx + pad)
        ax.set_ylim(maxy + pad, miny - pad)         # KiCad's y points down
        ax.set_xlabel("x (mm)", color="#ccc")
        ax.set_ylabel("y (mm)", color="#ccc")
        self.canvas.draw_idle()
