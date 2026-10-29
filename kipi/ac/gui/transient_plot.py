"""The estimated transient response of an AC solve (#166): each probe's load step, up and
back down, and the rail's deviation from its DC value while it steps."""

from __future__ import annotations

import html
import math
from typing import Optional

import numpy as np
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from kipi.ac.gui.plot import COLORS, _eng
from kipi.ac.transient import bandwidth_limited, transients
from kipi.gui.tooltip_art import art_tip

EMPTY = ("Give a probe a load step and slew rate (Probes tab) and its VRM a control loop "
         "bandwidth, then solve the rail to see the estimated transient response here.")
NOTE = ("An estimate, each probe's load step on its own: the open-loop PDN impedance with "
        "the VRM's loop closed at its control loop bandwidth (a typical compensation, not "
        "the converter's own) and regulating to its load line, if it has one. Undershoot "
        "and overshoot are measured from the voltage the rail settles to (dotted, with a "
        "load line). Dashed: ± target |Z| × ΔI about it, the AC tolerance a flat target "
        "stands for. Tick Zoom to edge, or zoom in with the toolbar, to see fast edges.")
BW_LIMIT_HZ = 20e6              # the bandwidth limit, as an oscilloscope's


def time_scale(t_end: float):
    """(factor, unit) for a time axis running to ``t_end`` seconds."""
    for scale, unit in ((1.0, "s"), (1e-3, "ms"), (1e-6, "µs")):
        if t_end >= 2 * scale:
            return 1.0 / scale, unit
    return 1e9, "ns"


def edge_text(tr) -> str:
    """What the load's step-up edge does to the rail (#200): the ESL spike, L·di/dt, and
    the rail's largest dip over the edge; or why no spike stands out."""
    rise = _eng(tr.t_rise_s, "s", 3)
    if tr.spike_v is None:
        f_edge = 1.0 / (math.pi * tr.t_rise_s)
        return (f"its {rise} edge is slow enough that the PDN is still capacitive at its "
                f"{_eng(f_edge, 'Hz', 3)}: no ESL spike stands out from the droop "
                f"(−{tr.edge_v * 1e3:.3g} mV over the edge)")
    slew = tr.step_a / tr.t_rise_s / 1e6
    # Rounded first, so 0.99997 nH reads 1 nH, not 1e+03 pH.
    l_h = float(f"{tr.edge_l_h:.3g}")
    return (f"ESL spike L·di/dt = {_eng(l_h, 'H', 3)} × {slew:.3g} A/µs = "
            f"{tr.spike_v * 1e3:.3g} mV over its {rise} edge; −{tr.edge_v * 1e3:.3g} mV "
            "there with the droop")


