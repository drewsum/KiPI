"""|Z| versus frequency, log-log, for every probe of an AC solve."""

from __future__ import annotations

from typing import Optional

import numpy as np
from qtpy.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

COLORS = ("#4fa3ff", "#ffa040", "#60d060", "#ff6080", "#c080ff", "#40d0d0", "#e0e060")


def _eng_ohms(z: float, _pos=None) -> str:
    if z <= 0:
        return "0"
    for scale, unit in ((1.0, "Ω"), (1e-3, "mΩ"), (1e-6, "µΩ")):
        if z >= scale * 0.999:
            return f"{z / scale:g} {unit}"
    return f"{z * 1e6:g} µΩ"


def _eng_hz(f: float, _pos=None) -> str:
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.999:
            return f"{f / scale:g} {unit}"
    return f"{f:g} Hz"


class ImpedancePlot(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(6, 3), tight_layout=True)
        self.figure.patch.set_facecolor("#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.lbl = QLabel("Solve a rail in AC mode to see its impedance here.", self)
        self.lbl.setStyleSheet("color: #999;")
        self.lbl.setWordWrap(True)          # else its text sets the window's minimum width
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.addWidget(self.lbl, 1)
        lay.addLayout(top)
        lay.addWidget(self.canvas, 1)
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.result = None
        self._style()
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

    def clear(self):
        self.result = None
        self.ax.clear()
        self._style()
        self.canvas.draw_idle()

    def set_result(self, result):
        from matplotlib.ticker import FuncFormatter
        self.result = result
        ax = self.ax
        ax.clear()
        self._style()
        f = result.freqs
        for i, c in enumerate(result.probes):
            color = COLORS[i % len(COLORS)]
            ax.loglog(f, c.mag, color=color, lw=1.6, label=c.name)
            if c.target_ohm:
                f_end = c.target_hz or f[-1]
                ax.loglog([f[0], f_end], [c.target_ohm, c.target_ohm], color=color, lw=1.2,
                          ls="--", alpha=0.8, label=f"{c.name} target")
        ax.set_xlim(f[0], f[-1])
        ax.set_xlabel("Frequency")
        ax.set_ylabel("|Z|")
        ax.xaxis.set_major_formatter(FuncFormatter(_eng_hz))
        ax.yaxis.set_major_formatter(FuncFormatter(_eng_ohms))
        ax.grid(True, which="major", color="#555", lw=0.6)
        ax.grid(True, which="minor", color="#333", lw=0.4)
        ax.set_title(f"{result.rail}: {' + '.join(result.rail_nets)} against {result.ground}",
                     fontsize=10)
        leg = ax.legend(loc="best", fontsize=8, facecolor="#2a2a2a", edgecolor="#555")
        for t in leg.get_texts():
            t.set_color("#ddd")
        self.lbl.setText("Move the pointer over the plot to read values.")
        self.canvas.draw_idle()

    def _hover(self, event):
        if self.result is None or event.inaxes is not self.ax or event.xdata is None:
            return
        f = self.result.freqs
        k = int(np.argmin(np.abs(np.log(f) - np.log(max(event.xdata, 1e-12)))))
        parts = [f"{_eng_hz(f[k])}"]
        for c in self.result.probes:
            parts.append(f"{c.name}: {_eng_ohms(float(c.mag[k]))}"
                         if c.mag[k] >= 1e-6 else f"{c.name}: {c.mag[k]:.3g} Ω")
        self.lbl.setText("   ".join(parts))

    def save_png(self, path: str):
        self.figure.savefig(path, dpi=150, facecolor=self.figure.get_facecolor())
