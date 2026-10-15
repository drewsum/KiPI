"""The Board map tab (#67): a top-down view of an AC solve, as in its HTML report. The solver's
mesh of the rail and its ground (one copper layer, or all of them), the board outline, the
probes, and the capacitors in the model (one colour per model) and those left out. It fills
in while the rail solves: the parts first, then the mesh."""

from __future__ import annotations

from typing import List, Optional

import numpy as np
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from kipi.ac.board_map import ACMap
from kipi.ac.gui.plot import COLORS

EMPTY = "Solve a rail in AC mode to see its mesh, capacitors and probes here."
HINT = "Hover a part for its model. Scroll to zoom and drag to pan; double-click to fit."
RAIL_COLOR = "#b98a5c"         # opaque: see-through triangles show seams
GROUND_COLOR = "#4a6178"
ALL_LAYERS = -1
HOVER_PX = 8                       # how near the pointer must be to a part


class BoardMap(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(6, 4), tight_layout=True)
        self.figure.patch.set_facecolor("#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.lbl = QLabel(EMPTY, self)
        self.lbl.setStyleSheet("color: #999;")
        self.lbl.setWordWrap(True)
        self.cmb_layer = QComboBox(self)
        self.cmb_layer.setToolTip("The copper layer whose mesh is shown")
        self.cmb_layer.currentIndexChanged.connect(self._redraw)
        self.chk_mesh = QCheckBox("Mesh edges", self)
        self.chk_mesh.setChecked(True)
        self.chk_mesh.setToolTip("Draw the edges of the solver's triangles; untick to see "
                                 "the copper alone")
        self.chk_mesh.toggled.connect(self._redraw)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.addWidget(QLabel("Layer", self))
        top.addWidget(self.cmb_layer)
        top.addWidget(self.chk_mesh)
        top.addStretch(1)
        lay.addLayout(top)
        lay.addWidget(self.lbl)
        lay.addWidget(self.canvas, 1)
        # Else the controls set the main window's minimum width (it must fit small screens).
        self.setMinimumWidth(120)
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.canvas.mpl_connect("scroll_event", self._scroll)
        self.canvas.mpl_connect("button_press_event", self._press)
        self.canvas.mpl_connect("button_release_event", self._release)
        self.board_map: Optional[ACMap] = None
        self._parts: List[tuple] = []          # (x, y, text) of every part drawn
        self._pan = None                       # (x, y) in data units where a drag started
        self._annot = None
        self._layer_ids: List[int] = []
        self._style()
        self._fill_layers()
        self.canvas.draw_idle()

    def _style(self):
        ax = self.ax
        ax.set_facecolor("#1e1e1e")
        for s in ax.spines.values():
            s.set_color("#888")
        ax.tick_params(colors="#ccc", which="both")
        ax.xaxis.label.set_color("#ccc")
        ax.yaxis.label.set_color("#ccc")
        ax.title.set_color("#ddd")

    # ------------------------------------------------------------------ data
    def clear(self):
        self.board_map = None
        self._parts = []
        self._fill_layers()
        self.lbl.setText(EMPTY)
        self.ax.clear()
        self._annot = None
        self._style()
        self.canvas.draw_idle()

    def set_map(self, board_map: Optional[ACMap]):
        """Show ``board_map`` (None clears the view). Called again as the solve meshes."""
        if board_map is None:
            self.clear()
            return
        same = self.board_map is board_map
        self.board_map = board_map
        self._fill_layers()
        self._redraw(keep_view=same)

    def _fill_layers(self):
        """The layer choice: every layer the mesh is on, keeping the choice made."""
        m = self.board_map
        layers = m.mesh_layers() if m is not None else []
        ids = [ALL_LAYERS] + [i for i, _ in layers]
        if ids == self._layer_ids:
            return
        keep = self.cmb_layer.currentData()
        self.cmb_layer.blockSignals(True)
        self.cmb_layer.clear()
        self.cmb_layer.addItem("All layers", ALL_LAYERS)
        for i, name in layers:
            self.cmb_layer.addItem(name, i)
        k = self.cmb_layer.findData(keep)
        self.cmb_layer.setCurrentIndex(max(k, 0))
        self.cmb_layer.blockSignals(False)
        self.cmb_layer.setEnabled(bool(layers))
        self._layer_ids = ids

    # ------------------------------------------------------------------ drawing
    def _redraw(self, *_args, keep_view: bool = True):
        m = self.board_map
        if m is None:
            return
        from matplotlib.collections import PolyCollection
        ax = self.ax
        view = (ax.get_xlim(), ax.get_ylim()) if keep_view and ax.has_data() else None
        ax.clear()
        self._annot = None
        self._style()
        mesh = m.mesh
        layer = self.cmb_layer.currentData()
        if mesh is not None and len(mesh.tris):
            keep = np.ones(len(mesh.tris), bool) if layer in (None, ALL_LAYERS) \
                else mesh.tri_layer == layer
            edges = self.chk_mesh.isChecked()
            # Ground first, so the rail's copper shows on top of a ground plane.
            for ground, color, label in ((True, GROUND_COLOR, m.ground),
                                         (False, RAIL_COLOR, " + ".join(m.rail_nets))):
                sel = keep & (mesh.tri_ground == ground)
                if not sel.any():
                    continue
                pc = PolyCollection(mesh.points[mesh.tris[sel]], facecolors=color,
                                    edgecolors="#111" if edges else "face",
                                    linewidths=0.2 if edges else 0.3,   # no seams
                                    label=label,
                                    zorder=1 if ground else 2)
                ax.add_collection(pc)
        for r in m.outline:
            ax.plot(r[:, 0], r[:, 1], color="#aaa", lw=1.0, zorder=3)
        self._parts = []
        for k, (name, parts) in enumerate(m.models.items()):
            self._scatter(parts, f"{name} ({len(parts)})", marker="o", s=36,
                          color=COLORS[k % len(COLORS)], edgecolors="#111")
        if m.missing:
            self._scatter(m.missing, f"no model, left out ({len(m.missing)})", marker="x",
                          s=60, color="#f0f0f0", linewidths=1.6)
        if m.probes:
            self._scatter(m.probes, "probes", marker="*", s=180, color="#ff4040",
                          edgecolors="#600")
            for p in m.probes:
                ax.annotate(p.ref, (p.x, p.y), xytext=(0, 9), textcoords="offset points",
                            ha="center", color="#ffb0b0", fontsize=8, zorder=6)
        ax.set_aspect("equal", adjustable="box")
        if view is not None:
            ax.set_xlim(*view[0])
            ax.set_ylim(*view[1])
        else:
            self._fit()
        ax.set_xlabel("x (mm)")
        ax.set_ylabel("y (mm)")
        where = ("all layers" if layer in (None, ALL_LAYERS) else m.layer_name(layer))
        ax.set_title(f"{m.rail}: {' + '.join(m.rail_nets)} against {m.ground}"
                     + (f", mesh on {where}" if mesh is not None else ", meshing…"),
                     fontsize=10)
        handles, _ = ax.get_legend_handles_labels()
        if handles:
            leg = ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8,
                            facecolor="#2a2a2a", edgecolor="#555")
            for t in leg.get_texts():
                t.set_color("#ddd")
        self.lbl.setText(HINT)
        self.canvas.draw_idle()

    def _scatter(self, parts, label, **kw):
        xs = [p.x for p in parts]
        ys = [p.y for p in parts]
        kw.setdefault("linewidths", 0.6)
        self.ax.scatter(xs, ys, label=label, zorder=5, **kw)
        self._parts += [(p.x, p.y, p.text) for p in parts]

    def _fit(self):
        ext = self.board_map.extent() if self.board_map is not None else None
        if ext is None:
            return
        x0, x1, y0, y1 = ext
        pad = 0.03 * max(x1 - x0, y1 - y0, 1.0)
        self.ax.set_xlim(x0 - pad, x1 + pad)
        self.ax.set_ylim(y1 + pad, y0 - pad)       # KiCad's y points down

    # ------------------------------------------------------------------ mouse
    def _hover(self, event):
        if self._pan is not None and event.inaxes is self.ax and event.xdata is not None:
            dx, dy = self._pan[0] - event.xdata, self._pan[1] - event.ydata
            ax = self.ax
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
            ax.set_xlim(x0 + dx, x1 + dx)
            ax.set_ylim(y0 + dy, y1 + dy)
            self.canvas.draw_idle()
            return
        text = self._part_at(event)
        if self._annot is None:
            self._annot = self.ax.annotate(
                "", (0, 0), xytext=(10, 10), textcoords="offset points", fontsize=8,
                color="#eee", zorder=10,
                bbox=dict(boxstyle="round", fc="#2a2a2a", ec="#777"))
            self._annot.set_visible(False)
        if text is None:
            if self._annot.get_visible():
                self._annot.set_visible(False)
                self.canvas.draw_idle()
            return
        x, y, t = text
        self._annot.xy = (x, y)
        self._annot.set_text(t)
        self._annot.set_visible(True)
        self.canvas.draw_idle()

    def _part_at(self, event):
        if not self._parts or event.inaxes is not self.ax or event.x is None:
            return None
        xy = self.ax.transData.transform([(x, y) for x, y, _ in self._parts])
        d = np.hypot(xy[:, 0] - event.x, xy[:, 1] - event.y)
        k = int(np.argmin(d))
        return self._parts[k] if d[k] <= HOVER_PX else None

    def _scroll(self, event):
        if self.board_map is None or event.inaxes is not self.ax or event.xdata is None:
            return
        f = 1 / 1.25 if event.button == "up" else 1.25
        ax = self.ax
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        cx, cy = event.xdata, event.ydata
        ax.set_xlim(cx + (x0 - cx) * f, cx + (x1 - cx) * f)
        ax.set_ylim(cy + (y0 - cy) * f, cy + (y1 - cy) * f)
        self.canvas.draw_idle()

    def _press(self, event):
        if self.board_map is None or event.inaxes is not self.ax or event.xdata is None:
            return
        if event.dblclick:
            self._fit()
            self.canvas.draw_idle()
        elif event.button == 1:
            self._pan = (event.xdata, event.ydata)
            self.canvas.setCursor(Qt.ClosedHandCursor)

    def _release(self, _event):
        if self._pan is not None:
            self._pan = None
            self.canvas.unsetCursor()

    def save_png(self, path: str):
        self.figure.savefig(path, dpi=150, facecolor=self.figure.get_facecolor())