class TransientPlot(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(6, 3), tight_layout=True)
        self.figure.patch.set_facecolor("#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setFocusPolicy(Qt.ClickFocus)
        self.ax_i, self.ax_v = self.figure.subplots(
            2, 1, sharex=True, gridspec_kw={"height_ratios": [1, 3]})
        self.lbl = QLabel(EMPTY, self)
        self.lbl.setStyleSheet("color: #999;")
        self.lbl.setWordWrap(True)
        self.lbl.setTextFormat(Qt.RichText)
        self.chk_bw = QCheckBox("20 MHz limit", self)
        self.chk_bw.setToolTip(art_tip(
            "Plot the rail as an oscilloscope with its 20 MHz bandwidth limit on shows it: "
            "through a single-pole low-pass filter, 3 dB down at 20 MHz. Fast spikes and "
            "ringing shrink to what such a measurement would see, and so do the undershoot "
            "and overshoot in the legend. The results table and report are unchanged.",
            "bw_limit", "bw"))
        self.chk_bw.toggled.connect(lambda _on: self._draw())
        self.chk_edge = QCheckBox("Zoom to edge", self)
        self.chk_edge.setToolTip(art_tip(
            "Show only the load's step-up edge (#200). While the current ramps, the PDN's "
            "inductance (the capacitors' ESL, their mounting, the planes) drops L·di/dt: "
            "the ESL spike, as long as the edge. Beside the loop's recovery it is too short "
            "to see unzoomed. Its size is listed above the plot.",
            "esl_spike", "spike"))
        self.chk_edge.toggled.connect(lambda _on: self._draw())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.addWidget(self.chk_bw)
        top.addWidget(self.chk_edge)
        top.addStretch(1)
        # Zoom and pan: a load step's fast edges last nanoseconds, the loop's recovery
        # tens of microseconds. Without matplotlib's toolbar the plot works unzoomed.
        try:
            from matplotlib.backends.backend_qtagg import NavigationToolbar2QT
            self.toolbar = NavigationToolbar2QT(self.canvas, self, coordinates=False)
            top.addWidget(self.toolbar)
        except Exception:                 # noqa: BLE001 — an optional convenience
            self.toolbar = None
        lay.addLayout(top)
        lay.addWidget(self.lbl)
        lay.addWidget(self.canvas, 1)
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.result = None
        self.transients = {}
        self._full = {}             # the transients at full bandwidth
        self._scale = (1e6, "µs")
        self._style()
        self.canvas.draw_idle()

    def _style(self):
        for ax in (self.ax_i, self.ax_v):
            ax.set_facecolor("#1e1e1e")
            for s in ax.spines.values():
                s.set_color("#888")
            ax.tick_params(colors="#ccc", which="both")
            ax.xaxis.label.set_color("#ccc")
            ax.yaxis.label.set_color("#ccc")
            ax.title.set_color("#ddd")

    def clear(self):
        self.result = None
        self.transients = self._full = {}
        self._wipe()
        self.lbl.setText(EMPTY)
        self.canvas.draw_idle()

    def _wipe(self):
        for ax in (self.ax_i, self.ax_v):
            ax.clear()
        self._style()

    def set_result(self, result):
        self.clear()
        self.result = result
        self._full = transients(result)
        self._draw()

    def _draw(self):
        """Plot the result's transients, through the bandwidth limit when it is ticked."""
        result = self.result
        if result is None:
            return
        self._wipe()
        self.transients = dict(self._full)
        if self.chk_bw.isChecked():
            self.transients = {n: bandwidth_limited(tr, BW_LIMIT_HZ)
                               for n, tr in self._full.items()}
        if not self.transients:
            self.lbl.setText(EMPTY)
            self.canvas.draw_idle()
            return
        names = [c.name for c in result.probes]
        t_end = max(tr.t[-1] for tr in self.transients.values())
        span = (0.0, t_end)
        if self.chk_edge.isChecked():
            wins = [tr.edge_window() for tr in self.transients.values()]
            span = (max(min(w[0] for w in wins), 0.0), max(w[1] for w in wins))
        k, unit = self._scale = time_scale(span[1] - span[0])
        for name, tr in self.transients.items():
            color = COLORS[names.index(name) % len(COLORS)]
            self.ax_i.plot(tr.t * k, tr.i, color=color, lw=1.4)
            self.ax_v.plot(tr.t * k, tr.v * 1e3, color=color, lw=1.6,
                           label=f"{name}: −{tr.undershoot_v * 1e3:.3g} / "
                                 f"+{tr.overshoot_v * 1e3:.3g} mV")
            for x in (tr.t_undershoot_s, tr.t_overshoot_s):
                self.ax_v.plot([x * k], [np.interp(x, tr.t, tr.v) * 1e3], "o", ms=5,
                               mfc="none", mew=1.4, color=color)
            settled = tr.settled
            if tr.load_line_ohm:
                self.ax_v.plot(tr.t * k, settled * 1e3, color=color, lw=1.0, ls=":")
            allowed = next(c for c in result.probes if c.name == name).allowed_v()
            if allowed:
                for y in (allowed, -allowed):
                    self.ax_v.plot(tr.t * k, (settled + y) * 1e3, color=color, lw=1.0,
                                   ls="--", alpha=0.8)
        self.ax_v.axhline(0, color="#666", lw=0.8)
        self.ax_i.set_ylabel("Load step (A)")
        self.ax_v.set_ylabel("Rail deviation (mV)")
        self.ax_v.set_xlabel(f"Time ({unit})")
        self.ax_v.set_xlim(span[0] * k, span[1] * k)
        if self.chk_edge.isChecked():
            # Fit each axis to what the zoom shows.
            for ax, values in ((self.ax_i, lambda tr: tr.i), (self.ax_v, lambda tr: tr.v * 1e3)):
                seen = [values(tr)[(tr.t >= span[0]) & (tr.t <= span[1])]
                        for tr in self.transients.values()]
                seen = np.concatenate([x for x in seen if x.size] or [np.zeros(1)])
                lo, hi = min(float(seen.min()), 0.0), max(float(seen.max()), 0.0)
                pad = 0.08 * (hi - lo) or 1.0
                ax.set_ylim(lo - pad, hi + pad)
        for ax in (self.ax_i, self.ax_v):
            ax.grid(True, color="#444", lw=0.6)
        bw = ", 20 MHz bandwidth limit" if self.chk_bw.isChecked() else ""
        self.ax_i.set_title(f"{result.rail}: estimated transient response{bw}", fontsize=10)
        leg = self.ax_v.legend(loc="best", fontsize=8, facecolor="#2a2a2a", edgecolor="#555")
        for t in leg.get_texts():
            t.set_color("#ddd")
        self.lbl.setText(NOTE + self._notes())
        self.canvas.draw_idle()

    def _notes(self) -> str:
        edges = [f"{html.escape(name)}: {html.escape(edge_text(tr))}"
                 for name, tr in self.transients.items()]
        notes = [f"{name}: {n}" for name, tr in self.transients.items() for n in tr.notes]
        return ("".join(f"<br>{e}" for e in edges)
                + "".join(f"<br>⚠ {html.escape(n)}" for n in notes))

    def _hover(self, event):
        if not self.transients or event.inaxes not in (self.ax_i, self.ax_v) \
                or event.xdata is None:
            return
        k, unit = self._scale
        t = event.xdata / k
        parts = [f"{_eng(t, 's')}"]
        for name, tr in self.transients.items():
            j = int(np.clip(np.searchsorted(tr.t, t), 0, len(tr.t) - 1))
            v = float(tr.v[j])
            parts.append(f"{html.escape(name)}: {_eng(float(tr.i[j]), 'A', 3)}, "
                         f"{v * 1e3:+.3g} mV")
        self.lbl.setText("   ".join(parts))

    def save_png(self, path: str):
        self.figure.savefig(path, dpi=150, facecolor=self.figure.get_facecolor())
