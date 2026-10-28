"""A probe's custom target mask: frequency/impedance points joined by straight lines on the
log-log plot, in place of the flat target, through the setup file, the PASS/FAIL check, the
plot and the probe editor."""

import math
import os

import numpy as np
import pytest

from kipi.ac.model import Probe
from kipi.ac.solver.sweep import ProbeCurve
from kipi.ac.target import mask_at, target_at
from kipi.dc.pi.model import PinRef, PISetup, Vrm

# 10 mΩ to 100 kHz, rising to 20 mΩ at 1 MHz, 20 mΩ above
MASK = [(1e6, 0.02), (1e5, 0.01)]


def flat(points):
    """(Hz, Ω) pairs as one flat list: pytest.approx takes no nested pairs (pytest 9.2+)."""
    return [v for p in points for v in p]


def test_mask_is_log_log_linear_and_flat_outside():
    t = mask_at(MASK, [1e3, 1e5, math.sqrt(1e5 * 1e6), 1e6, 1e9])
    assert t == pytest.approx([0.01, 0.01, math.sqrt(0.01 * 0.02), 0.02, 0.02])


def test_flat_target_applies_up_to_its_frequency():
    f = np.array([1e5, 1e6, 1e7])
    t = target_at(f, 0.01, 1e6)
    assert t[:2] == pytest.approx([0.01, 0.01]) and np.isnan(t[2])
    assert target_at(f, 0.01, 1e3) == pytest.approx([0.01] * 3)  # sweep starts above it
    assert target_at(f) is None
    assert target_at(f, 0.01, 1e6, MASK)[2] == pytest.approx(0.02)  # the mask wins


def test_mask_pass_fail_and_worst_point():
    f = np.logspace(4, 8, 41)
    z = np.full(f.shape, 0.015 + 0j)                    # 15 mΩ everywhere
    c = ProbeCurve("U1", z, target_ohm=0.05, target_points=MASK)
    assert c.passed(f) is False                         # over 10 mΩ below 100 kHz
    assert c.worst(f)[1] == pytest.approx(f[0])         # least margin at the bottom
    c.z = np.where(f > 5e5, 0.015, 0.009) + 0j          # under the mask everywhere
    assert c.passed(f) is True
    assert c.worst(f)[1] == pytest.approx(f[np.argmax(f > 5e5)])


def _setup(probe):
    return PISetup(vrms=[Vrm("VRM1", PinRef("U2", "+1V0", ["5"]), 1.0)], probes=[probe])


def _probe(**kw):
    return Probe("U1", PinRef("U1", "+1V0", ["1"]), PinRef("U1", "GND", ["2"]), **kw)


def test_mask_round_trips_through_the_setup_file():
    p = _probe(target_ohm=0.005, target_hz=1e6, target_custom=True, target_points=MASK)
    back = PISetup.from_dict(_setup(p).to_dict()).probes[0]
    assert back.target_custom and back.target_points == MASK
    assert back.target_ohm == 0.005                     # the flat target is kept
    assert back.target_mask() == sorted(MASK)
    back.target_custom = False
    assert back.target_mask() is None


@pytest.mark.parametrize("points, msg", [([], "at least one point"),
                                         ([(1e5, 0.0)], "above zero"),
                                         ([(1e5, 0.01), (1e5, 0.02)], "same frequency")])
def test_bad_masks_stop_the_solve(points, msg):
    from kipi.ac.checks import validate_ac
    errors = validate_ac(_setup(_probe(target_custom=True, target_points=points)),
                         "VRM1", "GND")
    assert any(msg in e for e in errors)


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_editor_switches_to_a_custom_mask(app):
    from kipi.ac.gui.panel import ACPanel
    panel = ACPanel()
    probe = _probe(target_ohm=0.01, target_hz=1e5)
    panel.setup.probes.append(probe)
    panel.setEnabled(True)
    panel.probe_list.reload()
    panel.probe_list.lst.setCurrentRow(0)
    ed = panel.probe_list.editor
    assert ed.obj is probe and not ed.form.isRowVisible(ed.points)

    ed.target_kind.setCurrentIndex(1)                   # custom: seeded from the flat target
    assert ed.form.isRowVisible(ed.points) and not ed.form.isRowVisible(ed.target)
    assert probe.target_custom and probe.target_points == [(1e5, 0.01)]
    ed.btn_point.click()                                # a decade up, same |Z|
    ed.points.cellWidget(1, 1).setValue(20)             # 20 mΩ
    assert flat(probe.target_points) == pytest.approx([1e5, 0.01, 1e6, 0.02])
    assert "custom target (2 points)" in panel._describe_probe(probe)

    ed.points.cellWidget(0, 2).click()                  # remove the first point
    assert flat(probe.target_points) == pytest.approx([1e6, 0.02])
    ed.target_kind.setCurrentIndex(0)                   # back to flat, mask kept
    assert not probe.target_custom and probe.target_mask() is None
    assert probe.target_ohm == pytest.approx(0.01) and len(probe.target_points) == 1


def test_plot_draws_the_mask(app):
    pytest.importorskip("matplotlib")
    from kipi.ac.gui.plot import ImpedancePlot
    from kipi.ac.solver.sweep import ACResult
    f = np.logspace(4, 8, 41)
    c = ProbeCurve("U1", np.full(f.shape, 0.015 + 0j), target_points=MASK)
    p = ImpedancePlot()
    p.set_result(ACResult(rail="VRM1", rail_nets=["+1V0"], ground="GND", freqs=f,
                          probes=[c], field_freqs=f))
    line = next(ln for ln in p.ax.get_lines() if ln.get_label() == "U1 target")
    assert line.get_ydata() == pytest.approx(mask_at(MASK, f))
    p.set_cursor(0, 0)                                  # 10 kHz: 15 mΩ over 10 mΩ
    assert "FAIL" in p.cursor_html() and "10 mΩ" in p.cursor_html()
