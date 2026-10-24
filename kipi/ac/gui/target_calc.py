"""The Target Impedance/Frequency Calculator, opened from a probe's editor."""

from __future__ import annotations

from typing import List, Optional, Tuple

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QMenu, QPushButton,
    QVBoxLayout, QWidget,
)

from kipi.ac.gui.plot import _eng
from kipi.ac.target import (
    ProbeLoad, Target, VrmAccuracy, target_from_transient,
)
from kipi.dc.gui.pi_panel import _spin
from kipi.gui.tooltip_art import art_tip

NOTE = ("Z<sub>target</sub> = V<sub>AC</sub> / I<sub>step</sub>, flat up to "
        "f<sub>target</sub> = 1 / (π · t<sub>rise</sub>), where t<sub>rise</sub> = "
        "I<sub>step</sub> / slew rate is the rise time of the load step. The target applies "
        "above the VRM's control loop bandwidth, where one is set.")


class TargetCalculator(QDialog):
    # The inputs last applied, offered again the next time the calculator opens; until
    # then a 10 A/µs slew rate.
    last: Tuple[float, float, float] = (0.0, 0.0, 10.0)

    def __init__(self, parent: Optional[QWidget] = None, probe_name: str = "",
                 loads: Optional[List[ProbeLoad]] = None,
                 accuracies: Optional[List[VrmAccuracy]] = None,
                 step: Tuple[float, float] = (0.0, 0.0)):
        super().__init__(parent)
        self.setWindowTitle("Target Impedance/Frequency Calculator"
                            + (f": {probe_name}" if probe_name else ""))
        self.v_ac = _spin(0, 1e5, 0, 2, " mV", 5)
        self.v_ac.setToolTip("How far the supply may move from its DC value during a load "
                             "transient (the AC part of the device's supply tolerance)")
        self.i_step = _spin(0, 1e4, 0, 3, " A", 0.5)
        self.i_step.setToolTip(art_tip("Size of the device's load current step", "load_step",
                                       "step"))
        self.slew = _spin(0, 1e6, 0, 3, " A/µs", 1)
        self.slew.setToolTip(art_tip("How fast the load current steps", "load_step", "slew"))
        # The probe's own load step (#166), where it has one, over the last one applied.
        last = self.last[:1] + (tuple(step) if all(step) else self.last[1:])
        for s, v in zip((self.v_ac, self.i_step, self.slew), last):
            s.setSpecialValueText("required")
            s.setKeyboardTracking(True)       # update the results while typing
            s.setValue(v)
            s.valueChanged.connect(self._update)
        # Volts left (below, above) the DC value once a load's tolerance is copied, so the
        # VRM accuracy and the DC drop each come off their own side (#172); None until then,
        # or once the tolerance is typed in, when they come off the one value.
        self._room: Optional[Tuple[float, float]] = None
        self._filling = False
        self.v_ac.valueChanged.connect(self._typed)

        # Copy the AC tolerance (#87) and the load step from the DC load on the probe's
        # pins; with none exactly on them, a dropdown picks among the loads on its part.
        self.loads = list(loads or [])
        self.load = QComboBox()
        for load in self.loads:
            self.load.addItem(load.label())
        if not self.loads:
            self.load.addItem("None on this probe's part")
        exact = sum(load.exact for load in self.loads) == 1
        self.load.setEnabled(len(self.loads) > 1 and not exact)
        self.load.setToolTip(
            "The DC load on this probe's supply and ground pins" if exact else
            "The DC load whose tolerance and current are copied: one with its supply pins "
            "(and ground pins, where set) on the same part as this probe's")
        self.load.currentIndexChanged.connect(self._load_changed)
        self.btn_copy = QPushButton("Copy tolerance from DC load")
        self.btn_copy.clicked.connect(lambda: self.copy_tolerance(self.selected_load()))
        # Or the load's own AC tolerance (#174), where it has one: greyed out otherwise.
        self.btn_copy_ac = QPushButton("Copy AC tolerance from DC load")
        self.btn_copy_ac.setEnabled(False)
        self.btn_copy_ac.clicked.connect(lambda: self.copy_ac_tolerance(self.selected_load()))
        # Take the DC drop at the load's own pins, out of the last DC solve, off the
        # tolerance: the copper has used up that much of it before any load step.
        self.btn_drop = QPushButton("Subtract DC drop at the load")
        self.btn_drop.clicked.connect(lambda: self.subtract_drop(self.selected_load()))
        # Take the DC accuracy of the rail's VRM off the AC tolerance.
        self.accuracies = list(accuracies or [])
        self.btn_accuracy = _copy_button(
            "Subtract VRM DC accuracy", self.accuracies, self.subtract_accuracy,
            "Take the DC accuracy of the VRM driving this rail off the AC voltage tolerance: "
            "how far the output may sit from its target voltage before any load step. With "
            "the tolerance copied from a DC load, each side of the accuracy comes off the "
            "same side of the load's tolerance; otherwise its larger side comes off. Each "
            "click subtracts it again.",
            "No LDO or switcher driving this probe's rail has a DC accuracy set")
        self.btn_current = QPushButton("Copy current from DC load")
        self.btn_current.clicked.connect(lambda: self.copy_current(self.selected_load()))
        self._load_changed()

        self.lbl_z, self.lbl_t, self.lbl_f = QLabel(), QLabel(), QLabel()
        for lbl in (self.lbl_z, self.lbl_t, self.lbl_f):
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        form = QFormLayout()
        form.addRow("DC load", self.load)
        form.addRow("AC voltage tolerance", self.v_ac)
        form.addRow("", self.btn_copy)
        form.addRow("", self.btn_copy_ac)
        form.addRow("", self.btn_drop)
        form.addRow("", self.btn_accuracy)
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

    def selected_load(self) -> Optional[ProbeLoad]:
        i = self.load.currentIndex()
        return self.loads[i] if 0 <= i < len(self.loads) else None

    def _load_changed(self, *_):
        load = self.selected_load()
        t = load and load.tolerance
        self.btn_copy.setEnabled(bool(t))
        if t:
            tip = (f"Fill in the AC voltage tolerance from {t.label()}: ± its tighter side. "
                   "That is the whole tolerance; the DC drop uses up part of it.")
        elif load and load.vrm_input:
            tip = (f"{load.name}: its regulator has no input range, or no VRM feeds its "
                   "input rail")
        elif load:
            tip = f"{load.name} has no tolerance, or no VRM feeding its rail"
        else:
            tip = "No DC load on this probe's part"
        self.btn_copy.setToolTip(tip)
        ac = load and load.ac_tolerance
        self.btn_copy_ac.setEnabled(bool(ac))
        if ac:
            tip = (f"Fill in the AC voltage tolerance from {ac.label()}: ± its tighter side. "
                   "It is the swing allowed about the DC voltage, so the VRM accuracy and DC "
                   "drop are already outside it: no need to subtract them.")
        elif load and not load.vrm_input:
            tip = (f"{load.name} has no AC tolerance: enter one in its DC load, or in % with "
                   "a VRM feeding its rail")
        elif load:
            tip = f"{load.name} is a regulator's input, which has no AC tolerance"
        else:
            tip = "No DC load on this probe's part"
        self.btn_copy_ac.setToolTip(art_tip(tip, "tol_split", "ac"))
        drop = load.drop if load else None
        self.btn_drop.setEnabled(bool(drop and drop.v > 0))
        if drop and drop.v > 0:
            tip = (f"Take the {drop.v * 1e3:.4g} mV {drop.kind} at {drop.name}'s pins off the "
                   f"AC voltage tolerance: its effective voltage in the last DC solve, "
                   f"{drop.v_effective:.4g} V, against the {drop.nominal:g} V its VRM is set "
                   "to. Each click subtracts it again.")
        elif drop:
            tip = f"{drop.name} sits at its VRM's target voltage in the last DC solve"
        elif load:
            tip = (f"No DC result for {load.name}: solve the power tree on the DC tab, then "
                   "open the calculator again")
        else:
            tip = "No DC load on this probe's part"
        self.btn_drop.setToolTip(tip)
        self.btn_current.setEnabled(bool(load and load.current_a))
        if load and load.current_a:
            solved = " in the last DC solve" if load.vrm_input else ""
            tip = (f"Fill in the load step from {load.name}'s current{solved}, "
                   f"{load.current_a:.4g} A: a step from no load to all of it. Lower it if "
                   "the device's current steps by less.")
        elif load and load.vrm_input:
            tip = (f"No DC result for {load.name}: its input current comes from the power "
                   "tree, so solve it on the DC tab, then open the calculator again")
        elif load:
            tip = f"{load.name} draws no current"
        self.btn_current.setToolTip(tip)

    def copy_tolerance(self, load: Optional[ProbeLoad]):
        if load and load.tolerance:
            self._fill(load.tolerance.room())

    def copy_ac_tolerance(self, load: Optional[ProbeLoad]):
        if load and load.ac_tolerance:
            self._fill(load.ac_tolerance.room())

    def subtract_drop(self, load: Optional[ProbeLoad]):
        if not (load and load.drop):
            return
        v = load.drop.v
        self._take((v, 0.0) if load.drop.kind == "drop" else (0.0, v), v)

    def subtract_accuracy(self, a: VrmAccuracy):
        self._take(a.sides(), a.v)

    def _take(self, sides: Tuple[float, float], v: float):
        """Take ``sides`` (volts below, above) off the room left, or ``v`` off the AC
        tolerance with no room known."""
        if self._room is None:
            self._fill(None, max(0.0, self.v_ac.value() * 1e-3 - v))
        else:
            self._fill((self._room[0] - sides[0], self._room[1] - sides[1]))

    def _fill(self, room: Optional[Tuple[float, float]], v_ac: Optional[float] = None):
        """Set the AC tolerance to the smaller side of ``room``, or to ``v_ac`` volts."""
        self._filling = True
        self.v_ac.setValue(max(0.0, min(room)) * 1e3 if room else v_ac * 1e3)
        self._filling = False
        self._room = room

    def _typed(self, *_):
        if not self._filling:
            self._room = None

    def copy_current(self, load: Optional[ProbeLoad]):
        if load and load.current_a:
            self.i_step.setValue(load.current_a)

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
