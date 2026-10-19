"""The aggressor's waveforms (#69): switch node, inductor currents, capacitor ripple
currents and rail ripple voltages over a few switching periods, or their harmonics."""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
from qtpy.QtWidgets import QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from kipi.ac.gui.plot import COLORS, _eng, _eng_hz

EMPTY = "Solve an aggressor in Coupled Noise mode to see its waveforms here."
MAX_CAP_CURVES = 12             # more capacitors than this: the largest currents only

VIEWS = (("sw", "Switch node voltage"), ("il", "Inductor currents"),
         ("cin", "Input capacitor currents"), ("cout", "Output capacitor currents"),
         ("ripple", "Input and output ripple voltage"), ("victim", "Victim noise"))

# The victim view's curves (#154): the superposition of the mechanisms is shown, each
# mechanism on its own only when ticked.
VICTIM_CURVES = (("total", "Superposition", True,
                  "The noise at the receiver from every mechanism added up"),
                 ("capacitive", "Capacitive", False,
                  "The noise coupled through the capacitance to the switch nodes"),
                 ("inductive", "Inductive", False,
                  "The noise induced by the capacitor loops' changing current"),
                 ("common", "Common ground", False,
                  "The noise from the ground voltage between the driver and the receiver"),
                 ("driver", "Driver end", True,
                  "The superposition at the driver's end of the net as well"))


