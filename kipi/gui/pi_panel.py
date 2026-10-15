"""Power-integrity setup panel: VRMs, loads and DC connections, all user-defined.

Each tab is a list of items with an editor form. Edits go straight into the
:class:`~kipi.pi.model.PISetup`, which is validated after every change.
"""

from __future__ import annotations

import copy
import html
import math
import re
from contextlib import contextmanager
from functools import partial
from typing import Callable, Dict, List, Optional

from qtpy.QtCore import QSettings, Qt, Signal
from qtpy.QtGui import QColor
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMenu, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout,
    QWidget,
)

from kipi.model.board import BoardSnapshot
from kipi.model.nets import guess_voltage, sorted_nets
from kipi.model.parts import resistor_ohms
from kipi.pi.model import DcLink, Load, PinRef, PISetup, Vrm, validate
from kipi.gui.sortable import make_sortable, reset_sort, set_row
from kipi.pi.tree import fmt_drop
from kipi.viz.results import FIELDS

NONE = "(none)"
UOHM_CM = 1e-5          # 1 µΩ·cm in Ω·mm, the unit the solver works in
# Copper thickness display units, as mm per unit. 1 oz/ft² is taken as 35 µm, the usual
# nominal value (KiCad's default 35 µm and 17.5 µm foils read as 1 oz and ½ oz).
THICKNESS_UNITS = {"µm": 1e-3, "mil": 0.0254, "oz": 0.035}


def format_thickness(mm: float, unit: str) -> str:
    value = mm / THICKNESS_UNITS[unit]
    if unit == "µm":
        return f"{value:.1f} µm"
    if unit == "mil":
        return f"{value:.2f} mil"
    return f"{value:.3g} oz"


