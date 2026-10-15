"""|Z| versus frequency, log-log, for every probe of an AC solve, with a cursor that tracks
one probe's curve: plot-wide crosshair lines, its values in the Info pane."""

from __future__ import annotations

import html
import math
from typing import Optional, Tuple

import numpy as np
from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

COLORS = ("#4fa3ff", "#ffa040", "#60d060", "#ff6080", "#c080ff", "#40d0d0", "#e0e060")
CURSOR_COLOR = "#e8e8e8"
EMPTY = "Solve a rail in AC mode to see its impedance here."
HINT = ("Click a curve to place the cursor. Drag to move it along the curve, ←/→ to step "
        "(Shift: ×10), ↑/↓ to change curve, Esc or right-click to remove it.")


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


def _eng(x: float, unit: str, digits: int = 4) -> str:
    """``x`` with an SI prefix: 0.00123, "H" -> "1.23 mH"."""
    if x == 0 or not math.isfinite(x):
        return f"{x:g} {unit}"
    e = max(-15, min(9, 3 * math.floor(math.log10(abs(x)) / 3)))
    prefix = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[e]
    return f"{x / 10 ** e:.{digits}g} {prefix}{unit}"


class ImpedancePlot(QWidget):
    # HTML describing the cursor point, for the Info pane; "" when the cursor is removed.
    cursor_moved = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(6, 3), tight_layout=True)
        self.figure.patch.set_facecolor("#1e1e1e")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setFocusPolicy(Qt.ClickFocus)     # for the cursor's arrow keys
        self.ax = self.figure.add_subplot(111)
        self.lbl = QLabel(EMPTY, self)
        self.lbl.setStyleSheet("color: #999;")
        self.lbl.setWordWrap(True)          # else its text sets the window's minimum width
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.addWidget(self.lbl, 1)
        lay.addLayout(top)
        lay.addWidget(self.canvas, 1)
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.canvas.mpl_connect("button_press_event", self._press)
        self.canvas.mpl_connect("button_release_event", self._release)
        self.canvas.mpl_connect("key_press_event", self._key)
        self.result = None
        self.cursor: Optional[Tuple[int, int]] = None      # (probe index, frequency index)
        self._cursor_artists = None
        # Tracked here: motion events carry the held buttons only in newer matplotlib.
        self._dragging = False
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
        self.lbl.setText(EMPTY)
        self.ax.clear()
        self._style()
        self._drop_cursor()
        self.canvas.draw_idle()

    def set_result(self, result):
        from matplotlib.ticker import FuncFormatter
        self.result = result
        ax = self.ax
        ax.clear()
        self._style()
        self._drop_cursor()
        f = result.freqs
        for i, c in enumerate(result.probes):
            color = COLORS[i % len(COLORS)]
            ax.loglog(f, c.mag, color=color, lw=1.6, label=c.name)
            t = c.target_at(f)
            if t is not None:                 # NaN where no target applies: not drawn
                ax.loglog(f, t, color=color, lw=1.2, ls="--", alpha=0.8,
                          label=f"{c.name} target")
        bw = getattr(result, "bandwidth_hz", None)
        if bw and f[0] < bw < f[-1]:
            ax.axvline(bw, color="#888", lw=1.0, ls=":",
                       label=f"VRM bandwidth {_eng_hz(bw)}")
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
        self.lbl.setText(HINT)
        self.canvas.draw_idle()

    def _nearest_freq(self, x: float) -> int:
        f = self.result.freqs
        return int(np.argmin(np.abs(np.log(f) - np.log(max(x, 1e-12)))))

    def _hover(self, event):
        if self.result is None or event.inaxes is not self.ax or event.xdata is None:
            return
        if self._dragging and self.cursor is not None:   # follow the curve
            self.set_cursor(self.cursor[0], self._nearest_freq(event.xdata))
            return
        k = self._nearest_freq(event.xdata)
        parts = [f"{_eng_hz(self.result.freqs[k])}"]
        for c in self.result.probes:
            parts.append(f"{c.name}: {_eng_ohms(float(c.mag[k]))}"
                         if c.mag[k] >= 1e-6 else f"{c.name}: {c.mag[k]:.3g} Ω")
        self.lbl.setText("   ".join(parts))

    # ------------------------------------------------------------------ cursor
    def _press(self, event):
        if self.result is None or not self.result.probes or event.inaxes is not self.ax:
            return
        if event.button == 3:
            self.remove_cursor()
            return
        if event.button != 1 or event.xdata is None or event.ydata is None:
            return
        k = self._nearest_freq(event.xdata)
        # The curve nearest the click on the log |Z| axis.
        y = math.log(max(event.ydata, 1e-30))
        i = int(np.argmin([abs(math.log(max(float(c.mag[k]), 1e-30)) - y)
                           for c in self.result.probes]))
        self.set_cursor(i, k)
        self._dragging = True

    def _release(self, _event):
        self._dragging = False

    def _key(self, event):
        if self.cursor is None or not event.key:
            return
        i, k = self.cursor
        key = event.key
        step = 10 if key.startswith("shift+") else 1
        key = key.split("+")[-1]
        n = len(self.result.probes)
        if key == "left":
            self.set_cursor(i, k - step)
        elif key == "right":
            self.set_cursor(i, k + step)
        elif key == "up":
            self.set_cursor((i + 1) % n, k)
        elif key == "down":
            self.set_cursor((i - 1) % n, k)
        elif key == "escape":
            self.remove_cursor()

    def set_cursor(self, probe: int, k: int):
        """Put the cursor on probe ``probe``'s curve at sweep point ``k`` (clamped)."""
        if self.result is None or not self.result.probes:
            return
        f = self.result.freqs
        k = max(0, min(len(f) - 1, int(k)))
        probe = max(0, min(len(self.result.probes) - 1, int(probe)))
        self.cursor = (probe, k)
        x, y = float(f[k]), float(self.result.probes[probe].mag[k])
        color = COLORS[probe % len(COLORS)]
        if self._cursor_artists is None:
            kw = dict(color=CURSOR_COLOR, lw=0.9, ls=":", label="_cursor")
            self._cursor_artists = (
                self.ax.axvline(x, **kw), self.ax.axhline(y, **kw),
                self.ax.plot([x], [y], "o", ms=7, mfc="none", mew=1.6, label="_cursor")[0])
        v, h, dot = self._cursor_artists
        v.set_xdata([x, x])
        h.set_ydata([y, y])
        dot.set_data([x], [y])
        dot.set_color(color)
        self.canvas.draw_idle()
        self.cursor_moved.emit(self.cursor_html())

    def remove_cursor(self):
        had = self.cursor is not None
        self._drop_cursor()
        self.canvas.draw_idle()
        if had:
            self.cursor_moved.emit("")

    def _drop_cursor(self):
        if self._cursor_artists is not None:
            for a in self._cursor_artists:
                if a.axes is not None:
                    a.remove()
        self._cursor_artists = None
        self.cursor = None
        self._dragging = False

    def cursor_html(self) -> str:
        """The cursor point: frequency, the tracked probe's impedance, its target margin,
        and every other probe's |Z| at the same frequency."""
        if self.cursor is None or self.result is None:
            return ""
        i, k = self.cursor
        r = self.result
        fk = float(r.freqs[k])
        c = r.probes[i]
        z = complex(c.z[k])
        mag = abs(z)
        w = 2 * math.pi * fk
        color = COLORS[i % len(COLORS)]
        rows = [("Frequency", _eng_hz(fk)), ("|Z|", _eng(mag, "Ω")),
                ("Phase", f"{math.degrees(math.atan2(z.imag, z.real)):.1f}°"),
                ("R", _eng(z.real, "Ω")), ("X", _eng(z.imag, "Ω"))]
        if z.imag > 0:
            rows.append(("as L", _eng(z.imag / w, "H")))
        elif z.imag < 0:
            rows.append(("as C", _eng(-1 / (w * z.imag), "F")))
        t = c.target_at(r.freqs)
        if t is not None:
            tk = float(t[k])
            if not math.isfinite(tk):
                if c.above_hz and fk < c.above_hz:
                    why = f"only above the VRM bandwidth, {_eng_hz(c.above_hz)}"
                else:
                    why = f"not above {_eng_hz(c.target_hz)}"
                rows.append(("Target", f"none here ({why})"))
            else:
                ok = mag <= tk
                margin = 20 * math.log10(tk / mag) if mag > 0 else math.inf
                rows.append(("Target", f"{_eng(tk, 'Ω')}: "
                                       f'<span style="color:{"#60d060" if ok else "#ff6060"}">'
                                       f'{"PASS" if ok else "FAIL"}</span> ({margin:+.1f} dB)'))
        body = "".join(f"<tr><td>{a}&nbsp;&nbsp;</td><td>{b}</td></tr>" for a, b in rows)
        out = [f'<b>Cursor: <span style="color:{color}">{html.escape(c.name)}</span></b>'
               f"<br><small>point {k + 1} of {len(r.freqs)}</small>",
               f"<table>{body}</table>"]
        others = [(j, p) for j, p in enumerate(r.probes) if j != i]
        if others:
            body = "".join(f'<tr><td style="color:{COLORS[j % len(COLORS)]}">'
                           f"{html.escape(p.name)}&nbsp;&nbsp;</td>"
                           f"<td>{_eng(float(p.mag[k]), 'Ω')}</td></tr>" for j, p in others)
            out.append(f"<br><b>Other probes at {_eng_hz(fk)}</b><table>{body}</table>")
        return "".join(out)

    def save_png(self, path: str):
        self.figure.savefig(path, dpi=150, facecolor=self.figure.get_facecolor())
