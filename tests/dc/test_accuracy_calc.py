"""Feedback divider DC accuracy calculations and VRM calculator wiring."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QDialog  # noqa: E402

from kipi.dc.pi.accuracy import feedback_divider_accuracy  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_feedback_divider_accuracy_combines_reference_resistor_and_leakage_corners():
    result = feedback_divider_accuracy(0.792, 0.8, 0.808, 100_000, 1, 10_000, 1,
                                       10e-9, 100e-9)
    assert result.nominal_v == pytest.approx(8.801)
    # low Vref, low Rt, high Rb, -100 nA: 0.792 * (1 + 99k / 10.1k) - 100 nA * 99k
    assert result.minimum_v == pytest.approx(8.5452683)
    assert result.maximum_v == pytest.approx(9.0613323)  # high Vref, high Rt, low Rb, +100 nA


def test_feedback_leakage_flows_either_way():
    no_leak = feedback_divider_accuracy(0.8, 0.8, 0.8, 100e3, 0, 10e3, 0, 0, 0)
    assert (no_leak.minimum_v, no_leak.nominal_v, no_leak.maximum_v) == pytest.approx(
        (8.8, 8.8, 8.8))
    out = feedback_divider_accuracy(0.8, 0.8, 0.8, 100e3, 0, 10e3, 0, -20e-9, 50e-9)
    assert out.nominal_v == pytest.approx(8.8 - 0.002)       # negative nominal lowers it
    assert (out.minimum_v, out.maximum_v) == pytest.approx((8.795, 8.805))   # +/-50 nA


@pytest.mark.parametrize("args", [
    (0.8, 0.7, 0.9, 100e3, 1, 10e3, 1, 0, 0),
    (0.7, 0.8, 0.9, 0, 1, 10e3, 1, 0, 0),
    (0.7, 0.8, 0.9, 100e3, 1, 10e3, 1, 10e-9, 5e-9),     # nominal beyond +/- max
    (0.7, 0.8, 0.9, 100e3, 1, 10e3, 1, -10e-9, 5e-9),
    (0.7, 0.8, 0.9, 100e3, 100, 10e3, 1, 0, 0),
])
def test_invalid_feedback_divider_inputs_have_no_result(args):
    assert feedback_divider_accuracy(*args) is None


def test_calculator_updates_vrm_nominal_and_absolute_limits(app, monkeypatch):
    from kipi.dc.gui import accuracy_calc
    from kipi.dc.gui.pi_panel import PIPanel
    from phase_boards import board, two_phase

    setup = two_phase(kind="smps")
    panel = PIPanel()
    panel.set_board(board(), setup)
    panel.vrm_list.lst.setCurrentRow(1)
    editor = panel.vrm_list.editor
    assert editor.form.isRowVisible(editor.btn_accuracy_calc)

    def run(dialog):
        dialog.vfb_min.setValue(0.792)
        dialog.vfb_nom.setValue(0.8)
        dialog.vfb_max.setValue(0.808)
        dialog.rtop.setValue(100_000)
        dialog.rtop_tol.setValue(1)
        dialog.rbottom.setValue(10_000)
        dialog.rbottom_tol.setValue(1)
        dialog.leak_nom.setValue(10)
        dialog.leak_max.setValue(100)
        dialog.accept()
        return QDialog.Accepted

    monkeypatch.setattr(accuracy_calc.DcAccuracyCalculator, "exec", run)
    vrm = setup.vrms[1]
    target = vrm.voltage
    editor.btn_accuracy_calc.click()
    # The divider's nominal is what it regulates to; the target loads need is left alone.
    assert vrm.nominal_v == pytest.approx(8.801) and vrm.voltage == target
    assert (vrm.acc_min, vrm.acc_max, vrm.acc_pct, vrm.acc_dv) == (
        pytest.approx(8.5452683), pytest.approx(9.0613323), False, False)
    assert editor.form.isRowVisible(editor.btn_accuracy_calc)
    editor.kind.setCurrentIndex(editor.kind.findData("ldo"))
    assert editor.form.isRowVisible(editor.btn_accuracy_calc)
    editor.kind.setCurrentIndex(editor.kind.findData("source"))
    assert not editor.form.isRowVisible(editor.btn_accuracy_calc)
    panel.close()
