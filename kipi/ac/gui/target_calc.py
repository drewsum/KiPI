"""The Target Impedance/Frequency Calculator, opened from a probe's editor."""

from __future__ import annotations

from typing import Optional, Tuple

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from kipi.ac.gui.plot import _eng
from kipi.ac.target import Target, target_from_transient
from kipi.dc.gui.pi_panel import _spin

NOTE = ("Z<sub>target</sub> = V<sub>AC</sub> / I<sub>step</sub>, flat up to "
        "f<sub>target</sub> = 1 / (π · t<sub>rise</sub>), where t<sub>rise</sub> = "
        "I<sub>step</sub> / slew rate is the rise time of the load step.")


class TargetCalculator(QDialog):
    # The inputs last applied, offered again the next time the calculator opens.
    last: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __init__(self, parent: Optional[QWidget] = None, probe_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Target Impedance/Frequency Calculator"
                            + (f": {probe_name}" if probe_name else ""))
        self.v_ac = _spin(0, 1e5, 0, 2, " mV", 5)
        self.v_ac.setToolTip("How far the supply may move from its DC value during a load "
                             "transient (the AC part of the device's supply tolerance)")
        self.i_step = _spin(0, 1e4, 0, 3, " A", 0.5)
        self.i_step.setToolTip("Size of the device's load current step")
        self.slew = _spin(0, 1e6, 0, 3, " A/µs", 1)
        self.slew.setToolTip("How fast the load current steps")
        for s, v in zip((self.v_ac, self.i_step, self.slew), self.last):
            s.setSpecialValueText("required")
            s.setKeyboardTracking(True)       # update the results while typing
            s.setValue(v)
            s.valueChanged.connect(self._update)

        self.lbl_z, self.lbl_t, self.lbl_f = QLabel(), QLabel(), QLabel()
        for lbl in (self.lbl_z, self.lbl_t, self.lbl_f):
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        form = QFormLayout()
        form.addRow("AC voltage tolerance", self.v_ac)
        form.addRow("Load step (ΔI)", self.i_step)
        form.addRow("Load step slew rate", self.slew)
        form.addRow(QLabel(""))
        form.addRow("<b>Target impedance</b>", self.lbl_z)
        form.addRow("Rise time", self.lbl_t)
        form.addRow("<b>Target frequency</b>", self.lbl_f)
        note = QLabel(NOTE)
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use for probe")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(self.buttons)
        self.setMinimumWidth(420)
        self._update()

    def target(self) -> Optional[Target]:
        return target_from_transient(self.v_ac.value() * 1e-3, self.i_step.value(),
                                     self.slew.value() * 1e6)

    def _update(self, *_):
        t = self.target()
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(t is not None)
        if t is None:
            for lbl in (self.lbl_z, self.lbl_t, self.lbl_f):
                lbl.setText("–")
            return
        self.lbl_z.setText(f"<b>{_eng(t.z_ohm, 'Ω')}</b>")
        self.lbl_t.setText(_eng(t.t_rise_s, "s"))
        self.lbl_f.setText(f"<b>{_eng(t.f_hz, 'Hz')}</b>")

    def accept(self):
        if self.target() is None:
            return
        TargetCalculator.last = (self.v_ac.value(), self.i_step.value(), self.slew.value())
        super().accept()
