"""The impedance plot's cursor: a click snaps it to the nearest curve, dragging and the arrow
keys move it along that curve, and its values reach the Info pane."""

import os

import numpy as np
import pytest

pytest.importorskip("qtpy")
pytest.importorskip("matplotlib")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from matplotlib.backend_bases import KeyEvent, MouseEvent  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.ac.gui.plot import ImpedancePlot  # noqa: E402
from kipi.ac.solver.sweep import ACResult, ProbeCurve  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _result():
    f = np.logspace(3, 8, 51)
    w = 2 * np.pi * f
    z_low = 1e-3 + 1j * w * 1e-9              # 1 mΩ + 1 nH
    z_high = 0.1 + 1j * w * 1e-9              # 100 mΩ + 1 nH
    return ACResult(rail="VRM1", rail_nets=["+3V3"], ground="GND", freqs=f,
                    probes=[ProbeCurve("U1", z_low, target_ohm=0.01, target_hz=1e6),
                            ProbeCurve("U2", z_high)], field_freqs=f[::10])


@pytest.fixture
def plot(app):
    p = ImpedancePlot()
    p.resize(800, 500)
    p.set_result(_result())
    p.canvas.draw()                           # the transforms need a drawn figure
    texts = []
    p.cursor_moved.connect(texts.append)
    p.texts = texts
    return p


def _mouse(p, name, f, z, button=1):
    x, y = p.ax.transData.transform((f, z))
    MouseEvent(name, p.canvas, x, y, button=button)._process()


def _key(p, key):
    KeyEvent("key_press_event", p.canvas, key)._process()


def test_click_snaps_to_the_nearest_curve_and_frequency(plot):
    r = plot.result
    _mouse(plot, "button_press_event", 1.1e4, 0.05)     # nearer U2 (0.1 Ω) than U1 (1 mΩ)
    assert plot.cursor == (1, 10)                       # 1e4 Hz is sweep point 10
    _mouse(plot, "button_release_event", 1.1e4, 0.05)
    _mouse(plot, "button_press_event", 1.1e4, 2e-3)
    assert plot.cursor == (0, 10)
    v, h, dot = plot._cursor_artists
    assert v.get_xdata()[0] == pytest.approx(r.freqs[10])
    assert h.get_ydata()[0] == pytest.approx(r.probes[0].mag[10])
    assert "U1" in plot.texts[-1] and "10 kHz" in plot.texts[-1]


def test_dragging_tracks_the_same_curve(plot):
    _mouse(plot, "button_press_event", 1e4, 2e-3)
    _mouse(plot, "motion_notify_event", 1e6, 0.1)       # pointer over U2's level
    assert plot.cursor == (0, 30)                       # still on U1
    _mouse(plot, "button_release_event", 1e6, 0.1)
    _mouse(plot, "motion_notify_event", 1e7, 0.1)       # hovering does not move it
    assert plot.cursor == (0, 30)


def test_keys_step_switch_and_remove(plot):
    plot.set_cursor(0, 20)
    _key(plot, "right")
    assert plot.cursor == (0, 21)
    _key(plot, "shift+left")
    assert plot.cursor == (0, 11)
    _key(plot, "up")
    assert plot.cursor == (1, 11)
    plot.set_cursor(1, 1000)
    assert plot.cursor == (1, 50)                       # clamped to the sweep
    _key(plot, "escape")
    assert plot.cursor is None and plot._cursor_artists is None
    assert plot.texts[-1] == ""


def test_cursor_text_has_the_impedance_and_target(plot):
    plot.set_cursor(0, 10)                              # 10 kHz: 1 mΩ + j63 µΩ
    text = plot.texts[-1]
    assert "Phase" in text and "as L" in text and "1 nH" in text
    assert "PASS" in text and "U2" in text              # the other probe is listed
    plot.set_cursor(0, 50)                              # 100 MHz: above the target band
    assert "PASS" not in plot.texts[-1] and "not above" in plot.texts[-1]


def test_new_result_removes_the_cursor(plot):
    plot.set_cursor(0, 5)
    plot.set_result(_result())
    assert plot.cursor is None and plot._cursor_artists is None


def test_cursor_reaches_the_info_pane(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    w.plot.set_result(_result())
    w.plot.set_cursor(1, 0)
    assert "U2" in w.info.toPlainText() and "1 kHz" in w.info.toPlainText()
    w.close()