def _spin(lo, hi, value, decimals, suffix, step=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setValue(value)
    s.setSuffix(suffix)
    if step:
        s.setSingleStep(step)
    s.setKeyboardTracking(False)
    return s


def _optional_spin(lo, hi, decimals, suffix, step) -> QDoubleSpinBox:
    """A spin box whose lowest value, one step below ``lo``, reads "not set"."""
    s = _spin(lo - step, hi, lo - step, decimals, suffix, step)
    s.setSpecialValueText("not set")
    return s


def _optional_volts() -> QDoubleSpinBox:
    return _optional_spin(-1000, 1000, 3, " V", 0.1)


def _get_optional(s: QDoubleSpinBox) -> Optional[float]:
    return None if s.value() <= s.minimum() else s.value()


def _set_optional(s: QDoubleSpinBox, value: Optional[float]):
    s.setValue(s.minimum() if value is None else value)


def make_searchable(combo: QComboBox):
    """Let the user type to filter a combo box (substring, case-insensitive).

    Text that matches no item is discarded when editing finishes, so the combo always
    holds a real choice.
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    comp = combo.completer()
    comp.setCompletionMode(QCompleter.PopupCompletion)
    comp.setFilterMode(Qt.MatchContains)
    comp.setCaseSensitivity(Qt.CaseInsensitive)
    combo.lineEdit().setPlaceholderText("type to search…")
    # Show the start of long entries (the reference), not the end.
    combo.currentIndexChanged.connect(lambda _i: combo.lineEdit().setCursorPosition(0))

    def settle():
        text = combo.lineEdit().text()
        i = combo.findText(text, Qt.MatchFixedString)
        if i < 0:
            # Accept a unique partial match (e.g. "u402"); otherwise restore the choice.
            hits = [k for k in range(combo.count())
                    if text and text.lower() in combo.itemText(k).lower()]
            i = hits[0] if len(hits) == 1 else combo.currentIndex()
        if i >= 0:
            if i != combo.currentIndex():
                combo.setCurrentIndex(i)
            combo.lineEdit().setText(combo.itemText(i))
    combo.lineEdit().editingFinished.connect(settle)


def parts_on_net(snap: BoardSnapshot, net: str):
    """[(ref, value, [pad numbers])] for parts with pads on ``net``, busiest first."""
    _, _, pads, _ = snap.items_on_net(net)
    by_ref: Dict[str, List[str]] = {}
    for p in pads:
        if p.footprint:
            by_ref.setdefault(p.footprint, []).append(p.number)
    values = {f.reference: f.value for f in snap.footprints}
    return sorted(((r, values.get(r, ""), sorted(set(n))) for r, n in by_ref.items()),
                  key=lambda t: (-len(t[2]), t[0]))


# ---------------------------------------------------------------------------
# Pin picker: net + part + pin numbers
# ---------------------------------------------------------------------------

class PinPicker(QWidget):
    changed = Signal()

    def __init__(self, panel: "PIPanel", optional: bool = False, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.optional = optional
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.cmb_net = QComboBox(self)
        make_searchable(self.cmb_net)
        self.cmb_net.setMinimumContentsLength(10)
        self.cmb_part = QComboBox(self)
        make_searchable(self.cmb_part)
        self.cmb_part.setMinimumContentsLength(8)
        self.txt_pins = QLineEdit(self)
        self.txt_pins.setPlaceholderText("all pins")
        self.txt_pins.setMaximumWidth(90)
        lay.addWidget(self.cmb_net, 3)
        lay.addWidget(self.cmb_part, 3)
        lay.addWidget(self.txt_pins, 2)
        self.cmb_net.currentIndexChanged.connect(self._net_changed)
        self.cmb_part.currentIndexChanged.connect(self._part_changed)
        self.txt_pins.editingFinished.connect(self.changed.emit)
        self._loading = False

    def net(self) -> str:
        """The selected net (not the half-typed search text)."""
        i = self.cmb_net.currentIndex()
        return self.cmb_net.itemText(i) if i >= 0 else ""

    def _select_net(self, name: str):
        i = self.cmb_net.findText(name, Qt.MatchFixedString)
        if i >= 0:
            self.cmb_net.setCurrentIndex(i)

    def refresh_nets(self, nets: List[str]):
        cur = self.net()
        self._loading = True
        self.cmb_net.clear()
        if self.optional:
            self.cmb_net.addItem(NONE)
        self.cmb_net.addItems(nets)
        if cur:
            self._select_net(cur)
        self._loading = False
        self._fill_parts()

    def _fill_parts(self, keep: Optional[str] = None):
        net = self.net()
        keep = keep or self.cmb_part.currentData()
        self.cmb_part.blockSignals(True)
        self.cmb_part.clear()
        if net and net != NONE and self.panel.snapshot is not None:
            for ref, value, pins in parts_on_net(self.panel.snapshot, net):
                self.cmb_part.addItem(f"{ref} {value} ({len(pins)})", ref)
        i = self.cmb_part.findData(keep) if keep else -1
        if i >= 0:
            self.cmb_part.setCurrentIndex(i)
        self.cmb_part.blockSignals(False)
        self.cmb_part.setEnabled(net != NONE)
        self.txt_pins.setEnabled(net != NONE)
        self._update_hint()

    def _update_hint(self):
        net, ref = self.net(), self.cmb_part.currentData()
        if self.panel.snapshot is None or not ref:
            return
        pins = next((p for r, _, p in parts_on_net(self.panel.snapshot, net) if r == ref), [])
        self.txt_pins.setPlaceholderText(f"all: {','.join(pins[:8])}{'…' if len(pins) > 8 else ''}")

    def _net_changed(self, _):
        if self._loading:
            return
        self._fill_parts()
        self.changed.emit()

    def _part_changed(self, _):
        self._update_hint()
        if not self._loading:
            self.changed.emit()

    def set_value(self, ref: Optional[PinRef]):
        self._loading = True
        if ref is None:
            if self.optional:
                self._select_net(NONE)
            self.txt_pins.clear()
        else:
            if self.cmb_net.findText(ref.net) < 0:
                self.cmb_net.addItem(ref.net)     # keep nets used by the setup selectable
            self._select_net(ref.net)
            self._fill_parts(keep=ref.ref)
            self.txt_pins.setText(",".join(ref.pins))
        self._loading = False
        self._fill_parts(keep=ref.ref if ref else None)

    def value(self) -> Optional[PinRef]:
        net = self.net()
        ref = self.cmb_part.currentData()
        if not net or net == NONE or not ref:
            return None
        pins = [t for t in re.split(r"[,\s]+", self.txt_pins.text().strip()) if t]
        return PinRef(ref, net, pins)


# ---------------------------------------------------------------------------
# Editors
# ---------------------------------------------------------------------------

class _Editor(QWidget):
    changed = Signal()

    def __init__(self, panel: "PIPanel", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.form = QFormLayout(self)
        self.obj = None
        self._loading = False
        self.pickers: List[PinPicker] = []

    def _picker(self, optional=False) -> PinPicker:
        p = PinPicker(self.panel, optional, self)
        p.changed.connect(self._store)
        self.pickers.append(p)
        return p

    def _watch(self, *widgets):
        for w in widgets:
            sig = getattr(w, "valueChanged", None) or getattr(w, "currentIndexChanged", None)
            if isinstance(w, QLineEdit):
                sig = w.editingFinished
            sig.connect(self._store)

    def refresh_nets(self, nets):
        # Repopulating the pickers moves their selections; that must not be saved into
        # the item being edited (nor re-guess a VRM's voltage). Callers reload it after.
        loading, self._loading = self._loading, True
        try:
            for p in self.pickers:
                p.refresh_nets(nets)
        finally:
            self._loading = loading

    def load(self, obj):
        self._loading = True
        self.obj = obj
        self._load(obj)
        self._loading = False

    def _store(self, *_):
        if self._loading or self.obj is None:
            return
        self._save(self.obj)
        self.changed.emit()


class VrmEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.kind = QComboBox()
        self.kind.addItem("Source (ideal supply, no input)", "source")
        self.kind.addItem("LDO (I_in = I_out + I_q)", "ldo")
        self.kind.addItem("Switcher (P_in = P_out / η)", "smps")
        self.output = self._picker()
        self.voltage = _spin(-1000, 1000, 3.3, 4, " V", 0.1)
        self.eff = _spin(1, 100, 90, 1, " %", 1)
        self.iq = _spin(0, 10000, 0, 3, " mA", 0.1)
        self.i_limit = _spin(0, 10000, 0, 3, " A", 0.1)
        self.i_limit.setSpecialValueText("no limit")
        self.i_limit.setToolTip("PASS if the VRM's total output current (all phases) is at "
                                "most this")
        self.dropout = _spin(0, 100, 0, 3, " V", 0.05)
        self.vin_min = _optional_volts()
        self.vin_max = _optional_volts()
        self.input = self._picker(optional=True)
        self.sense = self._picker(optional=True)
        self.ground = self._picker(optional=True)
        self.ground_sense = self._picker(optional=True)
        # Multi-phase: the output, input and ground pickers show the selected phase.
        self.n_phases = QSpinBox()
        self.n_phases.setRange(1, 32)
        self.n_phases.setToolTip("Parallel phases; each carries an equal share of the output "
                                 "current and has its own output, input and ground pins")
        self.cmb_phase = QComboBox()
        self.cmb_phase.setToolTip("The phase whose pins are shown below")
        self._phase = 0
        f.addRow("Name", self.name)
        f.addRow("Type", self.kind)
        f.addRow("Set voltage", self.voltage)
        f.addRow("Efficiency", self.eff)
        f.addRow("Quiescent", self.iq)
        f.addRow("Output current limit", self.i_limit)
        f.addRow("Dropout", self.dropout)
        f.addRow("Input min", self.vin_min)
        f.addRow("Input max", self.vin_max)
        f.addRow("Phases", self.n_phases)
        f.addRow("Edit phase", self.cmb_phase)
        f.addRow("Output pins", self.output)
        f.addRow("Input pins", self.input)
        f.addRow("Ground pins", self.ground)
        f.addRow("Sense pins", self.sense)
        f.addRow("Ground sense pins", self.ground_sense)
        self._watch(self.name, self.kind, self.voltage, self.eff, self.iq, self.i_limit,
                    self.dropout, self.vin_min, self.vin_max)
        self.kind.currentIndexChanged.connect(self._kind_rows)
        self.n_phases.valueChanged.connect(self._phase_count_changed)
        self.cmb_phase.currentIndexChanged.connect(self._phase_selected)
        self.output.cmb_net.currentIndexChanged.connect(
            lambda _i: self._guess_voltage(self.output.net()))

    def _kind_rows(self, *_):
        kind = self.kind.currentData()
        self.form.setRowVisible(self.eff, kind == "smps")
        self.form.setRowVisible(self.iq, kind != "source")
        self.form.setRowVisible(self.input, kind != "source")
        self.form.setRowVisible(self.dropout, kind == "ldo")
        self.form.setRowVisible(self.vin_min, kind != "source")
        self.form.setRowVisible(self.vin_max, kind != "source")

    def _guess_voltage(self, net):
        if not self._loading and net and self._phase == 0:
            self.voltage.setValue(guess_voltage(net))

    def _phase_rows(self):
        """Fill the phase selector and label the per-phase pickers."""
        n = self.n_phases.value()
        self._phase = min(self._phase, n - 1)
        self.cmb_phase.blockSignals(True)
        self.cmb_phase.clear()
        self.cmb_phase.addItems([f"Phase {k}" for k in range(1, n + 1)])
        self.cmb_phase.setCurrentIndex(self._phase)
        self.cmb_phase.blockSignals(False)
        self.form.setRowVisible(self.cmb_phase, n > 1)
        tag = f" (phase {self._phase + 1})" if n > 1 else ""
        for w, text in ((self.output, "Output pins"), (self.input, "Input pins"),
                        (self.ground, "Ground pins")):
            self.form.labelForField(w).setText(text + tag)

    def _show_phase(self, v: Vrm):
        ph = v.all_phases()[self._phase]
        loading, self._loading = self._loading, True
        self.output.set_value(ph.output)
        self.input.set_value(ph.input)
        self.ground.set_value(ph.ground)
        self._loading = loading

    def _phase_count_changed(self, _n):
        if self._loading or self.obj is None:
            return
        self._store()
        self._phase_rows()
        self._show_phase(self.obj)

    def _phase_selected(self, index):
        if self._loading or self.obj is None or index < 0:
            return
        self._phase = index
        self._phase_rows()
        self._show_phase(self.obj)

    def _load(self, v: Vrm):
        self.name.setText(v.name)
        self.kind.setCurrentIndex(max(0, self.kind.findData(v.kind)))
        self.voltage.setValue(v.voltage)
        self.eff.setValue(v.efficiency * 100)
        self.iq.setValue(v.quiescent_a * 1e3)
        self.i_limit.setValue(v.current_limit_a or 0.0)
        self.n_phases.setValue(len(v.phases) + 1)
        self._phase = 0
        self._phase_rows()
        self._show_phase(v)
        self.dropout.setValue(v.dropout_v)
        _set_optional(self.vin_min, v.vin_min)
        _set_optional(self.vin_max, v.vin_max)
        self.sense.set_value(v.sense)
        self.ground_sense.set_value(v.ground_sense)
        self._kind_rows()

    def _save(self, v: Vrm):
        v.name = self.name.text().strip() or v.name
        v.kind = self.kind.currentData()
        v.voltage = self.voltage.value()
        v.efficiency = self.eff.value() / 100
        v.quiescent_a = self.iq.value() / 1e3
        v.current_limit_a = self.i_limit.value() or None
        v.dropout_v = self.dropout.value()
        v.vin_min = _get_optional(self.vin_min)
        v.vin_max = _get_optional(self.vin_max)
        v.sense = self.sense.value()
        v.ground_sense = self.ground_sense.value()
        phases = v.all_phases()
        ph = phases[min(self._phase, len(phases) - 1)]
        ph.output = self.output.value() or ph.output
        ph.input = self.input.value()
        ph.ground = self.ground.value()
        # New phases start as a copy of the last one; the user then picks their own pins.
        n = self.n_phases.value()
        while len(phases) < n:
            phases.append(copy.deepcopy(phases[-1]))
        del phases[n:]
        for ph in phases:
            if v.kind == "source":
                ph.input = None
        v.output, v.input, v.ground = phases[0].output, phases[0].input, phases[0].ground
        v.phases = phases[1:]


class LoadEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.supply = self._picker()
        self.mode = QComboBox()
        self.mode.addItem("Equal across the supply pins", "equal")
        self.mode.addItem("Per pin group", "groups")
        self.current = _spin(-1000, 1000, 0.1, 4, " A", 0.01)
        self.groups = QTableWidget(0, 4)
        self.groups.setHorizontalHeaderLabels(["Group", "Pins", "Current", ""])
        hdr = self.groups.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.groups.setColumnWidth(3, 28)
        self.groups.verticalHeader().setVisible(False)
        self.groups.setMinimumHeight(110)
        self.btn_group = QPushButton("Add pin group")
        self.btn_group.clicked.connect(lambda: (self._add_group_row("", "", 0.0), self._store()))
        self.btn_each = QPushButton("One group per pin")
        self.btn_each.setToolTip("Replace the groups with one group per supply pin")
        self.btn_each.clicked.connect(self._group_per_pin)
        gb = QHBoxLayout()
        gb.addWidget(self.btn_group)
        gb.addWidget(self.btn_each)
        self.lbl_total = QLabel()
        self.ground = self._picker(optional=True)
        self.tol = _spin(0, 100, 0, 2, " %", 0.5)
        self.tol.setPrefix("± ")
        self.tol.setSpecialValueText("no limit")
        self.tol.setToolTip("PASS if every supply pin is within ± this % of the VRM set "
                            "voltage: no pin drops below it or is boosted above it by more")
        self.heat = _optional_spin(0, 1000, 3, " W", 0.01)
        self.heat.setToolTip("Heat the part puts into the board. Not set: all the power it "
                             "takes from its supply pins (V x I). Set it for a part that passes "
                             "power on (load switch, LED or motor driver).")
        f.addRow("Name", self.name)
        f.addRow("Supply pins", self.supply)
        f.addRow("Current split", self.mode)
        f.addRow("Current", self.current)
        f.addRow(self.groups)
        f.addRow(gb)
        f.addRow("Total", self.lbl_total)
        f.addRow("Ground pins", self.ground)
        f.addRow("Tolerance", self.tol)
        f.addRow("Heat", self.heat)
        self._watch(self.name, self.current, self.tol, self.mode, self.heat)
        self.mode.currentIndexChanged.connect(self._mode_rows)
        self._gb_widgets = (self.btn_group, self.btn_each)

    def _mode_rows(self, *_):
        grouped = self.mode.currentData() == "groups"
        self.form.setRowVisible(self.current, not grouped)
        self.form.setRowVisible(self.groups, grouped)
        self.form.setRowVisible(self.lbl_total, grouped)
        for w in self._gb_widgets:
            w.setVisible(grouped)

    def _add_group_row(self, name: str, pins: str, current: float):
        r = self.groups.rowCount()
        self.groups.insertRow(r)
        for col, text in ((0, name), (1, pins)):
            e = QLineEdit(text)
            e.setPlaceholderText("optional" if col == 0 else "e.g. 15,28")
            e.editingFinished.connect(self._store)
            self.groups.setCellWidget(r, col, e)
        sp = _spin(-1000, 1000, current, 4, " A", 0.01)
        sp.valueChanged.connect(self._store)
        self.groups.setCellWidget(r, 2, sp)
        btn = QToolButton()
        btn.setText("✕")
        btn.setAutoRaise(True)
        btn.clicked.connect(lambda _=False, b=btn: self._remove_group_of(b))
        self.groups.setCellWidget(r, 3, btn)

    def _remove_group_of(self, widget):
        for r in range(self.groups.rowCount()):
            if self.groups.cellWidget(r, 3) is widget:
                self.groups.removeRow(r)
                self._store()
                return

    def _supply_pins(self) -> List[str]:
        ref = self.supply.value()
        if ref is None or self.panel.snapshot is None:
            return []
        return ref.pins or next((p for r, _, p in parts_on_net(self.panel.snapshot, ref.net)
                                 if r == ref.ref), [])

    def _fill_per_pin(self, total: float):
        pins = self._supply_pins()
        self.groups.setRowCount(0)
        for pin in pins:
            self._add_group_row(f"pin {pin}", pin, total / max(1, len(pins)))

    def _group_per_pin(self):
        current = self._groups_value()
        total = sum(g.current for g in current) if current else self.current.value()
        self._fill_per_pin(total)
        self._store()

    def _groups_value(self):
        from kipi.pi.model import PinGroup
        out = []
        for r in range(self.groups.rowCount()):
            pins = [t for t in re.split(r"[,\s]+", self.groups.cellWidget(r, 1).text().strip()) if t]
            out.append(PinGroup(pins, self.groups.cellWidget(r, 2).value(),
                                self.groups.cellWidget(r, 0).text().strip()))
        return out

    def _load(self, l: Load):
        self.name.setText(l.name)
        self.supply.set_value(l.supply)
        self.current.setValue(l.current)
        self.groups.setRowCount(0)
        for g in l.groups:
            self._add_group_row(g.name, ",".join(g.pins), g.current)
        self.mode.setCurrentIndex(1 if l.groups else 0)
        self.ground.set_value(l.ground)
        self.tol.setValue(l.tolerance_pct or 0.0)
        _set_optional(self.heat, l.dissipation_w)
        self.lbl_total.setText(f"{l.total_current():.4g} A")
        self._mode_rows()

    def _save(self, l: Load):
        new_supply = self.supply.value() or l.supply
        typed = self.name.text().strip() or l.name
        # A name still equal to the old part's reference was the automatic default:
        # follow the newly picked part.
        if typed == l.supply.ref and new_supply.ref != l.supply.ref:
            typed = new_supply.ref
            self.name.setText(typed)
        l.name = typed
        l.supply = new_supply
        l.current = self.current.value()
        if self.mode.currentData() == "groups":
            if self.groups.rowCount() == 0:
                # Switching to groups: start with one group per pin, same total current.
                self._fill_per_pin(l.current)
            l.groups = self._groups_value()
        else:
            l.groups = []
        l.ground = self.ground.value()
        l.tolerance_pct = self.tol.value() or None
        l.dissipation_w = _get_optional(self.heat)
        self.lbl_total.setText(f"{l.total_current():.4g} A")


class LinkEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.a = self._picker()
        self.b = self._picker()
        self.r = _spin(0, 1e9, 0, 3, " mΩ", 1)
        self.r.setSpecialValueText("0 (short)")
        f.addRow("Name", self.name)
        f.addRow("Side A pins", self.a)
        f.addRow("Side B pins", self.b)
        f.addRow("Resistance", self.r)
        hint = QLabel("A part that passes DC between two nets (sense resistor, ferrite, "
                      "fuse, load switch…). The nets it joins are simulated together.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        f.addRow(hint)
        self._watch(self.name, self.r)

    def _load(self, k: DcLink):
        self.name.setText(k.name)
        self.a.set_value(k.side_a)
        self.b.set_value(k.side_b)
        self.r.setValue(k.resistance * 1e3)

    def _save(self, k: DcLink):
        new_a = self.a.value() or k.side_a
        typed = self.name.text().strip() or k.name
        if typed == k.side_a.ref and new_a.ref != k.side_a.ref:
            typed = new_a.ref
            self.name.setText(typed)
        parts = (k.side_a.ref, k.side_b.ref)
        k.name = typed
        k.side_a = new_a
        k.side_b = self.b.value() or k.side_b
        k.resistance = self.r.value() / 1e3
        # Both sides just became the same resistor: take its resistance from its value.
        if (k.side_a.ref, k.side_b.ref) != parts and k.side_a.ref == k.side_b.ref:
            ohms = resistor_ohms(self.panel.snapshot, k.side_a.ref)
            if ohms is not None:
                k.resistance = ohms
                self.r.blockSignals(True)
                self.r.setValue(ohms * 1e3)
                self.r.blockSignals(False)


class ItemList(QWidget):
    """A list of setup items plus the editor for the selected one."""
    changed = Signal()

    def __init__(self, items: Callable[[], list], editor: _Editor, make_new: Callable[[], object],
                 describe: Callable[[object], str], parent=None):
        super().__init__(parent)
        self.items, self.editor, self.make_new, self.describe = items, editor, make_new, describe
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.lst = QListWidget(self)
        self.lst.currentRowChanged.connect(self._select)
        row = QHBoxLayout()
        for text, slot in (("Add", self._add), ("Duplicate", self._dup), ("Remove", self._remove)):
            b = QPushButton(text, self)
            b.clicked.connect(slot)
            row.addWidget(b)
        split = QSplitter(Qt.Vertical, self)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.lst)
        tl.addLayout(row)
        split.addWidget(top)
        split.addWidget(editor)
        split.setSizes([120, 280])
        lay.addWidget(split)
        editor.changed.connect(self._edited)
        editor.setEnabled(False)

    def reload(self):
        row = self.lst.currentRow()
        self.lst.blockSignals(True)
        self.lst.clear()
        for obj in self.items():
            self.lst.addItem(self.describe(obj))
        self.lst.blockSignals(False)
        n = self.lst.count()
        self.lst.setCurrentRow(min(max(row, 0), n - 1) if n else -1)
        self._select(self.lst.currentRow())

    def _select(self, row):
        items = self.items()
        if 0 <= row < len(items):
            self.editor.setEnabled(True)
            self.editor.load(items[row])
        else:
            self.editor.setEnabled(False)
            self.editor.obj = None

    def _edited(self):
        row = self.lst.currentRow()
        if 0 <= row < len(self.items()):
            self.lst.item(row).setText(self.describe(self.items()[row]))
        self.changed.emit()

    def _add(self):
        obj = self.make_new()
        if obj is None:
            return
        self.items().append(obj)
        self.reload()
        self.lst.setCurrentRow(self.lst.count() - 1)
        self.changed.emit()

    def _dup(self):
        import copy
        row = self.lst.currentRow()
        if row < 0:
            return
        obj = copy.deepcopy(self.items()[row])
        obj.name = _unique(obj.name, [x.name for x in self.items()])
        self.items().append(obj)
        self.reload()
        self.lst.setCurrentRow(self.lst.count() - 1)
        self.changed.emit()

    def _remove(self):
        row = self.lst.currentRow()
        if row < 0:
            return
        del self.items()[row]
        self.reload()
        self.changed.emit()


def _unique(base: str, taken: List[str]) -> str:
    if base not in taken:
        return base
    m = re.match(r"(.*?)(\d+)$", base)
    stem, k = (m.group(1), int(m.group(2))) if m else (base + " ", 1)
    while f"{stem}{k}" in taken:
        k += 1
    return f"{stem}{k}"


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class PIPanel(QWidget):
    solve_requested = Signal(object)      # PISetup
    field_changed = Signal(str)
    group_changed = Signal(str)
    show_geometry = Signal()
    save_requested = Signal()
    arrows_toggled = Signal(bool)
    via_selected = Signal(str, str)        # group name, via / PTH pad id
    export_requested = Signal(str)         # "csv", "vtk" or "results"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot: Optional[BoardSnapshot] = None
        self.setup = PISetup()
        self._nets: List[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        row = QHBoxLayout()
        self.lbl_file = QLabel("No board", self)
        self.lbl_file.setStyleSheet("color: #999;")
        self.lbl_file.setWordWrap(True)
        b_save = QPushButton("Save", self)
        b_save.setToolTip("Save the setup next to the board")
        b_save.clicked.connect(self.save_requested.emit)
        row.addWidget(self.lbl_file, 1)
        row.addWidget(b_save)
        lay.addLayout(row)

        self.tabs = QTabWidget(self)
        self.vrm_list = ItemList(lambda: self.setup.vrms, VrmEditor(self), self._new_vrm,
                                 self._describe_vrm)
        self.load_list = ItemList(lambda: self.setup.loads, LoadEditor(self), self._new_load,
                                  self._describe_load)
        self.link_list = ItemList(lambda: self.setup.links, LinkEditor(self), self._new_link,
                                  self._describe_link)
        self.tabs.addTab(self.vrm_list, "VRMs")
        self.tabs.addTab(self.load_list, "Loads")
        self.tabs.addTab(self.link_list, "DC connections")
        self.tabs.addTab(self._settings_tab(), "Mesh")
        self.tabs.addTab(self._copper_tab(), "Copper")
        self.tabs.addTab(self._thermal_tab(), "Thermal")
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.changed.connect(self._on_changed)
        lay.addWidget(self.tabs, 3)

        self.lbl_errors = QLabel(self)
        self.lbl_errors.setWordWrap(True)
        self.lbl_errors.setStyleSheet("color: #ff8080;")
        lay.addWidget(self.lbl_errors)
        row = QHBoxLayout()
        self.btn_solve = QPushButton("Solve all", self)
        self.btn_solve.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_solve.clicked.connect(lambda: self.solve_requested.emit(self.setup))
        self.chk_report = QCheckBox("HTML report", self)
        self.chk_report.setToolTip("Also write an HTML report (interactive 3D, per-layer maps "
                                   "and tables) next to the board")
        self.chk_report.toggled.connect(lambda on: setattr(self.setup, "html_report", on))
        self.btn_report = QPushButton("Report", self)
        self.btn_report.setToolTip("Open the last HTML report in your browser")
        self.btn_report.setVisible(False)
        self.btn_report.clicked.connect(self._open_report)
        self.report_path = None
        row.addWidget(self.btn_solve, 1)
        row.addWidget(self.chk_report)
        row.addWidget(self.btn_report)
        lay.addLayout(row)

        g_res = QGroupBox("Results", self)
        lr = QVBoxLayout(g_res)
        row = QHBoxLayout()
        self.cmb_group = QComboBox(g_res)
        self.cmb_group.setToolTip("Net group shown in 3D")
        self.cmb_group.currentTextChanged.connect(
            lambda t: t and self.group_changed.emit(t))
        self.cmb_field = QComboBox(g_res)
        for key, (_, _, label, unit) in FIELDS.items():
            if key != "temperature":           # added once a thermal solve exists
                self.cmb_field.addItem(f"{label} ({unit})", key)
        self.cmb_field.currentIndexChanged.connect(
            lambda i: self.field_changed.emit(self.cmb_field.itemData(i)))
        self.cmb_field.currentIndexChanged.connect(self._update_arrow_box)
        self.chk_arrows = QCheckBox("Arrows", g_res)
        self.chk_arrows.setToolTip("Show current-flow arrows over the copper")
        self.chk_arrows.toggled.connect(self.arrows_toggled.emit)
        self._update_arrow_box()
        b_export = QPushButton("Export ▾", g_res)
        menu = QMenu(b_export)
        menu.addAction("Tables as CSV…", lambda: self.export_requested.emit("csv"))
        menu.addAction("3D results for ParaView (.vtm)…", lambda: self.export_requested.emit("vtk"))
        menu.addAction("Save results…", lambda: self.export_requested.emit("results"))
        b_export.setMenu(menu)
        self.btn_export = b_export
        b_export.setEnabled(False)
        b_geom = QPushButton("Show copper", g_res)
        b_geom.clicked.connect(self.show_geometry.emit)
        row.addWidget(self.cmb_group, 2)
        row.addWidget(self.cmb_field, 2)
        row.addWidget(self.chk_arrows)
        row.addWidget(b_geom)
        row.addWidget(b_export)
        lr.addLayout(row)
        self.res_tabs = QTabWidget(g_res)
        self.tbl_loads = self._table(["Load", "Group", "I (A)", "V eff (V)",
                                      "Drop / boost (mV)", "Worst pin (mV)", "Worst %",
                                      "Tolerance %", ""], sortable=True)
        self.tbl_loads.setToolTip("Drop: below the VRM set point. Boost: above it, e.g. near "
                                  "a remote-sensed VRM's output pins. The worst pin is the one "
                                  "furthest from the set point either way.")
        self.tbl_vrms = self._table(["VRM", "Type", "V out", "I out (A)", "V in", "I in (A)",
                                     "P in (W)", "Loss (W)", "I limit (A)", ""], sortable=True)
        self.tbl_links = self._table(["Part", "Group", "R (mΩ)", "I (A)", "P (mW)"],
                                      sortable=True)
        self.txt_notes = QTextBrowser(g_res)
        self.res_tabs.addTab(self.tbl_loads, "Loads")
        self.res_tabs.addTab(self.tbl_vrms, "VRMs")
        self.res_tabs.addTab(self.tbl_links, "DC connections")
        self.res_tabs.addTab(self._vias_tab(), "Vias")
        self.tbl_losses = self._table(["Group"], sortable=True)
        self.res_tabs.addTab(self.tbl_losses, "Losses")
        self.res_tabs.addTab(self.txt_notes, "Notes")
        self.tbl_loads.cellDoubleClicked.connect(self._row_to_group)
        lr.addWidget(self.res_tabs)
        lay.addWidget(g_res, 2)
        self.setEnabled(False)

    # ------------------------------------------------------------------ widgets
    def set_report(self, path):
        self.report_path = path
        self.btn_report.setVisible(path is not None)

    def _open_report(self):
        if self.report_path:
            from qtpy.QtCore import QUrl
            from qtpy.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_path)))

    def _set_temperature_field(self, available: bool):
        i = self.cmb_field.findData("temperature")
        if available and i < 0:
            self.cmb_field.addItem("Temperature (°C)", "temperature")
        elif not available and i >= 0:
            if self.cmb_field.currentIndex() == i:
                self.cmb_field.setCurrentIndex(0)
            self.cmb_field.removeItem(i)

    def _update_arrow_box(self, *_):
        """Arrows only apply to the current-density view."""
        on = self.cmb_field.currentData() == "current_density"
        self.chk_arrows.setEnabled(on)
        self.chk_arrows.setToolTip("Show current-flow arrows over the copper" if on else
                                   "Current arrows are shown with Current density")

    def _vias_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Current limit"))
        self.spin_via_limit = _spin(0, 1e6, 0, 1, " mA", 10)
        self.spin_via_limit.setSpecialValueText("none")
        self.spin_via_limit.setToolTip("Vias carrying more than this are marked FAIL")
        self.spin_via_limit.valueChanged.connect(self._via_limit_changed)
        row.addWidget(self.spin_via_limit)
        row.addStretch(1)
        row.addWidget(QLabel("double-click a row to show it"))
        v.addLayout(row)
        self.tbl_vias = self._table(["Group", "Via / pad", "x (mm)", "y (mm)", "Drill (mm)",
                                     "I (mA)", "P (mW)", ""], sortable=True)
        self.tbl_vias.cellDoubleClicked.connect(self._via_row_clicked)
        v.addWidget(self.tbl_vias)
        return w

    def _via_limit_changed(self, value):
        self.setup.via_current_limit_a = (value / 1e3) or None
        if getattr(self, "_tree", None) is not None:
            self._set_via_status()      # the rows and their order do not depend on the limit

    def _via_row_clicked(self, row, _col):
        it = self.tbl_vias.item(row, 0)
        if it is not None:
            self.via_selected.emit(it.text(), it.data(Qt.UserRole))

    def _fill_vias(self, max_rows: int = 500):
        from kipi.pi.report import via_rows
        rows = via_rows(self._tree, self.snapshot)[:max_rows]
        t = self.tbl_vias
        reset_sort(t)
        with _bulk_fill(t):
            t.setRowCount(len(rows))
            for r, v in enumerate(rows):
                set_row(t, r, [v.group, v.label, f"{v.x:.3f}", f"{v.y:.3f}",
                               f"{v.drill_mm:.3f}", (f"{v.current * 1e3:.2f}", v.current * 1e3),
                               (f"{v.power * 1e3:.4f}", v.power * 1e3), ""])
                t.item(r, 0).setData(Qt.UserRole, v.owner_id)
        self._set_via_status()

    def _set_via_status(self):
        """PASS / FAIL against the current limit, without rebuilding the table."""
        limit = self.setup.via_current_limit_a
        t = self.tbl_vias
        with _bulk_fill(t):
            for r in range(t.rowCount()):           # rows may have been sorted
                current = t.item(r, 5).key / 1e3
                it = t.item(r, 7)
                if limit is None:
                    it.setText("")
                    continue
                ok = current <= limit
                it.setText("PASS" if ok else "FAIL")
                it.setForeground(QColor("#60d060" if ok else "#ff6060"))

    def _fill_losses(self):
        from kipi.pi.report import loss_rows
        rows = loss_rows(self._tree, self.snapshot)
        layers = [c.name for c in self.snapshot.stackup.copper
                  if any(c.name in r.by_layer for r in rows)]
        headers = ["Group", *[f"{n} (mW)" for n in layers], "Vias (mW)", "DC parts (mW)",
                   "Total (mW)"]
        t = self.tbl_losses
        reset_sort(t)
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        vrm_rows = [v for v in self._tree.vrms if v.p_loss is not None]
        t.setRowCount(len(rows) + 1 + len(vrm_rows))
        total = 0.0
        for r, row in enumerate(rows):
            cells = [row.group, *[f"{row.by_layer.get(n, 0.0) * 1e3:.3f}" for n in layers],
                     f"{row.vias * 1e3:.3f}", f"{row.links * 1e3:.3f}", f"{row.total * 1e3:.3f}"]
            total += row.total
            set_row(t, r, cells)
        # The total and VRM conversion rows stay at the bottom whatever the sort.
        blank = [""] * (len(headers) - 2)
        r = len(rows)
        set_row(t, r, ["All copper + parts", *blank, f"{total * 1e3:.3f}"], pin=1)
        for k, v in enumerate(vrm_rows, start=r + 1):
            set_row(t, k, [f"VRM {v.name} (conversion)", *blank, f"{v.p_loss * 1e3:.3f}"],
                    pin=1, sub=k)

    def _table(self, headers, sortable: bool = False) -> QTableWidget:
        t = QTableWidget(0, len(headers), self)
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        if sortable:
            make_sortable(t)
        return t

    def _settings_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.setup.settings
        self.spin_edge = _spin(0.05, 5.0, s.max_edge_mm, 2, " mm", 0.05)
        self.spin_grading = _spin(0.0, 2.0, s.grading, 2, "", 0.05)
        from kipi.gui.via_rules_editor import ViaRulesEditor
        self.via_rules = ViaRulesEditor(w)
        self.via_rules.set_rules(s.via_rules)
        f.addRow("Max edge", self.spin_edge)
        f.addRow("Via grading", self.spin_grading)
        f.addRow(QLabel("<b>Via plating</b>"))
        f.addRow(self.via_rules)
        for sp in (self.spin_edge, self.spin_grading):
            sp.valueChanged.connect(self._settings_changed)
        self.via_rules.changed.connect(self._settings_changed)
        return w

    def _copper_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        s = self.setup.settings
        self.spin_rho = _spin(0.5, 100.0, s.rho_ohm_mm / UOHM_CM, 3, " µΩ·cm", 0.01)
        self.spin_rho.setToolTip("Resistivity at 20 °C of every layer without its own value, "
                                 "and of via barrel plating. Annealed copper is 1.724 µΩ·cm.")
        self.spin_tcr = _spin(0.0, 0.02, s.tcr_per_c, 5, " /°C", 0.0001)
        self.spin_tcr.setToolTip("Temperature coefficient of resistance, used by the "
                                 "electro-thermal co-simulation. Copper is 0.00393 /°C.")
        f.addRow("Resistivity", self.spin_rho)
        f.addRow("TCR", self.spin_tcr)
        self.cmb_thk_unit = QComboBox()
        self.cmb_thk_unit.addItems(list(THICKNESS_UNITS))
        self.cmb_thk_unit.setToolTip("Unit for the layer thicknesses below (1 oz = 35 µm). "
                                     "Thicknesses come from the board stackup in KiCad.")
        unit = QSettings("KiPI", "KiPI").value("copper/thickness_unit", "µm")
        self.cmb_thk_unit.setCurrentText(unit if unit in THICKNESS_UNITS else "µm")
        self.cmb_thk_unit.currentTextChanged.connect(self._thickness_unit_changed)
        f.addRow("Thickness unit", self.cmb_thk_unit)
        lay.addLayout(f)
        self.tbl_rho = self._table(["Layer", "Thickness", "Resistivity at 20 °C"])
        self.tbl_rho.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.tbl_rho, 1)
        note = QLabel("Per-layer values override the resistivity above, e.g. for thin "
                      "electroplated or rolled-annealed foil. 'default' uses the value above.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        lay.addWidget(note)
        for sp in (self.spin_rho, self.spin_tcr):
            sp.valueChanged.connect(self._copper_changed)
        return w

    def _fill_rho_table(self):
        t = self.tbl_rho
        layers = self.snapshot.stackup.copper if self.snapshot is not None else []
        overrides = self.setup.settings.rho_by_layer
        t.setRowCount(len(layers))
        for r, cu in enumerate(layers):
            t.setItem(r, 0, QTableWidgetItem(cu.name))
            item = QTableWidgetItem()
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, 1, item)
            sp = _spin(0.0, 100.0, overrides.get(cu.name, 0.0) / UOHM_CM, 3, " µΩ·cm", 0.01)
            sp.setSpecialValueText("default")
            sp.setToolTip(f"Resistivity of {cu.name} at 20 °C ('default' uses the "
                          f"Resistivity setting above)")
            sp.valueChanged.connect(partial(self._layer_rho_changed, cu.name))
            t.setCellWidget(r, 2, sp)
        self._show_thicknesses()

    def _show_thicknesses(self):
        unit = self.cmb_thk_unit.currentText()
        layers = self.snapshot.stackup.copper if self.snapshot is not None else []
        for r, cu in enumerate(layers):
            self.tbl_rho.item(r, 1).setText(format_thickness(cu.thickness_mm, unit))

    def _thickness_unit_changed(self, unit: str):
        QSettings("KiPI", "KiPI").setValue("copper/thickness_unit", unit)
        self._show_thicknesses()

    def _layer_rho_changed(self, name: str, value: float):
        overrides = self.setup.settings.rho_by_layer
        if value > 0:
            overrides[name] = value * UOHM_CM
        else:
            overrides.pop(name, None)
        self._on_changed()

    def _copper_changed(self, *_):
        s = self.setup.settings
        s.rho_ohm_mm = self.spin_rho.value() * UOHM_CM
        s.tcr_per_c = self.spin_tcr.value()
        self._on_changed()

    def _thermal_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        t = self.setup.thermal
        self.chk_thermal = QCheckBox("Electro-thermal co-simulation")
        self.chk_thermal.setToolTip("Iterate the DC solve with a board temperature map: copper "
                                    "loss heats the board, hotter copper is more resistive")
        self.spin_ambient = _spin(-55, 200, t.ambient_c, 1, " °C", 1)
        self.spin_iters = QSpinBox()
        self.spin_iters.setRange(1, 50)
        self.spin_iters.setValue(t.iterations)
        self.spin_airflow = _spin(0, 30, t.airflow_m_s, 1, " m/s", 0.5)
        self.spin_airflow.setSpecialValueText("still air")
        self.spin_tgrid = _spin(0, 10, t.grid_mm, 2, " mm", 0.1)
        self.spin_tgrid.setSpecialValueText("auto")
        self.spin_lam_k = _spin(0, 500, t.laminate_k_w_mk, 2, " W/m·K", 0.1)
        self.spin_lam_k.setSpecialValueText("from stackup")
        self.spin_lam_k.setToolTip("In-plane conductivity of the dielectric. 'From stackup' "
                                   "looks it up from each dielectric's material name "
                                   "(FR4, polyimide, Kapton, PTFE, Rogers, ceramic, Al...).")
        f.addRow(self.chk_thermal)
        f.addRow("Ambient", self.spin_ambient)
        f.addRow("Iterations", self.spin_iters)
        f.addRow("Airflow", self.spin_airflow)
        f.addRow("Grid", self.spin_tgrid)
        f.addRow("Laminate k", self.spin_lam_k)
        note = QLabel("Heat from every net and layer (copper, vias, DC parts, VRM conversion "
                      "loss) spreads through the board's copper and laminate and leaves both "
                      "faces by convection: h = 10 + 3.8·v W/m²K per face.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        f.addRow(note)
        self.chk_thermal.toggled.connect(self._thermal_changed)
        for wdg in (self.spin_ambient, self.spin_airflow, self.spin_tgrid, self.spin_lam_k):
            wdg.valueChanged.connect(self._thermal_changed)
        self.spin_iters.valueChanged.connect(self._thermal_changed)
        return w

    def _thermal_changed(self, *_):
        t = self.setup.thermal
        t.enabled = self.chk_thermal.isChecked()
        t.ambient_c = self.spin_ambient.value()
        t.iterations = self.spin_iters.value()
        t.airflow_m_s = self.spin_airflow.value()
        t.grid_mm = self.spin_tgrid.value()
        t.laminate_k_w_mk = self.spin_lam_k.value()
        for wdg in (self.spin_ambient, self.spin_iters, self.spin_airflow, self.spin_tgrid,
                    self.spin_lam_k):
            wdg.setEnabled(t.enabled)
        self.btn_solve.setText("Solve all (electro-thermal)" if t.enabled else "Solve all")

    def _settings_changed(self, *_):
        s = self.setup.settings
        s.max_edge_mm = self.spin_edge.value()
        s.grading = self.spin_grading.value()
        s.via_rules = self.via_rules.rules()
        self._on_changed()

    # ------------------------------------------------------------------ data
    def set_board(self, snap: BoardSnapshot, setup: PISetup, file_label: str):
        self.snapshot = snap
        self.setup = setup
        self.lbl_file.setText(file_label)
        for sp, v in ((self.spin_edge, setup.settings.max_edge_mm),
                      (self.spin_grading, setup.settings.grading),
                      (self.spin_rho, setup.settings.rho_ohm_mm / UOHM_CM),
                      (self.spin_tcr, setup.settings.tcr_per_c)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
        self.via_rules.blockSignals(True)
        self.via_rules.set_rules(setup.settings.via_rules)
        self.via_rules.blockSignals(False)
        self._fill_rho_table()
        self.clear_results()
        self.spin_via_limit.blockSignals(True)
        self.spin_via_limit.setValue((setup.via_current_limit_a or 0.0) * 1e3)
        self.spin_via_limit.blockSignals(False)
        t = setup.thermal
        for wdg, val in ((self.spin_ambient, t.ambient_c), (self.spin_iters, t.iterations),
                         (self.spin_airflow, t.airflow_m_s), (self.spin_tgrid, t.grid_mm),
                         (self.spin_lam_k, t.laminate_k_w_mk),
                         (self.chk_thermal, t.enabled), (self.chk_report, setup.html_report)):
            wdg.blockSignals(True)
            (wdg.setChecked if isinstance(wdg, QCheckBox) else wdg.setValue)(val)
            wdg.blockSignals(False)
        self._thermal_changed()
        self.setEnabled(True)
        self._refresh_pickers()
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.reload()
        self._on_changed()

    def set_nets(self, nets: List[str]):
        """Nets offered in the pin pickers (the ones ticked in the Board panel)."""
        if self.snapshot is None:
            return
        order = {n: i for i, n in enumerate(sorted_nets(self.snapshot))}
        nets = sorted(set(nets), key=lambda n: order.get(n, len(order)))
        if nets == self._nets:
            return
        self._nets = nets
        self._refresh_pickers()
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.reload()

    def _refresh_pickers(self):
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.editor.refresh_nets(self._nets)

    def _on_changed(self):
        errors = validate(self.setup) if (self.setup.vrms or self.setup.loads) else []
        if not self.setup.vrms:
            errors.insert(0, "Add a VRM (the source of a net) to start")
        self.lbl_errors.setText("\n".join(f"• {e}" for e in errors))
        self.btn_solve.setEnabled(not errors)

    # ------------------------------------------------------------------ new items
    def _first_net(self) -> Optional[str]:
        if not self._nets:
            self.lbl_errors.setText("• Tick nets in the Board panel first")
            return None
        return self._nets[0]

    def _first_part(self, net) -> str:
        parts = parts_on_net(self.snapshot, net)
        return parts[0][0] if parts else ""

    def _new_vrm(self):
        net = self._first_net()
        if net is None:
            return None
        name = _unique("VRM1", [x.name for x in self.setup.vrms])
        return Vrm(name, PinRef(self._first_part(net), net), guess_voltage(net), "source")

    def _new_load(self):
        net = self._first_net()
        if net is None:
            return None
        ref = self._first_part(net)
        return Load(_unique(ref or "LOAD1", [x.name for x in self.setup.loads]),
                    PinRef(ref, net), 0.1)

    def _new_link(self):
        net = self._first_net()
        if net is None:
            return None
        other = self._nets[1] if len(self._nets) > 1 else net
        ref, ref_b = self._first_part(net), self._first_part(other)
        # Prefer a part that bridges the two nets, e.g. a sense resistor between them.
        on_other = {r for r, _, _ in parts_on_net(self.snapshot, other)} if other != net else set()
        bridge = next((r for r, _, _ in parts_on_net(self.snapshot, net) if r in on_other), None)
        if bridge:
            ref = ref_b = bridge
        ohms = resistor_ohms(self.snapshot, ref) if bridge else None
        return DcLink(_unique(ref or "R1", [x.name for x in self.setup.links]),
                      PinRef(ref, net), PinRef(ref_b, other), ohms or 0.0)

    @staticmethod
    def _describe_vrm(v: Vrm) -> str:
        src = "" if v.kind == "source" else f"  ← {v.input.net if v.input else '?'}"
        kind = v.kind if not v.phases else f"{v.kind}, {len(v.phases) + 1} phases"
        return f"{v.name}: {v.output.net} {v.voltage:g} V ({kind}){src}"

    @staticmethod
    def _describe_load(l: Load) -> str:
        return f"{l.name}: {l.current:g} A from {l.supply.net} ({l.supply.label()})"

    @staticmethod
    def _describe_link(k: DcLink) -> str:
        r = "short" if k.resistance <= 0 else f"{k.resistance * 1e3:g} mΩ"
        return f"{k.name}: {k.side_a.net} ↔ {k.side_b.net} ({r})"

    # ------------------------------------------------------------------ results
    def set_busy(self, busy: bool, queued: bool = False):
        self.btn_solve.setEnabled(not busy and not self.lbl_errors.text())
        idle = "Solve all (electro-thermal)" if self.setup.thermal.enabled else "Solve all"
        self.btn_solve.setText(("Queued…" if queued else "Solving…") if busy else idle)

    def current_field(self) -> str:
        return self.cmb_field.currentData()

    def clear_results(self):
        self._tree = None
        if hasattr(self, "btn_export"):
            self.btn_export.setEnabled(False)
        for t in (self.tbl_loads, self.tbl_vrms, self.tbl_links, self.tbl_vias, self.tbl_losses):
            t.setRowCount(0)
        self.txt_notes.clear()
        self.cmb_group.blockSignals(True)
        self.cmb_group.clear()
        self.cmb_group.blockSignals(False)

    def show_error(self, message: str):
        self.res_tabs.setCurrentWidget(self.txt_notes)
        self.txt_notes.setHtml(f'<span style="color:#ff7070"><b>Cannot solve:</b><br>'
                               f'{html.escape(message).replace(chr(10), "<br>")}</span>')

    def show_results(self, tree, initial_group: str):
        for t in (self.tbl_loads, self.tbl_vrms, self.tbl_links):
            reset_sort(t)                         # new results show in their natural order
        t = self.tbl_loads
        t.setRowCount(len(tree.loads))
        for r, l in enumerate(tree.loads):
            status = l.verdict
            # Drop and boost sort as one signed value: largest boost ... largest drop.
            set_row(t, r, [l.name, l.group, f"{l.current:.4g}", f"{l.v_effective:.5f}",
                           (fmt_drop(l.drop, unit=""), l.drop * 1e3),
                           (fmt_drop(l.drop_worst, unit=""), l.drop_worst * 1e3),
                           (f"{abs(l.drop_worst_pct):.3f}", l.drop_worst_pct),
                           "" if l.tolerance_pct is None else f"±{l.tolerance_pct:g}", status])
            color = (("#ffb040" if l.status else None) if l.passed is None
                     else ("#60d060" if l.passed else "#ff6060"))
            if color:
                t.item(r, 8).setForeground(QColor(color))
        f = lambda x, fmt: "" if x is None else format(x, fmt)
        t = self.tbl_vrms
        t.setRowCount(sum(1 + len(v.phases) for v in tree.vrms))
        r = 0
        for v in tree.vrms:                       # each VRM, then a row per phase under it
            kind = v.kind if not v.phases else f"{v.kind}, {len(v.phases)} phases"
            status = "" if v.passed is None else ("PASS" if v.passed else "FAIL")
            set_row(t, r, [v.name, kind, f"{v.v_out:.5f}", f"{v.i_out:.4g}", f(v.v_in, ".5f"),
                           f(v.i_in, ".4g"), f(v.p_in, ".4g"), f(v.p_loss, ".4g"),
                           f(v.current_limit_a, "g"), status])
            if v.passed is not None:
                t.item(r, 9).setForeground(QColor("#60d060" if v.passed else "#ff6060"))
            parent = r
            for p in v.phases:
                r += 1
                set_row(t, r, [f"    phase {p.index}", "", f"{p.v_out:.5f}", f"{p.i_out:.4g}",
                               f(p.v_in, ".5f"), f(p.i_in, ".4g"), f(p.p_in, ".4g"),
                               f(p.p_loss, ".4g")], parent_row=parent, sub=p.index)
                for c in range(t.columnCount()):
                    t.item(r, c).setForeground(QColor("#a0a0a0"))
            r += 1
        rows = [(g.name, s) for g in tree.groups.values() for s in g.result.fields.series]
        self.tbl_links.setRowCount(len(rows))
        for r, (gname, s) in enumerate(rows):
            cur = "" if math.isnan(s.current) else f"{s.current:.4g}"
            set_row(self.tbl_links, r, [s.name, gname, f"{s.resistance * 1e3:g}", cur,
                                        f"{s.power * 1e3:.3f}"])
        notes = [f"Solved {len(tree.groups)} net group(s) in {tree.solve_s:.2f} s:"]
        th = getattr(tree, "thermal", None)
        if th is not None:
            x, y, tmax = th.hottest()
            thermal_notes = [
                f"<b>Electro-thermal:</b> {th.heat_w:.3g} W into the board, peak "
                f"<b>{tmax:.1f} °C</b> at ({x:.1f}, {y:.1f}) mm, ambient "
                f"{th.settings.ambient_c:g} °C, airflow {th.settings.airflow_m_s:g} m/s"]
            if getattr(th, "heat_by_kind", None):
                thermal_notes.append("&nbsp;&nbsp;heat by source: "
                                     + html.escape(th.heat_breakdown()))
            thermal_notes.append("&nbsp;&nbsp;iterations: " + ", ".join(
                f"{h['iteration']}: {h['t_max']:.2f} °C (Δ {h['dt_max']:.3f})" for h in th.history))
            thermal_notes.append("&nbsp;&nbsp;dielectrics: " + "; ".join(
                f"{html.escape(m or '(unnamed)')} {t:.3f} mm → {html.escape(i.matched)}, "
                f"{i.k_w_mk:g} W/m·K{' ⚠ assumed' if i.assumed else ''}"
                for m, t, i in th.dielectrics))
            notes = thermal_notes + notes
        for g in tree.groups.values():
            role = f"driven by {g.driver}" if g.kind == "rail" else f"ground, referenced at {g.driver}"
            notes.append(f"&nbsp;&nbsp;<b>{html.escape(g.name)}</b> ({role}): "
                         f"{html.escape(', '.join(g.nets))}")
        for l in tree.loads:
            if l.pin_groups:
                notes.append(f"<b>{html.escape(l.name)}</b> pin groups:")
                for name, amps, vmean, vmin, vmax in l.pin_groups:
                    span = (f"lowest pin {vmin:.5f} V" if vmax is None
                            else f"pins {vmin:.5f} to {vmax:.5f} V")
                    notes.append(f"&nbsp;&nbsp;{html.escape(name)}: {amps:.4g} A, "
                                 f"{vmean:.5f} V, {fmt_drop(l.nominal - vmean)} ({span})")
        notes += [f"⚠ {html.escape(w)}" for w in tree.warnings]
        self.txt_notes.setHtml("<br>".join(notes))
        self._set_temperature_field(getattr(tree, "thermal", None) is not None)
        self.cmb_group.blockSignals(True)
        self.cmb_group.clear()
        self.cmb_group.addItems(list(tree.groups))
        self.cmb_group.setCurrentText(initial_group)
        self.cmb_group.blockSignals(False)
        self._tree = tree
        self._fill_vias()
        self._fill_losses()
        self.btn_export.setEnabled(True)
        self.res_tabs.setCurrentWidget(self.tbl_loads)

    def _row_to_group(self, row, _col):
        it = self.tbl_loads.item(row, 1)
        if it:
            self.cmb_group.setCurrentText(it.text())


@contextmanager
def _bulk_fill(t: QTableWidget):
    """Fill a table with its columns' resize-to-contents paused.

    With it on, Qt re-measures every row each time one cell changes, which made a few
    thousand cell updates on a visible table take about a minute.
    """
    header = t.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    try:
        yield
    finally:
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
