"""The Target Impedance/Frequency Calculator, opened from a probe's editor."""

from __future__ import annotations

from typing import List, Optional, Tuple

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget,
)

from kipi.ac.gui.plot import _eng
from kipi.ac.target import LoadCurrent, LoadTolerance, Target, target_from_transient
from kipi.dc.gui.pi_panel import _spin

NOTE = ("Z<sub>target</sub> = V<sub>AC</sub> / I<sub>step</sub>, flat up to "
        "f<sub>target</sub> = 1 / (π · t<sub>rise</sub>), where t<sub>rise</sub> = "
        "I<sub>step</sub> / slew rate is the rise time of the load step. The target applies "
        "above the VRM's control loop bandwidth, where one is set.")
PAPER_URL = "https://doi.org/10.1109/6040.784476"
PAPER = (f'For more information on the target impedance approach, please see '
         f'<a href="{PAPER_URL}">this paper</a>: L. D. Smith, R. E. Anderson, D. W. Forehand, '
         f'T. J. Pelc and T. Roy, "Power Distribution System Design Methodology and Capacitor '
         f'Selection for Modern CMOS Technology," <i>IEEE Transactions on Advanced '
         f'Packaging</i>, vol. 22, no. 3, pp. 284–291, Aug. 1999.')


class TargetCalculator(QDialog):
    # The inputs last applied, offered again the next time the calculator opens; until
    # then a 10 A/µs slew rate.
    last: Tuple[float, float, float] = (0.0, 0.0, 10.0)

    def __init__(self, parent: Optional[QWidget] = None, probe_name: str = "",
                 loads: Optional[List[LoadTolerance]] = None,
                 currents: Optional[List[LoadCurrent]] = None):
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

        # Copy the AC tolerance from a DC load's tolerance (#87), and the load step from a
        # DC load's current; a menu when several fit.
        self.loads = list(loads or [])
        self.btn_copy = _copy_button(
            "Copy tolerance from DC load", self.loads, self.copy_tolerance,
            "Fill in the AC voltage tolerance from a DC load's tolerance: ± that % of its "
            "VRM's voltage. That is the whole tolerance; the DC drop uses up part of it.",
            "No DC load on this probe's rail has a tolerance and a VRM feeding it")
        self.currents = list(currents or [])
        self.btn_current = _copy_button(
            "Copy current from DC load", self.currents, self.copy_current,
            "Fill in the load step from a DC load's current: a step from no load to all of "
            "it. Lower it if the device's current steps by less.",
            "No DC load on this probe's rail draws a current")

        self.lbl_z, self.lbl_t, self.lbl_f = QLabel(), QLabel(), QLabel()
        for lbl in (self.lbl_z, self.lbl_t, self.lbl_f):
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        form = QFormLayout()
        form.addRow("AC voltage tolerance", self.v_ac)
        form.addRow("", self.btn_copy)
        form.addRow("Load step (ΔI)", self.i_step)
        form.addRow("", self.btn_current)
        form.addRow("Load step slew rate", self.slew)
        form.addRow(QLabel(""))
        form.addRow("<b>Target impedance</b>", self.lbl_z)
        form.addRow("Rise time", self.lbl_t)
        form.addRow("<b>Target frequency</b>", self.lbl_f)
        note = QLabel(NOTE)
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        self.lbl_paper = QLabel(PAPER)
        self.lbl_paper.setWordWrap(True)
        self.lbl_paper.setTextFormat(Qt.RichText)
        self.lbl_paper.setOpenExternalLinks(True)        # opens in the browser
        self.lbl_paper.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.lbl_paper.setToolTip(PAPER_URL)
        self.lbl_paper.setStyleSheet("color: #999;")

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use for probe")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(self.lbl_paper)
        lay.addWidget(self.buttons)
        self.setMinimumWidth(420)
        self._update()

    def copy_tolerance(self, t: LoadTolerance):
        self.v_ac.setValue(t.v_ac_v * 1e3)

    def copy_current(self, c: LoadCurrent):
        self.i_step.setValue(c.current_a)

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


def _copy_button(text: str, items: list, copy, tip: str, none_tip: str) -> QPushButton:
    """A button copying one of ``items`` (each with a ``label()``) into the calculator:
    straight away for one, from a menu for several, disabled with ``none_tip`` for none."""
    btn = QPushButton(text)
    if not items:
        btn.setEnabled(False)
        btn.setToolTip(none_tip)
        return btn
    btn.setToolTip(tip)
    if len(items) == 1:
        btn.clicked.connect(lambda: copy(items[0]))
    else:
        menu = QMenu(btn)
        for item in items:
            menu.addAction(item.label(), lambda item=item: copy(item))
        btn.setMenu(menu)
    return btn