class NoisePlot(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(6, 3), tight_layout=True)
        self.figure.patch.set_facecolor("#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.cmb_view = QComboBox(self)
        for key, text in VIEWS:
            self.cmb_view.addItem(text, key)
        self.cmb_view.setToolTip("The waveforms to plot")
        self.chk_spectrum = QCheckBox("Spectrum", self)
        self.chk_spectrum.setToolTip("Plot each waveform's harmonics (peak amplitude at each "
                                     "multiple of the switching frequency) instead of its "
                                     "time waveform")
        self.lbl = QLabel(EMPTY, self)
        self.lbl.setStyleSheet("color: #999;")
        self.lbl.setWordWrap(True)
        # The window must fit a small screen: the view list may shrink, and the readout has
        # a line of its own.
        self.cmb_view.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_view.setMinimumContentsLength(14)
        self.setMinimumWidth(120)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.addWidget(self.cmb_view, 1)
        top.addWidget(self.chk_spectrum)
        top.addStretch(1)
        lay.addLayout(top)
        self.victim_row = QWidget(self)
        grid = QGridLayout(self.victim_row)            # three across, to fit a small screen
        grid.setContentsMargins(0, 0, 0, 0)
        self.chk_curves = {}
        for i, (key, text, on, tip) in enumerate(VICTIM_CURVES):
            chk = QCheckBox(text, self.victim_row)
            chk.setChecked(on)
            chk.setToolTip(tip)
            chk.toggled.connect(lambda _: self._draw())
            grid.addWidget(chk, i // 3, i % 3)
            self.chk_curves[key] = chk
        grid.setColumnStretch(3, 1)
        self.victim_row.setVisible(False)
        lay.addWidget(self.victim_row)
        lay.addWidget(self.lbl)
        lay.addWidget(self.canvas, 1)
        self.cmb_view.currentIndexChanged.connect(lambda _: self._draw())
        self.chk_spectrum.toggled.connect(lambda _: self._draw())
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.result = None
        self.periods = 2                # switching periods shown (NoiseSettings.periods_shown)
        self.victim = ""                # the victim shown in the victim view
        self._curves: List[Tuple[str, np.ndarray, np.ndarray, str]] = []
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
        self._curves = []
        self.lbl.setText(EMPTY)
        self.ax.clear()
        self._style()
        self.canvas.draw_idle()

    def set_result(self, result):
        """Plot a :class:`~kipi.noise.result.NoiseResult`'s waveforms."""
        self.result = result
        self._draw()

    def set_victim(self, name: str):
        """Show victim ``name``'s noise."""
        self.victim = name
        if self.cmb_view.currentData() == "victim":
            self._draw()
        else:
            self.set_view("victim")

    def save_png(self, path: str):
        self.figure.savefig(path, dpi=150, facecolor=self.figure.get_facecolor())

    def set_view(self, key: str):
        i = self.cmb_view.findData(key)
        if i >= 0:
            self.cmb_view.setCurrentIndex(i)

    # ------------------------------------------------------------------ drawing
    def _spectra(self):
        """[(label, Spectrum)] of the chosen view."""
        wf = self.result.waveforms
        key = self.cmb_view.currentData()
        if key == "sw":
            return [(s.switch_node.name, s.switch_node) for s in wf.phases]
        if key == "il":
            return [(s.inductor.name, s.inductor) for s in wf.phases]
        if key in ("cin", "cout"):
            caps = wf.input_caps if key == "cin" else wf.output_caps
            ranked = sorted(caps.items(), key=lambda kv: -kv[1].ac_rms())[:MAX_CAP_CURVES]
            return [(f"{ref} ({kv.ac_rms():.3g} A rms)", kv) for ref, kv in ranked]
        if key == "victim":
            v = self._victim()
            if v is None:
                return []
            shown = {k for k, chk in self.chk_curves.items() if chk.isChecked()}
            out = [(f"{v.receiver} {k}", sp) for k, sp in v.receiver_v.items()
                   if k != "total" and k in shown]
            if "total" in v.receiver_v and "total" in shown:
                out.insert(0, (f"{v.receiver} (receiver)", v.receiver_v["total"]))
            if "total" in v.driver_v and "driver" in shown:
                out.append((f"{v.driver} (driver)", v.driver_v["total"]))
            return out
        out = []
        if wf.input_ripple is not None:
            out.append(("Vin ripple", wf.input_ripple))
        if wf.output_ripple is not None:
            out.append(("Vout ripple", wf.output_ripple))
        return out

    def _victim(self):
        victims = getattr(self.result, "victims", []) if self.result is not None else []
        return next((v for v in victims if v.name == self.victim),
                    victims[0] if victims else None)

    def _draw(self):
        from matplotlib.ticker import FuncFormatter
        ax = self.ax
        ax.clear()
        self._style()
        self._curves = []
        key = self.cmb_view.currentData()
        self.victim_row.setVisible(key == "victim")
        if self.result is None:
            self.canvas.draw_idle()
            return
        wf = self.result.waveforms
        spectra = self._spectra()
        spectrum = self.chk_spectrum.isChecked()
        if key == "victim":
            v = self._victim()
            for k, chk in self.chk_curves.items():       # mechanisms this victim lacks
                chk.setEnabled(v is None or k in ("total", "driver") or k in v.receiver_v)
        if not spectra:
            victim = ("No victims were solved: add them in the Victims tab"
                      if self._victim() is None else "Tick a curve to plot")
            self.lbl.setText({"victim": victim,
                              "sw": EMPTY}.get(key, "Nothing to plot: no capacitors are set "
                                                    "for this rail"))
            self.canvas.draw_idle()
            return
        unit = spectra[0][1].unit
        if spectrum:
            f = wf.freqs()
            for i, (label, s) in enumerate(spectra):
                amp = s.amplitude()
                ax.loglog(f, np.maximum(amp, 1e-15), color=COLORS[i % len(COLORS)], lw=1.0,
                          label=label)
                self._curves.append((label, f, amp, unit))
            peak = max(float(np.max(c[2])) for c in self._curves)
            if peak > 0:                      # the nulls between harmonics' lobes go far down
                ax.set_ylim(peak * 1e-8, peak * 3)
            ax.set_xlabel("Frequency")
            ax.set_ylabel(f"Amplitude ({unit} peak)")
            ax.xaxis.set_major_formatter(FuncFormatter(_eng_hz))
            ax.grid(True, which="major", color="#555", lw=0.6)
        else:
            periods = max(1, int(self.periods))
            t = wf.time(periods)
            n = wf.samples_per_period()
            for i, (label, s) in enumerate(spectra):
                y = s.samples(n, periods)
                ax.plot(t * 1e6, y, color=COLORS[i % len(COLORS)], lw=1.0, label=label)
                self._curves.append((label, t, y, unit))
            ax.set_xlim(0, t[-1] * 1e6)
            v = self._victim() if self.cmb_view.currentData() == "victim" else None
            # The margin lines are drawn where they fit; far above small noise they would
            # flatten it, so the axis then follows the noise.
            if v is not None and v.margin_v <= 4 * max(v.peak_v, 1e-15):
                for sign in (1, -1):
                    ax.axhline(sign * v.margin_v, color="#ff6060", lw=1.0, ls="--",
                               label="noise margin" if sign > 0 else None)
            elif v is not None:
                ax.set_ylim(-1.3 * v.peak_v, 1.3 * v.peak_v)
            ax.set_xlabel("Time (µs)")
            ax.set_ylabel(unit)
            ax.grid(True, color="#444", lw=0.5)
        op = self.result.operating
        v = self._victim() if self.cmb_view.currentData() == "victim" else None
        if v is not None:
            ax.set_title(f"{v.name}: {v.peak_v * 1e3:.4g} mV peak, {v.ratio * 100:.3g} % of "
                         f"its margin, from {self.result.aggressor}", fontsize=10)
        else:
            ax.set_title(f"{self.result.aggressor} ({op.topology}, {op.vin:g} V → {op.vout:g} V, "
                         f"{op.iout:.3g} A, {wf.fsw_hz / 1e3:g} kHz, D = {op.duty:.3f})",
                         fontsize=10)
        leg = ax.legend(loc="best", fontsize=8, facecolor="#2a2a2a", edgecolor="#555")
        for txt in leg.get_texts():
            txt.set_color("#ddd")
        hint = "Hover to read the curves"
        if v is not None and not spectrum and v.margin_v > 4 * max(v.peak_v, 1e-15):
            hint += f" (the ±{v.margin_v * 1e3:.4g} mV noise margin is off the scale)"
        self.lbl.setText(hint)
        self.canvas.draw_idle()

    def _hover(self, event):
        if not self._curves or event.inaxes is not self.ax or event.xdata is None:
            return
        spectrum = self.chk_spectrum.isChecked()
        x = event.xdata if spectrum else event.xdata * 1e-6
        parts = []
        for label, xs, ys, unit in self._curves:
            k = int(np.argmin(np.abs(xs - x)))
            if not parts:
                parts.append(_eng_hz(xs[k]) if spectrum else _eng(xs[k], "s"))
            parts.append(f"{label.split(' (')[0]}: {_eng(float(ys[k]), unit)}")
        self.lbl.setText("   ".join(parts))
