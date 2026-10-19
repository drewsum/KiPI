"""The aggressor's waveforms (#69): switch node, inductor currents, capacitor ripple
currents and rail ripple voltages over a few switching periods, or their harmonics."""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QIcon, QPixmap
from qtpy.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
                            QVBoxLayout, QWidget)

from kipi.ac.gui.plot import COLORS, _eng, _eng_hz
from kipi.gui.tooltip_art import art_tip
from kipi.noise.waveforms import bandwidth_limited

EMPTY = "Solve an aggressor in Coupled Noise mode to see its waveforms here."
MAX_CAP_CURVES = 12             # more capacitors than this: the largest currents only
BW_LIMIT_HZ = 20e6              # the bandwidth limit, as an oscilloscope's

VIEWS = (("sw", "Switch node voltage"), ("il", "Inductor currents"),
         ("cin", "Input capacitor currents"), ("cout", "Output capacitor currents"),
         ("ripple", "Input and output ripple voltage"), ("probes", "Ripple at AC probes"),
         ("victim", "Victim noise"),
         ("states", "Victim held high and low"))

# The victim view's curves (#154): the superposition of the mechanisms is shown, each
# mechanism on its own only when ticked.
VICTIM_CURVES = (("total", "Superposition", True,
                  "The noise at the receiver from every mechanism added up",
                  ("coupling", "cap", "ind", "gnd")),
                 ("capacitive", "Capacitive", False,
                  "The noise coupled through the capacitance to the switch nodes",
                  ("coupling", "cap")),
                 ("inductive", "Inductive", False,
                  "The noise induced by the capacitor loops' changing current",
                  ("coupling", "ind")),
                 ("common", "Common ground", False,
                  "The noise from the ground voltage between the driver and the receiver",
                  ("coupling", "gnd")),
                 ("driver", "Driver end", True,
                  "The superposition at the driver's end of the net as well",
                  ("coupling", "drv")))


