"""The DC Accuracy Calculator for an adjustable regulator's feedback divider."""

from __future__ import annotations

from typing import Optional, Tuple

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout, QWidget,
)

from kipi.dc.gui.pi_panel import _spin
from kipi.dc.pi.accuracy import DcAccuracy, feedback_divider_accuracy


NOTE = ("V<sub>out</sub> = V<sub>FB</sub> · (1 + R<sub>top</sub> / R<sub>bottom</sub>) "
        "+ I<sub>FB</sub> · R<sub>top</sub>. Positive feedback pin leakage flows into "
        "the FB pin, raising the output; negative leakage flows out of it. The leakage may "
        "be anywhere within ± its maximum. The minimum and maximum output are the worst "
        "corners of the reference, resistor and leakage limits.")


class DcAccuracyCalculator(QDialog):
    """Calculate the expected adjustable regulator output range for a VRM editor."""

    last: Tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def __init__(self, parent: Optional[QWidget] = None, vrm_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("DC Accuracy Calculator" + (f": {vrm_name}" if vrm_name else ""))
        self.vfb_min = _spin(0, 100, 0, 4, " V", 0.01)
        self.vfb_max = _spin(0, 100, 0, 4, " V", 0.01)
        self.rtop = _spin(0, 1e12, 0, 3, " Ω", 100)
        self.rtop_tol = _spin(0, 99.99, 0, 2, " %", 0.1)
        self.rbottom = _spin(0, 1e12, 0, 3, " Ω", 100)
        self.rbottom_tol = _spin(0, 99.99, 0, 2, " %", 0.1)
        self.leak_max = _spin(0, 1e9, 0, 3, " nA", 1)
        self.inputs = (self.vfb_min, self.vfb_max, self.rtop, self.rtop_tol,
                       self.rbottom, self.rbottom_tol, self.leak_max)
        for s, value in zip(self.inputs, self.last):
            s.setKeyboardTracking(True)
            s.setValue(value)
            s.valueChanged.connect(self._update)
        for s in (self.vfb_min, self.vfb_max, self.rtop, self.rbottom):
            s.setSpecialValueText("required")

        self.lbl_min, self.lbl_max = QLabel(), QLabel()
        for label in (self.lbl_min, self.lbl_max):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form = QFormLayout()
        form.addRow("FB reference minimum", self.vfb_min)
        form.addRow("FB reference maximum", self.vfb_max)
        form.addRow("Top resistor", self.rtop)
        form.addRow("Top resistor tolerance", self.rtop_tol)
        form.addRow("Bottom resistor", self.rbottom)
        form.addRow("Bottom resistor tolerance", self.rbottom_tol)
        form.addRow("FB leakage maximum (±)", self.leak_max)
        self.leak_max.setToolTip("Largest FB pin leakage in either direction: the output "
                                 "range covers anywhere from -max to +max")
        form.addRow(QLabel(""))
        form.addRow("Minimum output", self.lbl_min)
        form.addRow("Maximum output", self.lbl_max)
        note = QLabel(NOTE)
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use for VRM")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(420)
        self._update()

    def result_data(self) -> Optional[DcAccuracy]:
        *vals, leak_na = [s.value() for s in self.inputs]
        return feedback_divider_accuracy(*vals, leak_na * 1e-9)

    def _update(self, *_):
        result = self.result_data()
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(result is not None)
        if result is None:
            for label in (self.lbl_min, self.lbl_max):
                label.setText("–")
            return
        self.lbl_min.setText(f"{result.minimum_v:.6g} V")
        self.lbl_max.setText(f"{result.maximum_v:.6g} V")

    def accept(self):
        result = self.result_data()
        if result is None:
            return
        DcAccuracyCalculator.last = tuple(s.value() for s in self.inputs)
        super().accept()