class NoisePlot(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(6, 3), tight_layout=True)
        self.figure.patch.set_facecolor("#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.axes = [self.ax]
        self.cmb_view = QComboBox(self)
        for key, text in VIEWS:
            self.cmb_view.addItem(text, key)
        self.cmb_view.setToolTip("The waveforms to plot")
        self.chk_spectrum = QCheckBox("Spectrum", self)
        self.chk_spectrum.setToolTip(art_tip(
            "Plot each waveform's harmonics (peak amplitude at each multiple of the switching "
            "frequency) instead of its time waveform", "spectrum", "harm"))
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
        self.chk_bw = QCheckBox("20 MHz limit", self)
        self.chk_bw.setToolTip(art_tip(
            "Plot every waveform as an oscilloscope with its 20 MHz bandwidth limit on shows "
            "it: through a single-pole low-pass filter, 3 dB down at 20 MHz. The edges' "
            "ringing and fast spikes shrink to what such a measurement would see. The results "
            "table and notes are unchanged.", "bw_limit", "bw"))
        top.addWidget(self.chk_bw)
        top.addStretch(1)
        lay.addLayout(top)
        self.victim_row = QWidget(self)
        grid = QGridLayout(self.victim_row)            # three across, to fit a small screen
        grid.setContentsMargins(0, 0, 0, 0)
        self.chk_curves = {}
        for i, (key, text, on, tip, art) in enumerate(VICTIM_CURVES):
            chk = QCheckBox(text, self.victim_row)
            chk.setChecked(on)
            chk.setToolTip(art_tip(tip, *art))
            chk.toggled.connect(lambda _: self._draw())
            grid.addWidget(chk, i // 3, i % 3)
            self.chk_curves[key] = chk
        grid.setColumnStretch(3, 1)
        self.victim_row.setVisible(False)
        lay.addWidget(self.victim_row)
        lay.addWidget(self.lbl)
        # Beside the plot, a tickbox per curve to show or hide it, in the curve's colour.
        self.curve_box = QWidget()
        self.curve_lay = QVBoxLayout(self.curve_box)
        self.curve_lay.setContentsMargins(2, 2, 2, 2)
        self.curve_lay.setSpacing(2)
        self.curve_lay.addStretch(1)
        self.curve_scroll = QScrollArea(self)
        self.curve_scroll.setWidget(self.curve_box)
        self.curve_scroll.setWidgetResizable(True)
        self.curve_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.curve_scroll.setMaximumWidth(220)
        self.curve_scroll.setVisible(False)
        row = QHBoxLayout()
        row.addWidget(self.canvas, 1)
        row.addWidget(self.curve_scroll)
        lay.addLayout(row, 1)
        self.chk_shown: List[QCheckBox] = []
        self._hidden = {}               # view -> the names of the curves hidden in it
        self._ticks_for = (None, [])    # the view and curves the tickboxes are for
        self.cmb_view.currentIndexChanged.connect(lambda _: self._draw())
        self.chk_spectrum.toggled.connect(lambda _: self._draw())
        self.chk_bw.toggled.connect(lambda _: self._draw())
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.result = None
        self.periods = 2                # switching periods shown (NoiseSettings.periods_shown)
        self.victim = ""                # the victim shown in the victim view
        self._curves: List[Tuple[str, np.ndarray, np.ndarray, str, object]] = []
        self._style()
        self.canvas.draw_idle()

    def _style(self):
        for ax in self.axes:
            ax.set_facecolor("#1e1e1e")
            for s in ax.spines.values():
                s.set_color("#888")
            ax.tick_params(colors="#ccc", which="both")
            ax.xaxis.label.set_color("#ccc")
            ax.yaxis.label.set_color("#ccc")
            ax.title.set_color("#ddd")

    def _layout(self, n: int):
        """``n`` axes stacked, sharing the time axis; cleared and styled."""
        if len(self.axes) != n:
            self.figure.clear()
            self.ax = self.figure.add_subplot(n, 1, 1)
            self.axes = [self.ax] + [self.figure.add_subplot(n, 1, i + 1, sharex=self.ax)
                                     for i in range(1, n)]
        for ax in self.axes:
            ax.clear()
        self._style()

    def clear(self):
        self.result = None
        self._curves = []
        self.curve_scroll.setVisible(False)
        self.lbl.setText(EMPTY)
        self._layout(1)
        self.canvas.draw_idle()

    def set_result(self, result):
        """Plot a :class:`~kipi.noise.result.NoiseResult`'s waveforms."""
        self.result = result
        self._draw()

    def set_victim(self, name: str):
        """Show victim ``name``'s noise."""
        self.victim = name
        if self.cmb_view.currentData() in ("victim", "states"):
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
        if key == "probes":
            return [(f"{n} (AC probe)", sp)
                    for n, sp in getattr(wf, "probe_ripple", {}).items()]
        if key == "states":
            v = self._victim()
            return [] if v is None else [(f"{v.receiver} held {st.name}", st.receiver_v)
                                         for st in v.states if st.receiver_v is not None]
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
        self._curves = []
        key = self.cmb_view.currentData()
        spectrum = self.chk_spectrum.isChecked()
        self.victim_row.setVisible(key == "victim")
        spectra = self._spectra() if self.result is not None else []
        spectra = [(label, self._limited(s)) for label, s in spectra]
        states = key == "states" and not spectrum and len(spectra) > 1
        if states or key == "victim" or not spectra:     # the victim view has its own
            self.curve_scroll.setVisible(False)
        self._layout(len(spectra) if states else 1)
        ax = self.ax
        if self.result is None:
            self.canvas.draw_idle()
            return
        wf = self.result.waveforms
        if states:
            self._draw_states(self._victim(), wf)
            return
        if key == "victim":
            v = self._victim()
            for k, chk in self.chk_curves.items():       # mechanisms this victim lacks
                chk.setEnabled(v is None or k in ("total", "driver") or k in v.receiver_v)
        if not spectra:
            victim = ("No victims were solved: add them in the Victims tab"
                      if self._victim() is None else "Tick a curve to plot")
            if key == "states" and self._victim() is not None:
                victim = "This result has no high and low states: solve it again"
            self.lbl.setText({"victim": victim, "states": victim,
                              "sw": EMPTY}.get(key, "Nothing to plot: no capacitors are set "
                                                    "for this rail"))
            self.canvas.draw_idle()
            return
        unit = spectra[0][1].unit
        # Each curve keeps its colour when others are hidden.
        shown = self._curve_ticks(key, spectra)
        if not shown:
            self.lbl.setText("Tick a curve to plot")
            self.canvas.draw_idle()
            return
        if spectrum:
            # A periodic waveform has nothing between its harmonics: each is drawn as a
            # stem up from the axis's floor, as a spectrum analyser shows it, not joined.
            f = wf.freqs()
            amps = [(i, label, s.amplitude()) for i, (label, s) in shown]
            peak = max(float(np.max(a)) for _, _, a in amps)
            floor = peak * 1e-8 if peak > 0 else 1e-15   # the nulls between lobes go far down
            for i, label, amp in amps:
                color = COLORS[i % len(COLORS)]
                xs = np.repeat(f, 3)
                xs[2::3] = np.nan
                ys = np.column_stack([np.full_like(amp, floor), np.maximum(amp, floor),
                                      np.full_like(amp, np.nan)]).ravel()
                ax.loglog(xs, ys, color=color, lw=0.8, label=label)
                ax.loglog(f, np.maximum(amp, floor), ls="none", marker="o", ms=2.5,
                          color=color)
                self._curves.append((label, f, amp, unit, ax))
            if peak > 0:
                ax.set_ylim(floor, peak * 3)
            ax.set_xlabel("Frequency")
            ax.set_ylabel(f"Amplitude ({unit} peak)")
            ax.xaxis.set_major_formatter(FuncFormatter(_eng_hz))
            ax.grid(True, which="major", color="#555", lw=0.6)
        else:
            periods = max(1, int(self.periods))
            t = wf.time(periods)
            n = wf.samples_per_period()
            for i, (label, s) in shown:
                y = s.samples(n, periods)
                ax.plot(t * 1e6, y, color=COLORS[i % len(COLORS)], lw=1.0, label=label)
                self._curves.append((label, t, y, unit, ax))
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
        v = self._victim() if self.cmb_view.currentData() in ("victim", "states") else None
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
        if (v is not None and key == "victim" and not spectrum
                and v.margin_v > 4 * max(v.peak_v, 1e-15)):
            hint += f" (the ±{v.margin_v * 1e3:.4g} mV noise margin is off the scale)"
        self.lbl.setText(hint + self._bw_note())
        self.canvas.draw_idle()

    def _bw_note(self) -> str:
        return (". 20 MHz bandwidth limit: the title's figures are the full bandwidth's"
                if self.chk_bw.isChecked() else "")

    def _limited(self, sp):
        """``sp`` through the 20 MHz bandwidth limit when it is ticked."""
        if not self.chk_bw.isChecked() or self.result is None:
            return sp
        return bandwidth_limited(sp, self.result.waveforms.fsw_hz, BW_LIMIT_HZ)

    def _curve_ticks(self, key, spectra):
        """A tickbox per curve of ``spectra`` beside the plot (several curves; not the
        victim view, which has its own). Returns [(colour index, (label, Spectrum))] of
        those ticked."""
        names = [_name(label) for label, _ in spectra]
        hidden = self._hidden.setdefault(key, set())
        listed = key != "victim" and len(spectra) > 1
        labels = (key, [label for label, _ in spectra] if listed else [])
        if labels != self._ticks_for:
            self._ticks_for = labels
            for chk in self.chk_shown:
                self.curve_lay.removeWidget(chk)
                chk.deleteLater()
            self.chk_shown = []
            if listed:
                for i, (label, _) in enumerate(spectra):
                    chk = QCheckBox(label, self.curve_box)
                    chk.setProperty("curve", names[i])
                    chk.setChecked(names[i] not in hidden)
                    chk.setToolTip(f"Show {label}")
                    pix = QPixmap(12, 12)
                    pix.fill(QColor(COLORS[i % len(COLORS)]))
                    chk.setIcon(QIcon(pix))
                    chk.toggled.connect(lambda on, n=names[i]: self._show_curve(n, on))
                    self.curve_lay.insertWidget(i, chk)
                    self.chk_shown.append(chk)
        self.curve_scroll.setVisible(listed)
        if not listed:
            return list(enumerate(spectra))
        return [(i, c) for i, c in enumerate(spectra) if names[i] not in hidden]

    def _show_curve(self, name: str, on: bool):
        hidden = self._hidden.setdefault(self.cmb_view.currentData(), set())
        if on:
            hidden.discard(name)
        else:
            hidden.add(name)
        self._draw()

    def _draw_states(self, v, wf):
        """The victim's line held high (top) and low (#156): the driver's level with the
        receiver's noise on it, against the input threshold it must not cross."""
        periods = max(1, int(self.periods))
        t = wf.time(periods)
        n = wf.samples_per_period()
        off = []
        names = {"high": ("VOH", "VIH"), "low": ("VOL", "VIL")}
        for i, (ax, st) in enumerate(zip(self.axes, v.states)):
            noise = self._limited(st.receiver_v).samples(n, periods)
            label = f"{v.receiver} held {st.name}"
            ax.plot(t * 1e6, st.level_v + noise, color=COLORS[i % len(COLORS)], lw=1.0,
                    label=label)
            self._curves.append((label, t, st.level_v + noise, "V", ax))
            out_name, in_name = names.get(st.name, ("level", "threshold"))
            ax.axhline(st.level_v, color="#999", lw=0.8, ls=":",
                       label=f"{out_name} {st.level_v:.3g} V")
            peak = max(float(np.max(np.abs(noise))), 1e-15)
            # As in the noise view: the threshold is drawn where it fits beside the noise.
            if st.margin_v <= 4 * peak:
                ax.axhline(st.threshold_v, color="#ff6060", lw=1.0, ls="--",
                           label=f"{in_name} {st.threshold_v:.3g} V")
            else:
                ax.set_ylim(st.level_v - 1.3 * peak, st.level_v + 1.3 * peak)
                off.append(f"{in_name} {st.threshold_v:.3g} V")
            ax.ticklabel_format(axis="y", useOffset=False)
            ax.set_ylabel("V")
            ax.set_xlim(0, t[-1] * 1e6)
            ax.grid(True, color="#444", lw=0.5)
            ax.set_title(f"{v.name} held {st.name} (driver {st.driver_ohm:.4g} Ω): "
                         f"{st.excursion_v * 1e3:.4g} mV toward {in_name}, "
                         f"{st.ratio * 100:.3g} % of its {st.margin_v * 1e3:.4g} mV margin",
                         fontsize=9)
            leg = ax.legend(loc="best", fontsize=8, facecolor="#2a2a2a", edgecolor="#555")
            for txt in leg.get_texts():
                txt.set_color("#ddd")
        self.axes[-1].set_xlabel("Time (µs)")
        hint = "Hover to read the curves"
        if off:
            hint += f" ({' and '.join(off)} off the scale)"
        self.lbl.setText(hint + self._bw_note())
        self.canvas.draw_idle()

    def _hover(self, event):
        if not self._curves or event.inaxes not in self.axes or event.xdata is None:
            return
        spectrum = self.chk_spectrum.isChecked()
        x = event.xdata if spectrum else event.xdata * 1e-6
        parts = []
        for label, xs, ys, unit, ax in self._curves:
            if ax is not event.inaxes:
                continue
            k = int(np.argmin(np.abs(xs - x)))
            if not parts:
                parts.append(_eng_hz(xs[k]) if spectrum else _eng(xs[k], "s"))
            parts.append(f"{label.split(' (')[0]}: {_eng(float(ys[k]), unit)}")
        self.lbl.setText("   ".join(parts))


def _name(label: str) -> str:
    """A curve's name without the figures after it (a capacitor's rms current), which
    change from solve to solve: what hiding it goes by."""
    return label.split(" (")[0]
