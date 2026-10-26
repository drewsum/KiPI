# Portions Copyright (c) 2026 Tomachie LLC.
"""Power-integrity setup panel: VRMs, loads and series links, all user-defined.

Each tab is a list of items with an editor form. Edits go straight into the
:class:`~kipi.dc.pi.model.PISetup`, which is validated after every change. The Solve tab
picks which net groups are solved; only those are validated.

The VRM and series link editors are shared with the AC panel (:mod:`kipi.ac.gui.panel`):
both analyses use the same VRMs and links, and each editor grays out the fields that do
not apply to its analysis (``mode``).
"""

from __future__ import annotations

import copy
import html
import math
import re
from contextlib import contextmanager
from functools import partial
from typing import Callable, Dict, List, Optional

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QColor
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTabWidget,
    QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from kipi.core.model.board import BoardSnapshot
from kipi.core.model.nets import guess_voltage, sorted_nets, voltage_from_name
from kipi.core.model.parts import bead_esl_h, is_capacitor_ref, part_esl, resistor_ohms
from kipi.dc.pi.autofill import (
    guess_ground_pins, guess_input_ground, guess_input_pins, guess_other_side, looks_like_ground,
    next_phase_refs, other_net, part_nets, setup_grounds,
)
from kipi.dc.pi.load_model import apply_to_load, new_items
from kipi.dc.pi.model import (
    DEFAULT_VIA_CURRENT_LIMIT_A, DELTA_VOLTS, PERCENT, VOLTS, DcLink, Load, PinRef, PISetup,
    SensePoint, Vrm, VrmPhase, active_setup, group_name, net_groups, range_unit, range_volts, rail_nominal,
    validate, volts_in,
)
from kipi.gui.load_model_dialogs import (
    ApplyDialog, PartsDialog, candidate_parts, export_model, model_button, open_model,
)
from kipi.gui.report_option import ReportOption
from kipi.gui.sortable import make_sortable, reset_sort, set_row, sorted_rows
from kipi.gui.spice_dialog import LINK, import_spice, part_pins, part_values, spice_text
from kipi.gui.tooltip_art import add_text, art_tip
from kipi.gui.unit_spin import UnitSpinBox
from kipi.dc.pi import vunits
from kipi.dc.pi.tree import fmt_drop
from kipi.dc.viz.results import FIELDS, scale_decimals

NONE = "(none)"


def _spin(lo, hi, value, decimals, suffix, step=None) -> QDoubleSpinBox:
    """A value field in ``suffix`` units that also takes SI prefixes ("10n" in µF)."""
    s = UnitSpinBox()
    s.setShownDecimals(decimals)
    s.setRange(lo, hi)
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


class RangeRow(QWidget):
    """Optional min and max on one row: in volts, or in volts or % from a nominal voltage
    that ``nominal()`` gives (None if unknown, #131). Switching the unit converts the
    values, or clears them where that needs the nominal voltage and it is unknown."""
    changed = Signal()

    def __init__(self, nominal: Callable[[], Optional[float]], parent=None,
                 units=(VOLTS, DELTA_VOLTS, PERCENT)):
        super().__init__(parent)
        self._nominal = nominal
        self._quiet = False
        self.min = _optional_volts()
        self.max = _optional_volts()
        self.unit = QComboBox()
        for u in units:
            self.unit.addItem(u, u)
        self._unit = units[0]                   # the unit the values are in
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel("min"))
        h.addWidget(self.min, 1)
        h.addWidget(QLabel("max"))
        h.addWidget(self.max, 1)
        h.addWidget(self.unit)
        for s in (self.min, self.max):
            s.valueChanged.connect(self._changed)
        self.unit.currentIndexChanged.connect(self._unit_changed)

    def fields(self):
        return self.min, self.max, self.unit

    def pct(self) -> bool:
        return self.unit.currentData() == PERCENT

    def dv(self) -> bool:
        """Whether the values are volts from nominal."""
        return self.unit.currentData() == DELTA_VOLTS

    def values(self):
        return _get_optional(self.min), _get_optional(self.max)

    def set_values(self, lo: Optional[float], hi: Optional[float], pct: bool,
                   dv: bool = False):
        quiet, self._quiet = self._quiet, True
        try:
            self._unit = range_unit(pct, dv)
            if self.unit.findData(self._unit) < 0:      # a unit this row does not offer
                self._unit, lo, hi = self.unit.itemData(0), None, None
            pct = self._unit == PERCENT
            self.unit.setCurrentIndex(self.unit.findData(self._unit))
            step = {PERCENT: 1.0, DELTA_VOLTS: 0.01}.get(self._unit, 0.1)
            for s in (self.min, self.max):
                s.setSingleStep(step)
                s.setRange(-1000 - step, 1000)          # one step below: "not set"
                s.setSuffix(" %" if pct else " V")
                s.setShownDecimals(2 if pct else 3)
            _set_optional(self.min, lo)
            _set_optional(self.max, hi)
        finally:
            self._quiet = quiet

    def _unit_changed(self, *_):
        if self._quiet:
            return
        unit = self.unit.currentData()
        nom = self._nominal()
        lo, hi = range_volts(nom, *self.values(), self._unit)      # from the old unit
        lo, hi = volts_in(nom, lo, hi, unit)
        self.set_values(lo, hi, unit == PERCENT, unit == DELTA_VOLTS)
        self.changed.emit()

    def _changed(self, *_):
        if not self._quiet:
            self.changed.emit()


def _get_optional(s: QDoubleSpinBox) -> Optional[float]:
    return None if s.value() <= s.minimum() else s.value()


def _set_optional(s: QDoubleSpinBox, value: Optional[float]):
    s.setValue(s.minimum() if value is None else value)


def tip(form: QFormLayout, field: QWidget, text: str, art: Optional[tuple] = None):
    """Give a form row's field and its label the tooltip ``text``, with the drawing
    ``art`` (a name and the parts to pick out, #116) under it if given."""
    shown = art_tip(text, *art) if art else text
    if isinstance(field, PinPicker):
        field.art = art or ()               # it adds its boxes' lines above the drawing
        field.setToolTip(text)
    else:
        field.setToolTip(shown)
    label = form.labelForField(field)
    if label is not None:
        label.setToolTip(shown)


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


def _load_heat(th) -> str:
    """' (of which x W from loads)' when the solve heated the board with the loads (#62)."""
    loads = getattr(th, "load_heat_w", {}) or {}
    if not getattr(th.settings, "load_power", False):
        return ""
    return f" (of which {sum(max(v, 0.0) for v in loads.values()):.3g} W from loads)"


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
        self.art: tuple = ()                # a drawing under the tooltip (#116), see tip()
        self.setToolTip("")

    # What each of the three boxes picks, added under the row's own tooltip.
    _PART_TIPS = ("Net: one of the nets ticked in the Board panel (type to search)",
                  "Part: a part with pads on that net, as reference, value and pad count",
                  "Pins: pin numbers, e.g. 1,2,5. Blank uses every pin of the part on "
                  "the net.")

    def setToolTip(self, text: str):
        """The row's tooltip, shown on each box with a line on what that box picks, and
        the drawing ``art`` under it if set."""
        drawn = text.startswith("<table")      # already has it (_gray adds a line to it)
        shown = art_tip(text, *self.art) if self.art and text and not drawn else text
        super().setToolTip(shown)
        for w, line in zip((self.cmb_net, self.cmb_part, self.txt_pins), self._PART_TIPS):
            w.setToolTip(add_text(shown, line))

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
        self._follow_part()
        self.changed.emit()

    def _part_changed(self, _):
        self._update_hint()
        if not self._loading:
            self._follow_part()
            self.changed.emit()

    def _follow_part(self):
        """Typed pins that are not all the picked part's pins on the net were the last
        part's: they become this part's pins on the net. Blank (every pin) stays blank."""
        typed = [t for t in re.split(r"[,\s]+", self.txt_pins.text().strip()) if t]
        ref = self.cmb_part.currentData()
        if not typed or not ref or self.panel.snapshot is None:
            return
        pins = next((p for r, _, p in parts_on_net(self.panel.snapshot, self.net())
                     if r == ref), [])
        if pins and not set(typed) <= set(pins):
            self.txt_pins.setText(",".join(pins))

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

    # Guesses from the board that pre-fill fields (kipi.dc.pi.autofill). They only ever
    # replace a value the user hasn't chosen, and set nothing when they can't tell.
    def _guess_ground(self, supply: Optional[PinRef], prefer=()) -> Optional[PinRef]:
        p = self.panel
        return guess_ground_pins(p.snapshot, p.setup, p._nets, supply, prefer)

    def _show(self, picker: PinPicker, ref: Optional[PinRef]):
        """Show a guessed value in ``picker`` without saving it back a second time."""
        loading, self._loading = self._loading, True
        try:
            picker.set_value(ref)
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

    def _legend(self, unused: bool = False):
        """A line over the fields saying how their labels show what a solve needs (#136);
        ``unused``: some fields are only another analysis's."""
        text = NEED_LEGEND + (NEED_LEGEND_UNUSED if unused else "")
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #999;")
        self.form.insertRow(0, lbl)
        self.lbl_legend = lbl

    def _mark(self, needs: Dict[QWidget, str]):
        mark_needs(self.form, needs)


# What a solve needs from a field, shown on its label (#136): bold if the solve cannot run
# without it, gray if it is optional (blank, or a default that can be left), gray italic if
# only the other analysis uses it (it stays editable).
REQUIRED, OPTIONAL, UNUSED = "required", "optional", "unused"
NEED_STYLES = {REQUIRED: "font-weight: bold;", OPTIONAL: "color: #8c8c8c;",
               UNUSED: "color: #6f6f6f; font-style: italic;"}
NEED_LEGEND = "Bold fields are needed to solve; gray ones are optional."
NEED_LEGEND_UNUSED = " Gray italic ones are used by the other analysis only."


def mark_needs(form: QFormLayout, needs: Dict[QWidget, str]):
    """Style the labels of ``form``'s rows by what a solve needs from each field."""
    for w, need in needs.items():
        label = form.labelForField(w)
        if label is not None:
            label.setStyleSheet(NEED_STYLES[need])
            label.setProperty("kipi_need", need)


def _gray(widgets, on: bool, why: str, editable: bool = False):
    """Enable ``widgets`` or gray them out, saying why in their tooltip. ``editable``: they
    stay editable, and only the tooltip says the analysis doesn't use them."""
    for w in widgets:
        w.setEnabled(on or editable)
        tip = w.property("kipi_tip")
        if tip is None:
            tip = w.toolTip()
            w.setProperty("kipi_tip", tip)
        w.setToolTip(tip if on else add_text(tip, why))


class VrmEditor(_Editor):
    """``mode`` "dc" or "ac": the analysis this editor is shown in. Every field is shown
    and editable; the tooltips of the ones only the other analysis uses say so."""

    def __init__(self, panel, parent=None, mode: str = "dc"):
        super().__init__(panel, parent)
        self.mode = mode
        f = self.form
        self.name = QLineEdit()
        self.kind = QComboBox()
        self.kind.addItem("Source (ideal supply, no input)", "source")
        self.kind.addItem("LDO (I_in = I_out + I_q)", "ldo")
        self.kind.addItem("Switcher (P_in = P_out / η)", "smps")
        self.kind.addItem("Isolated (P_in = P_out / η, own output ground)", "isolated")
        self.output = self._picker()
        self.voltage = _spin(-1000, 1000, 3.3, 4, " V", 0.1)
        self.eff = _spin(1, 100, 90, 1, " %", 1)
        self.iq = _spin(0, 10000, 0, 3, " mA", 0.1)
        self.i_limit = _spin(0, 10000, 0, 3, " A", 0.1)
        self.i_limit.setSpecialValueText("no limit")
        self.load_line = _spin(0, 1e6, 0, 3, " mΩ", 0.1)
        self.load_line.setSpecialValueText("none")
        self.dropout = _spin(0, 100, 0, 3, " V", 0.05)
        # Input range, in volts or in % from the input rail's nominal voltage.
        self.vin_row = RangeRow(self._input_nominal)
        # DC accuracy, in % of the target voltage or in volts.
        self.acc_row = RangeRow(lambda: self.voltage.value() or None)
        self.btn_accuracy_calc = QPushButton("DC Accuracy Calculator…")
        self.btn_accuracy_calc.setToolTip(
            "Calculate an adjustable regulator's output range from its feedback reference, "
            "resistor divider and FB pin leakage")
        self.btn_accuracy_calc.clicked.connect(self._accuracy_calculator)
        self.input = self._picker(optional=True)
        self.sense = self._picker(optional=True)
        self.ground = self._picker(optional=True)
        self.input_ground = self._picker(optional=True)
        self.ground_sense = self._picker(optional=True)
        # Mesh remote sense (#93): the sense pickers show the selected point.
        self.mesh = QCheckBox("Enable mesh remote sense")
        self.n_points = QSpinBox()
        self.n_points.setRange(1, 64)
        self.cmb_point = QComboBox()
        self._point = 0
        # Multi-phase: the output, input and ground pickers show the selected phase.
        self.n_phases = QSpinBox()
        self.n_phases.setRange(1, 32)
        self.cmb_phase = QComboBox()
        self._phase = 0
        self.bandwidth = _spin(0, 1e6, 0, 2, " kHz", 1)
        self.bandwidth.setSpecialValueText("none")
        f.addRow("Name", self.name)
        f.addRow("Type", self.kind)
        f.addRow("Target voltage", self.voltage)
        f.addRow("Efficiency", self.eff)
        f.addRow("Quiescent", self.iq)
        f.addRow("Output current limit", self.i_limit)
        f.addRow("Load line", self.load_line)
        f.addRow("Dropout", self.dropout)
        f.addRow("Input range", self.vin_row)
        f.addRow("DC accuracy", self.acc_row)
        f.addRow("", self.btn_accuracy_calc)
        f.addRow("Control loop bandwidth", self.bandwidth)
        f.addRow("Phases", self.n_phases)
        f.addRow("Edit phase", self.cmb_phase)
        f.addRow("Output pins", self.output)
        f.addRow("Input pins", self.input)
        f.addRow("Ground pins", self.ground)
        f.addRow("Input ground pins", self.input_ground)
        f.addRow("Mesh sense", self.mesh)
        f.addRow("Sense points", self.n_points)
        f.addRow("Edit sense point", self.cmb_point)
        f.addRow("Sense pins", self.sense)
        f.addRow("Ground sense pins", self.ground_sense)
        # Drawings under the tooltips (#116), the field's part picked out.
        art = {self.kind: ("vrm_types",), self.eff: ("vrm_types", "smps", "iso"),
               self.iq: ("vrm_types", "ldo"), self.load_line: ("load_line", "ll"),
               self.dropout: ("dropout", "dropout"), self.vin_row: ("dropout", "range"),
               self.vin_row.min: ("dropout", "range"), self.vin_row.max: ("dropout", "range"),
               self.acc_row: ("accuracy", "min", "max"), self.acc_row.min: ("accuracy", "min"),
               self.acc_row.max: ("accuracy", "max"), self.bandwidth: ("bandwidth", "bw"),
               self.n_phases: ("phases", "phase"), self.sense: ("remote_sense", "sense"),
               self.ground_sense: ("remote_sense", "gsense"),
               self.mesh: ("remote_sense", "sense", "gsense"),
               self.n_points: ("remote_sense", "sense"),
               self.vin_row.unit: ("tolerance", "volts", "dv", "pct"),
               self.acc_row.unit: ("tolerance", "volts", "dv", "pct"),
               self.output: ("vrm_pins", "out"), self.input: ("vrm_pins", "inp"),
               self.ground: ("vrm_pins", "gnd"),
               self.input_ground: ("isolated", "ignd")}
        for w, text in (
                (self.name, "A name for this regulator, used in the results tables"),
                (self.kind, "Source: an ideal supply with no input (a connector, a bench "
                            "supply).\nLDO: input current = output current + quiescent "
                            "current.\nSwitcher: input power = output power / efficiency."
                            "\nIsolated: a switcher whose output side has a ground of its "
                            "own; its output ground pins are that ground's 0 V reference "
                            "and its input current returns through its input ground pins. "
                            "Not modeled in Coupled Noise."),
                (self.voltage, "The voltage the VRM regulates to, at its output pins or at "
                               "its sense pins if it has them; loads and results are judged "
                               "against it. Picking the output net fills it in from the "
                               "net's name (3V3, +1.8V, V1P2, -5V...)."),
                (self.eff, "Switcher efficiency: input power = output power / efficiency"),
                (self.iq, "Quiescent current, drawn from the input on top of the load "
                          "current"),
                (self.i_limit, "PASS if the VRM's total output current (all phases) is at "
                               "most this"),
                (self.load_line, "Load line (adaptive voltage positioning): the VRM's "
                                 "target voltage droops by this resistance times its output "
                                 "current, i.e. the current its connected loads and the "
                                 "VRMs it feeds actually draw. Drops are still measured "
                                 "from the target voltage, so the droop counts against each "
                                 "load's tolerance. 'none' holds the target voltage."),
                (self.dropout, "LDO dropout: the least input-to-output difference it "
                               "regulates at. Below it, its loads are marked VRM OUT OF "
                               "REGULATION."),
                (self.vin_row, "Optional input range it regulates over. A solved input "
                               "outside it marks its loads VRM OUT OF REGULATION."),
                (self.vin_row.min, "Optional lowest input voltage it regulates at. A lower "
                                   "solved input marks its loads VRM OUT OF REGULATION."),
                (self.vin_row.max, "Optional highest input voltage it regulates at. A higher "
                                   "solved input marks its loads VRM OUT OF REGULATION."),
                (self.vin_row.unit, "V: the input range in volts.\n±V: in volts below and "
                                    "above the input rail's nominal voltage, the target voltage "
                                    "of the VRM driving it (e.g. min -0.5 V, max +0.5 V).\n"
                                    "%: in % from that nominal voltage (e.g. min -10 %, max "
                                    "+10 %).\nSwitching converts the values when that VRM "
                                    "is known."),
                (self.acc_row, "Optional DC accuracy of the target voltage (also called DC "
                               "error or DC tolerance), from the datasheet, or from the DC "
                               "Accuracy Calculator. It comes from the regulator's reference "
                               "accuracy, the feedback resistor tolerances, leakage into the "
                               "FB pin, and line and load regulation. The target calculator "
                               "can take it off the AC voltage tolerance."),
                (self.acc_row.min, "How far below the target voltage the output may sit, e.g. "
                                   "-1 % or -0.01 V (±V), or in V, the lowest output voltage"),
                (self.acc_row.max, "How far above the target voltage the output may sit, e.g. "
                                   "+1 % or +0.01 V (±V), or in V, the highest output "
                                   "voltage"),
                (self.acc_row.unit, "%: the accuracy in % of the target voltage.\n±V: in "
                                    "volts below and above it.\nV: the lowest and "
                                    "highest output voltage.\nSwitching converts the "
                                    "values."),
                (self.bandwidth, "Control loop bandwidth of the converter (AC analysis), "
                                 "also called the loop gain crossover, gain crossover or "
                                 "control loop crossover frequency. It depends on the total "
                                 "output capacitance and the loop tuning. The AC solve "
                                 "leaves the VRM out of circuit (the open-loop PDN "
                                 "impedance); below this frequency the loop holds the rail, "
                                 "so probe targets are drawn and judged only above it. "
                                 "Required to solve the rail it drives in AC mode."),
                (self.n_phases, "Parallel phases of a multi-phase regulator. Each carries an "
                                "equal share of the output current and has its own output, "
                                "input and ground pins. A new phase takes the next parts in "
                                "sequence (L1 → L2, Q1 → Q2), or the same parts on the next "
                                "schematic sheet (L101 → L201), where the board has them."),
                (self.cmb_phase, "The phase whose output, input and ground pins are shown "
                                 "below"),
                (self.output, "Where the VRM drives its rail: e.g. its output pin, or a "
                              "switcher's inductor"),
                (self.input, "Where the VRM draws its input current from. This ties it to its "
                             "upstream net and builds the power tree. Filled in from the "
                             "output part when it has just one other rail (an LDO, say)."),
                (self.ground, "Optional: the VRM's ground pins. With them, the ground return "
                              "is solved too and drops are measured output to ground. Picking "
                              "the output pins fills in the same part's ground pins."),
                (self.input_ground, "Optional, isolated converters only: the input "
                                    "side's ground pins, where its input current returns to "
                                    "the VRM feeding it. Its ground pins are then its output "
                                    "side's: they hold that ground at 0 V, the reference of "
                                    "everything grounded on it."),
                (self.sense, "Optional remote sense: the VRM holds its target voltage at these "
                             "pins instead of at its output"),
                (self.ground_sense, "Optional negative remote sense on the ground net: the "
                                    "target voltage is held relative to these pins instead of "
                                    "0 V (needs ground pins)"),
                (self.mesh, "Mesh remote sense, a niche model, off by default: any number "
                            "of sense points, each with its sense pins and optional ground "
                            "sense pins. Every point's sense pins are held at the target "
                            "voltage (less the load line), above its own ground sense pins; "
                            "the VRM's current enters there and its output pins only "
                            "measure."),
                (self.n_points, "How many mesh sense points; the sense pickers below show "
                                "the one picked in Edit sense point"),
                (self.cmb_point, "The mesh sense point whose sense and ground sense pins "
                                 "are shown below")):
            tip(f, w, text, art.get(w))
        self._watch(self.name, self.kind, self.voltage, self.eff, self.iq, self.i_limit,
                    self.load_line, self.dropout, self.bandwidth)
        self.vin_row.changed.connect(self._store)
        self.acc_row.changed.connect(self._store)
        self.kind.currentIndexChanged.connect(self._kind_rows)
        self.n_phases.valueChanged.connect(self._phase_count_changed)
        self.cmb_phase.currentIndexChanged.connect(self._phase_selected)
        self.mesh.toggled.connect(self._point_count_changed)
        self.n_points.valueChanged.connect(self._point_count_changed)
        self.cmb_point.currentIndexChanged.connect(self._point_selected)
        self.output.cmb_net.currentIndexChanged.connect(
            lambda _i: self._guess_voltage(self.output.net()))
        self._legend(unused=True)
        self._kind_rows()

    def _kind_rows(self, *_):
        kind = self.kind.currentData()
        isolated = kind == "isolated"
        self.form.setRowVisible(self.eff, kind in ("smps", "isolated"))
        self.form.setRowVisible(self.input_ground, isolated)
        self.form.setRowVisible(self.n_phases, not isolated or self.n_phases.value() > 1)
        self._phase_rows()
        self.form.setRowVisible(self.iq, kind != "source")
        self.form.setRowVisible(self.input, kind != "source")
        self.form.setRowVisible(self.dropout, kind == "ldo")
        self.form.setRowVisible(self.vin_row, kind != "source")
        self.form.setRowVisible(self.acc_row, kind != "source")
        self.form.setRowVisible(self.btn_accuracy_calc, kind != "source")
        dc = self.mode == "dc"
        dc_only = (self.kind, self.eff, self.iq, self.i_limit, self.load_line, self.n_phases,
                   self.input, self.sense, self.ground_sense, self.dropout, self.mesh,
                   self.n_points)
        ac_only = (self.bandwidth, self.acc_row)
        _gray((*dc_only, *self.vin_row.fields()),
              dc, "Used by the DC analysis only", editable=True)
        _gray((self.bandwidth, *self.acc_row.fields()), not dc, "Used by the AC analysis only",
              editable=True)
        needs = {w: OPTIONAL for w in (self.iq, self.i_limit, self.load_line, self.dropout,
                                       self.vin_row, self.n_phases, self.ground,
                                       self.input_ground, self.sense,
                                       self.ground_sense, self.mesh, self.n_points,
                                       *ac_only)}
        needs.update({w: REQUIRED for w in (self.name, self.kind, self.voltage, self.output)})
        needs[self.eff] = REQUIRED              # a switcher's input current follows from it
        needs[self.input] = REQUIRED if kind != "source" else OPTIONAL   # ties it into the tree
        needs.update({w: UNUSED for w in (ac_only if dc else (*dc_only, self.vin_row))})
        if not dc:
            needs[self.bandwidth] = REQUIRED    # the AC targets apply above it (#183)
        self.bandwidth.setSpecialValueText("none" if dc else "required")
        self._mark(needs)

    def _input_nominal(self) -> Optional[float]:
        inp = self.input.value()
        return rail_nominal(self.panel.setup, inp.net if inp else None,
                            self.obj.name if self.obj else "")

    def _guess_voltage(self, net):
        """Picking the output net sets the voltage its name gives (3V3, +1.8V, V1P2...)."""
        volts = voltage_from_name(net)
        if not self._loading and volts is not None and self._phase == 0:
            self.voltage.setValue(volts)

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
        ground = ("Output ground pins" if self.kind.currentData() == "isolated"
                  else "Ground pins")
        for w, text in ((self.output, "Output pins"), (self.input, "Input pins"),
                        (self.ground, ground)):
            self.form.labelForField(w).setText(text + tag)

    def _show_phase(self, v: Vrm):
        ph = v.all_phases()[self._phase]
        loading, self._loading = self._loading, True
        self.output.set_value(ph.output)
        self.input.set_value(ph.input)
        self.ground.set_value(ph.ground)
        self.input_ground.set_value(v.input_ground)
        self._loading = loading

    def _point_rows(self):
        """Fill the sense point selector and label the sense pickers."""
        mesh = self.mesh.isChecked()
        n = self.n_points.value() if mesh else 1
        self._point = min(self._point, n - 1)
        self.cmb_point.blockSignals(True)
        self.cmb_point.clear()
        self.cmb_point.addItems([f"Point {k}" for k in range(1, n + 1)])
        self.cmb_point.setCurrentIndex(self._point)
        self.cmb_point.blockSignals(False)
        self.form.setRowVisible(self.n_points, mesh)
        self.form.setRowVisible(self.cmb_point, n > 1)
        tag = f" (point {self._point + 1})" if n > 1 else ""
        for w, text in ((self.sense, "Sense pins"), (self.ground_sense, "Ground sense pins")):
            self.form.labelForField(w).setText(text + tag)

    @staticmethod
    def _points(v: Vrm) -> List[SensePoint]:
        return [SensePoint(v.sense, v.ground_sense), *v.sense_points]

    def _show_point(self, v: Vrm):
        pts = self._points(v)
        sp = pts[self._point] if self._point < len(pts) else SensePoint()
        loading, self._loading = self._loading, True
        self.sense.set_value(sp.sense)
        self.ground_sense.set_value(sp.ground_sense)
        self._loading = loading

    def _point_count_changed(self, *_):
        if self._loading or self.obj is None:
            return
        self._store()
        self._point_rows()
        self._show_point(self.obj)

    def _point_selected(self, index):
        if self._loading or self.obj is None or index < 0:
            return
        self._point = index
        self._point_rows()
        self._show_point(self.obj)

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
        self.load_line.setValue(v.load_line_mohm)
        self.bandwidth.setValue(v.bandwidth_khz or 0.0)
        self.n_phases.setValue(len(v.phases) + 1)
        self._phase = 0
        self._phase_rows()
        self._show_phase(v)
        self.dropout.setValue(v.dropout_v)
        self.vin_row.set_values(v.vin_min, v.vin_max, v.vin_pct, v.vin_dv)
        self.acc_row.set_values(v.acc_min, v.acc_max, v.acc_pct, v.acc_dv)
        self.mesh.setChecked(v.mesh_sense)
        self.n_points.setValue(len(v.sense_points) + 1)
        self._point = 0
        self._point_rows()
        self._show_point(v)
        self._kind_rows()

    def _accuracy_calculator(self):
        from kipi.dc.gui.accuracy_calc import DcAccuracyCalculator

        dlg = DcAccuracyCalculator(self, self.obj.name if self.obj else "")
        if dlg.exec() != QDialog.Accepted:
            return
        result = dlg.result_data()
        if result is None:
            return
        # Record the range as absolute output voltages; the target voltage is left alone.
        self.acc_row.set_values(result.minimum_v, result.maximum_v, False, False)
        self._store()

    def _save(self, v: Vrm):
        typed = self.name.text().strip() or v.name
        # A name still equal to the output part's reference follows a newly picked part.
        new_out = self.output.value() if self._phase == 0 else None
        if new_out is not None and typed == v.output.ref and new_out.ref != v.output.ref:
            typed = new_out.ref
            self.name.setText(typed)
        v.name = typed
        v.kind = self.kind.currentData()
        v.voltage = self.voltage.value()
        v.efficiency = self.eff.value() / 100
        v.quiescent_a = self.iq.value() / 1e3
        v.current_limit_a = self.i_limit.value() or None
        v.load_line_mohm = self.load_line.value()
        v.dropout_v = self.dropout.value()
        v.vin_min, v.vin_max = self.vin_row.values()
        v.vin_pct, v.vin_dv = self.vin_row.pct(), self.vin_row.dv()
        v.acc_min, v.acc_max = self.acc_row.values()
        v.acc_pct, v.acc_dv = self.acc_row.pct(), self.acc_row.dv()
        v.bandwidth_khz = self.bandwidth.value() or None
        v.mesh_sense = self.mesh.isChecked()
        pts = self._points(v)
        while len(pts) < max(self.n_points.value(), self._point + 1):
            pts.append(SensePoint())
        pts[self._point] = SensePoint(self.sense.value(), self.ground_sense.value())
        del pts[max(self.n_points.value(), 1):]
        v.sense, v.ground_sense = pts[0].sense, pts[0].ground_sense
        v.sense_points = pts[1:]
        phases = v.all_phases()
        ph = phases[min(self._phase, len(phases) - 1)]
        old = ph.output
        ph.output = self.output.value() or ph.output
        ph.input = self.input.value()
        ph.ground = self.ground.value()
        self._autofill_phase(v, ph, old)
        # A new phase repeats the last one on the parts the next phase likely uses: the
        # same step on as between the last two, the same parts on the next schematic sheet
        # (L101 -> L201), or the next in sequence (L1 -> L2); otherwise on the same parts,
        # for the user to change.
        n = self.n_phases.value()
        snap = self.panel.snapshot
        roles = ("output", "input", "ground")
        while len(phases) < n:
            phases.append(VrmPhase(*next_phase_refs(
                snap, [[getattr(q, r) for r in roles] for q in phases])))
        del phases[n:]
        for ph in phases:
            if v.kind == "source":
                ph.input = None
        v.output, v.input, v.ground = phases[0].output, phases[0].input, phases[0].ground
        v.phases = phases[1:]
        v.input_ground = self.input_ground.value() if v.kind == "isolated" else None
        if v.kind == "isolated" and v.input_ground is None:
            v.input_ground = guess_input_ground(snap, self.panel.setup, self.panel._nets,
                                                v.output, v.ground, v.input)
            if v.input_ground is not None:
                self._show(self.input_ground, v.input_ground)

    def _autofill_phase(self, v: Vrm, ph: VrmPhase, old: PinRef):
        """Pick the ground and input pins that go with newly picked output pins: the same
        part's pins on its ground net and (for an LDO or switcher) its input rail. Pins
        the user picked on another part are kept, and so is anything with no guess."""
        old_ref = old.ref
        moved = (ph.output.ref, ph.output.net) != (old.ref, old.net)
        if moved and (ph.ground is None or ph.ground.ref == old_ref):
            gnd = self._guess_ground(ph.output)
            if gnd is not None and gnd != ph.ground:
                ph.ground = gnd
                self._show(self.ground, gnd)
        if v.kind != "source" and (ph.input is None or (moved and ph.input.ref == old_ref)):
            p = self.panel
            vin = guess_input_pins(p.snapshot, p._nets, ph.output, ph.ground)
            if vin is not None and vin != ph.input:
                ph.input = vin
                self._show(self.input, vin)


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
        self.btn_group.setToolTip("Add a group of supply pins with its own current")
        self.btn_group.clicked.connect(
            lambda: (self._add_group_row("", "", 0.0), reset_sort(self.groups), self._store()))
        self.btn_each = QPushButton("One group per pin")
        self.btn_each.setToolTip("Replace the groups with one group per supply pin")
        self.btn_each.clicked.connect(self._group_per_pin)
        gb = QHBoxLayout()
        gb.addWidget(self.btn_group)
        gb.addWidget(self.btn_each)
        self.lbl_total = QLabel()
        self.ground = self._picker(optional=True)
        # Tolerance, in % or volts from the rail's target voltage, or in volts.
        self.tol = RangeRow(self._rail_nominal)
        # Optional DC and AC shares of it (#174), saved for later use. AC is a swing about
        # the DC voltage, so only from nominal.
        self.dc_tol = RangeRow(self._rail_nominal)
        self.ac_tol = RangeRow(self._rail_nominal, units=(DELTA_VOLTS, PERCENT))
        f.addRow("Name", self.name)
        f.addRow("Supply pins", self.supply)
        f.addRow("Current split", self.mode)
        f.addRow("Current", self.current)
        f.addRow(self.groups)
        f.addRow(gb)
        f.addRow("Total", self.lbl_total)
        f.addRow("Ground pins", self.ground)
        f.addRow("Tolerance", self.tol)
        f.addRow("DC tolerance", self.dc_tol)
        f.addRow("AC tolerance", self.ac_tol)
        # Drawings under the tooltips (#116), the field's part picked out.
        art = {self.mode: ("current_split",), self.current: ("current_split", "equal"),
               self.tol: ("tolerance", "min", "max"), self.tol.min: ("tolerance", "min"),
               self.tol.max: ("tolerance", "max"),
               self.tol.unit: ("tolerance", "volts", "dv", "pct")}
        for row, part in ((self.dc_tol, "dc"), (self.ac_tol, "ac")):
            for w in (row, *row.fields()):
                art[w] = ("tol_split", part)
        for w, text in (
                (self.name, "A name for this load, used in the results tables"),
                (self.supply, "The pins the load draws its current from"),
                (self.mode, "Split the current evenly over all supply pins, or give pin "
                            "groups, each with its own current (e.g. a core and an I/O "
                            "supply)"),
                (self.current, "Total current the load draws, shared evenly by its supply "
                               "pins"),
                (self.lbl_total, "Sum of the pin groups' currents"),
                (self.ground, "Optional: the load's ground pins, to include the ground "
                              "return in the solve. Picking the supply pins fills in the same "
                              "part's ground pins."),
                (self.tol, "Optional: PASS if every supply pin is within this range, so no "
                           "pin drops below the min or is boosted above the max"),
                (self.tol.min, "The lowest voltage a supply pin may have, or in ±V or %, "
                               "the most it may drop below the VRM target voltage (e.g. "
                               "-0.15 V or -5 %)"),
                (self.tol.max, "The highest voltage a supply pin may have, or in ±V or %, "
                               "the most it may be boosted above the VRM target voltage (e.g. "
                               "+0.15 V or +5 %)"),
                (self.tol.unit, "V: the lowest and highest voltage, in volts.\n±V: in volts "
                                "below and above the target voltage of the VRM driving "
                                "the rail.\n%: in % from that target voltage."
                                "\nSwitching converts the values when that VRM is known.")):
            tip(f, w, text, art.get(w))
        dc_text = ("Optional, for a part that gives its tolerance in two shares: the DC "
                   "share, where the pin's steady voltage may sit, from the VRM's accuracy "
                   "and the copper's DC drop together. In V, or ±V or % from the VRM "
                   "target voltage. Saved with the setup; no solve uses it yet.")
        ac_text = ("Optional, for a part that gives its tolerance in two shares: the AC "
                   "share, how far ripple and load transients may swing the pin about its "
                   "steady voltage. In ±V or % from it. Saved with the setup; the AC target "
                   "calculator can copy it, but no solve uses it.")
        for row, text in ((self.dc_tol, dc_text), (self.ac_tol, ac_text)):
            for w in (row, *row.fields()):
                tip(f, w, text, art[w])
        self.groups.setToolTip(art_tip(
            "Pin groups: an optional name, the pin numbers (e.g. 15,28) and the current into "
            "those pins. Click a column header to sort by it.", "current_split", "groups"))
        make_sortable(self.groups, self._sort_groups, columns=(0, 1, 2))
        self._watch(self.name, self.current, self.mode)
        for row in (self.tol, self.dc_tol, self.ac_tol):
            row.changed.connect(self._store)
        self.mode.currentIndexChanged.connect(self._mode_rows)
        self._gb_widgets = (self.btn_group, self.btn_each)
        self._legend()
        self._mode_rows()

    def _mode_rows(self, *_):
        grouped = self.mode.currentData() == "groups"
        self._mark({self.name: REQUIRED, self.supply: REQUIRED, self.mode: REQUIRED,
                    self.current: REQUIRED, self.ground: OPTIONAL, self.tol: OPTIONAL,
                    self.dc_tol: OPTIONAL, self.ac_tol: OPTIONAL})
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
        btn.setToolTip("Remove this pin group")
        btn.clicked.connect(lambda _=False, b=btn: self._remove_group_of(b))
        self.groups.setCellWidget(r, 3, btn)

    def _remove_group_of(self, widget):
        for r in range(self.groups.rowCount()):
            if self.groups.cellWidget(r, 3) is widget:
                self.groups.removeRow(r)
                self._store()
                return

    def _sort_groups(self, col: int, descending: bool):
        """Rebuild the pin group rows ordered by name, pins or current."""
        rows = [[g.name, ",".join(g.pins), g.current] for g in self._groups_value()]
        rows = sorted_rows(rows, col, descending)
        self.groups.setRowCount(0)
        for name, pins, current in rows:
            self._add_group_row(name, pins, current)
        self._store()

    def _supply_pins(self) -> List[str]:
        ref = self.supply.value()
        if ref is None or self.panel.snapshot is None:
            return []
        return ref.pins or next((p for r, _, p in parts_on_net(self.panel.snapshot, ref.net)
                                 if r == ref.ref), [])

    def _fill_per_pin(self, total: float):
        pins = self._supply_pins()
        self.groups.setRowCount(0)
        reset_sort(self.groups)
        for pin in pins:
            self._add_group_row(f"pin {pin}", pin, total / max(1, len(pins)))

    def _group_per_pin(self):
        current = self._groups_value()
        total = sum(g.current for g in current) if current else self.current.value()
        self._fill_per_pin(total)
        self._store()

    def _groups_value(self):
        from kipi.dc.pi.model import PinGroup
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
        reset_sort(self.groups)
        for g in l.groups:
            self._add_group_row(g.name, ",".join(g.pins), g.current)
        self.mode.setCurrentIndex(1 if l.groups else 0)
        self.ground.set_value(l.ground)
        self.tol.set_values(l.tol_min, l.tol_max, l.tol_pct, l.tol_dv)
        self.dc_tol.set_values(l.dc_tol_min, l.dc_tol_max, l.dc_tol_pct, l.dc_tol_dv)
        self.ac_tol.set_values(l.ac_tol_min, l.ac_tol_max, l.ac_tol_pct, l.ac_tol_dv)
        self.lbl_total.setText(f"{l.total_current():.4g} A")
        self._mode_rows()

    def _rail_nominal(self) -> Optional[float]:
        sup = self.supply.value()
        return rail_nominal(self.panel.setup, sup.net if sup else None)

    def _save(self, l: Load):
        old = l.supply
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
        # A new supply part brings its own ground pins, unless the user picked another
        # part's (see kipi.dc.pi.autofill.ground_net for how the ground net is found).
        if ((new_supply.ref, new_supply.net) != (old.ref, old.net)
                and (l.ground is None or l.ground.ref == old.ref)):
            gnd = self._guess_ground(new_supply)
            if gnd is not None:
                l.ground = gnd
                self._show(self.ground, gnd)
        l.tol_min, l.tol_max = self.tol.values()
        l.tol_pct, l.tol_dv = self.tol.pct(), self.tol.dv()
        l.dc_tol_min, l.dc_tol_max = self.dc_tol.values()
        l.dc_tol_pct, l.dc_tol_dv = self.dc_tol.pct(), self.dc_tol.dv()
        l.ac_tol_min, l.ac_tol_max = self.ac_tol.values()
        l.ac_tol_pct, l.ac_tol_dv = self.ac_tol.pct(), self.ac_tol.dv()
        self.lbl_total.setText(f"{l.total_current():.4g} A")


class LinkEditor(_Editor):
    def __init__(self, panel, parent=None, mode: str = "dc"):
        super().__init__(panel, parent)
        self.mode = mode
        f = self.form
        self.name = QLineEdit()
        self.a = self._picker()
        self.b = self._picker()
        self.r = _spin(0, 1e9, 0, 3, " mΩ", 1)
        self.r.setSpecialValueText("0 (short)")
        self.esl = _spin(0, 1e6, 0, 3, " nH", 0.1)
        self.esl.setSpecialValueText("none")
        f.addRow("Name", self.name)
        f.addRow("Side A pins", self.a)
        f.addRow("Side B pins", self.b)
        f.addRow("Resistance (DCR)", self.r)
        f.addRow("ESL", self.esl)
        # Estimates of the ESL (#159): from the part's package, or a ferrite bead's
        # impedance.
        self.btn_esl_package = QPushButton("From package", self)
        self.btn_esl_package.clicked.connect(self._esl_from_package)
        self.btn_esl_bead = QPushButton("Ferrite bead…", self)
        self.btn_esl_bead.clicked.connect(self._esl_from_bead)
        est = QWidget(self)
        row = QHBoxLayout(est)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.btn_esl_package)
        row.addWidget(self.btn_esl_bead)
        f.addRow("Estimate ESL", est)
        # Or both values from the part's SPICE model (#51).
        self.btn_spice = QPushButton("Import SPICE model…", self)
        self.btn_spice.clicked.connect(self._from_spice)
        f.addRow("SPICE model", self.btn_spice)
        self.chk_spice = QCheckBox("Use the SPICE model's impedance")
        self.chk_spice.toggled.connect(self._spice_toggled)
        f.addRow(self.chk_spice)
        self.lbl_spice = QLabel()                   # which model, and whether it is used
        self.lbl_spice.setWordWrap(True)
        self.lbl_spice.setStyleSheet("color: #999;")
        f.addRow(self.lbl_spice)
        self.lbl_esl = QLabel()                     # where the last estimate came from
        self.lbl_esl.setWordWrap(True)
        self.lbl_esl.setStyleSheet("color: #999;")
        self.lbl_esl.setVisible(False)
        f.addRow(self.lbl_esl)
        # Drawings under the tooltips (#116): the part as a DCR and an ESL in series.
        art = {self.a: ("series_link", "a"), self.b: ("series_link", "b"),
               self.r: ("series_link", "r"), self.esl: ("series_link", "l"),
               self.btn_esl_package: ("series_link", "l"), self.btn_esl_bead: ("bead_z", "l"),
               self.btn_spice: ("series_link", "r", "l")}
        for w, text in (
                (self.name, "A name for this part, used in the results tables"),
                (self.a, "The part's pins on one net"),
                (self.b, "The part's pins on the other net. Filled in when side A is on a "
                         "part with pins on just two nets (a resistor, ferrite, fuse...)."),
                (self.r, "DC resistance (DCR), used by both analyses. For a resistor, it is "
                         "read from the part's value when both sides are on it."),
                (self.esl, "Series inductance (ESL) of the part"),
                (self.btn_esl_package,
                 "Estimate the ESL from the size of the part on side A: its footprint's "
                 "name (R_0603_1608Metric), else how far apart its two pads are. About "
                 "0.1 nH + 0.3 nH per mm between the terminals, a line through Vishay's "
                 "measured 0402 and 0603 chip resistors (0.4 and 0.6 nH): 0805 0.7 nH, "
                 "1206 1.1 nH, 2512 2 nH. For a chip resistor, fuse or jumper; a ferrite "
                 "bead's ferrite makes it far more, so use its impedance."),
                (self.btn_esl_bead,
                 "Work out the ESL of a ferrite bead from its impedance at one frequency, "
                 "read off its datasheet's curve. Below its peak a bead is an inductor, "
                 "|Z| = √(DCR² + (2πf·L)²); at and above the peak it is a resistor, so a "
                 "point well below the peak gives the inductance best."),
                (self.btn_spice,
                 "Use the part's SPICE model, a .SUBCKT of resistors, inductors and "
                 "capacitors from its maker: a ferrite bead's, say, with its resistive peak. "
                 "It is saved with the setup, and the AC and Coupled Noise solves take the "
                 "part's impedance from it at every frequency. The DCR (which the DC solve "
                 "uses) is the model's resistance at DC, and the ESL its inductance at "
                 "100 kHz, low on a bead's inductive slope, for the tables and reports.\n\n"
                 "A diode is its .model D card (IS, N, RS, BV, IBV, CJO, VJ, M, TT) or a "
                 ".SUBCKT with diodes in it; side A is its anode (the model's first pin). "
                 "The DC solve takes its drop from the model at the current it carries. AC "
                 "and Coupled Noise use its small-signal resistance and capacitance at the "
                 "current the DC loads beyond it draw. The DCR and ESL shown are its slope "
                 "resistance and inductance at 1 A.\n\n"
                 "A part with more than two pins (a common-mode choke, a feedthrough "
                 "capacitor) is a .SUBCKT with a pin per side, coupled inductors (K) "
                 "allowed. Map each of its pins to one of side A's part's pins in the "
                 "dialog: each becomes a side of the link. DC takes its conductance "
                 "between the sides, AC and Coupled Noise its admittance at every "
                 "frequency.\n\n"
                 "The model is worked out at the copper's temperature under the part when "
                 "the solve knows it (TEMP, TC1 and TC2, and a diode's temperature "
                 "parameters), else at 27 °C."),
                (self.chk_spice,
                 "Take the impedance from the imported SPICE model. Untick to use the DCR "
                 "and ESL as you enter them; the model is kept, and ticking again puts its "
                 "fitted values back.")):
            tip(f, w, text, art.get(w))
        hint = QLabel("A part in series between two nets (sense resistor, ferrite, fuse, "
                      "load switch or diode). For a diode, import its SPICE model, with "
                      "side A its anode.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        f.addRow(hint)
        self._watch(self.name, self.r, self.esl)
        _gray((self.esl, self.btn_esl_package, self.btn_esl_bead), mode == "ac",
              "Used by the AC analysis only")
        self._legend(unused=mode == "dc")
        self._mark({self.name: REQUIRED, self.a: REQUIRED, self.b: REQUIRED, self.r: REQUIRED,
                    self.esl: OPTIONAL if mode == "ac" else UNUSED})

    def _esl_from_package(self):
        k = self.obj
        if k is None:
            return
        est = part_esl(self.panel.snapshot, k.side_a.ref)
        if est is None:
            QMessageBox.information(
                self, "Estimate ESL",
                f"The size of {k.side_a.ref or 'the part on side A'} cannot be told: its "
                "footprint's name gives no chip size (such as 0603) and it has not two "
                "pads. Enter the ESL from its datasheet.")
            return
        henries, how = est
        self.esl.setValue(henries * 1e9)            # saved through valueChanged
        self._estimated(f"ESL estimated from {k.side_a.ref}'s {how}: {henries * 1e9:.2g} nH")

    def _esl_from_bead(self):
        k = self.obj
        if k is None:
            return
        dlg = BeadDialog(k.resistance, self)
        if dlg.exec() == QDialog.Accepted and dlg.esl_h is not None:
            self.esl.setValue(dlg.esl_h * 1e9)
            self._estimated(f"ESL from the bead's {dlg.z.value():g} Ω at "
                            f"{dlg.freq.value():g} MHz: {dlg.esl_h * 1e9:.3g} nH")

    def _from_spice(self):
        k = self.obj
        if k is None:
            return
        refs = dict.fromkeys(r.ref for r in (k.side_a, k.side_b) if r is not None)
        # A multi-pin model (#193) maps its pins to side A's part's pins, its sides.
        part = k.side_a.ref if k.side_a is not None else ""
        pins = part_pins(self.panel.snapshot, part)
        got = import_spice(self, LINK, [k.name, *part_values(self.panel.snapshot, refs)],
                           pins)
        if got is None:
            return
        k.spice, _values, mapped = got
        if mapped:
            net = dict(pins)
            sides = [PinRef(part, net[n], [n]) for n in mapped]
            k.side_a, k.side_b, k.more_sides = sides[0], sides[1], sides[2:]
        else:
            k.more_sides = []
        self._fitted(k)

    def _spice_toggled(self, on: bool):
        k = self.obj
        if self._loading or k is None or k.spice is None:
            return
        k.spice.use = on
        if on:
            self._fitted(k)
        else:
            self._spice_fields(k)
            self.changed.emit()

    def _fitted(self, k: DcLink):
        """Put the values fitted to ``k``'s SPICE model in it and in the fields."""
        k.resistance = k.spice.values.get("resistance", k.resistance)
        k.esl_h = k.spice.values.get("esl_h")
        self.load(k)
        self.changed.emit()

    def _spice_fields(self, k: DcLink):
        """The SPICE model's line, and the DCR and ESL locked while it is in use."""
        self.chk_spice.setVisible(k.spice is not None)
        self.lbl_spice.setVisible(k.spice is not None)
        self.chk_spice.setChecked(k.uses_spice)
        text = spice_text(k.spice) if k.spice else ""
        if k.more_sides:                            # a multi-pin part's sides (#193)
            text += (" Its pins: " + ", ".join(f"{chr(65 + i)} {p.label()} · {p.net}"
                                              for i, p in enumerate(
                                                  [k.side_a, k.side_b, *k.more_sides]))
                     + ("." if k.uses_spice else "; only A and B while it is not in use."))
        self.lbl_spice.setText(text)
        free = not k.uses_spice
        self.r.setEnabled(free)
        for w in (self.esl, self.btn_esl_package, self.btn_esl_bead):
            w.setEnabled(free and self.mode == "ac")

    def _estimated(self, text: str):
        self.lbl_esl.setText(text)
        self.lbl_esl.setVisible(True)

    def _load(self, k: DcLink):
        self.lbl_esl.setVisible(False)
        self._spice_fields(k)
        self.name.setText(k.name)
        self.a.set_value(k.side_a)
        self.b.set_value(k.side_b)
        self.r.setValue(k.resistance * 1e3)
        self.esl.setValue((k.esl_h or 0.0) * 1e9)

    def _save(self, k: DcLink):
        old_a = k.side_a
        new_a = self.a.value() or k.side_a
        typed = self.name.text().strip() or k.name
        if typed == k.side_a.ref and new_a.ref != k.side_a.ref:
            typed = new_a.ref
            self.name.setText(typed)
        parts = (k.side_a.ref, k.side_b.ref)
        k.name = typed
        k.side_a = new_a
        k.side_b = self.b.value() or k.side_b
        # Side A on a two-terminal part (one with pads on just two nets) puts side B on
        # the same part's other net.
        if (new_a.ref, new_a.net) != (old_a.ref, old_a.net):
            other = guess_other_side(self.panel.snapshot, new_a, setup_grounds(self.panel.setup))
            if other is not None and (other.ref, other.net) != (k.side_b.ref, k.side_b.net):
                k.side_b = other
                self._show(self.b, other)
        k.resistance = self.r.value() / 1e3
        k.esl_h = self.esl.value() * 1e-9 or None
        # Both sides just became the same resistor: take its resistance from its value.
        if (k.side_a.ref, k.side_b.ref) != parts and k.side_a.ref == k.side_b.ref:
            ohms = resistor_ohms(self.panel.snapshot, k.side_a.ref)
            if ohms is not None:
                k.resistance = ohms
                self.r.blockSignals(True)
                self.r.setValue(ohms * 1e3)
                self.r.blockSignals(False)


class BeadDialog(QDialog):
    """A ferrite bead's ESL from its impedance at one frequency (#159)."""

    def __init__(self, dcr_ohm: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ferrite bead ESL")
        self.dcr = dcr_ohm
        self.esl_h: Optional[float] = None
        lay = QVBoxLayout(self)
        hint = QLabel("Read the bead's impedance at one frequency off its datasheet's |Z| "
                      "curve. Pick a point well below the curve's peak, where it still "
                      "climbs in a straight line: there the bead is an inductor. At and "
                      "above the peak its ferrite is lossy and the bead a resistor, so a "
                      "point there (such as the 100 MHz headline value of many beads) "
                      "overstates the inductance.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        f = QFormLayout()
        self.z = _spin(0.001, 1e5, 120, 3, " Ω", 10)
        self.freq = _spin(0.001, 1e4, 10, 3, " MHz", 1)
        self.lbl = QLabel()
        f.addRow("Impedance |Z|", self.z)
        f.addRow("At frequency", self.freq)
        f.addRow("ESL", self.lbl)
        lay.addLayout(f)
        art = art_tip("The bead's |Z| on its inductive slope: L = √(|Z|² − DCR²) / 2πf, "
                      f"with the link's DCR ({dcr_ohm * 1e3:.3g} mΩ).", "bead_z", "l")
        for w in (self.z, self.freq):
            w.setToolTip(art)
            w.valueChanged.connect(self._update)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._update()

    def _update(self, *_):
        self.esl_h = bead_esl_h(self.z.value(), self.freq.value() * 1e6, self.dcr)
        ok = self.esl_h is not None
        self.lbl.setText(f"{self.esl_h * 1e9:.4g} nH" if ok
                         else "|Z| must be more than the DCR")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)


class ItemList(QWidget):
    """A list of setup items plus the editor for the selected one."""
    changed = Signal()
    #: The item now selected (or None), and whether the user picked it in the list.
    selected = Signal(object, bool)

    def __init__(self, items: Callable[[], list], editor: _Editor, make_new: Callable[[], object],
                 describe: Callable[[object], str], parent=None, noun: str = "item"):
        super().__init__(parent)
        self.items, self.editor, self.make_new, self.describe = items, editor, make_new, describe
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.lst = QListWidget(self)
        self.lst.currentRowChanged.connect(self._select)
        self.lst.itemClicked.connect(lambda _: self.selected.emit(self.current(), True))
        self.lst.setToolTip(f"Select a {noun} to edit it below")
        #: The Add / Duplicate / Remove row, for panels that add buttons of their own.
        self.buttons = row = QHBoxLayout()
        for text, slot, what in (("Add", self._add, f"Add a new {noun}"),
                                 ("Duplicate", self._dup, f"Add a copy of the selected {noun}"),
                                 ("Remove", self._remove, f"Delete the selected {noun}")):
            b = QPushButton(text, self)
            b.setToolTip(what)
            b.clicked.connect(slot)
            row.addWidget(b)
        split = QSplitter(Qt.Vertical, self)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.lst)
        tl.addLayout(row)
        split.addWidget(top)
        # The editor scrolls rather than setting the window's minimum height: a VRM's form
        # alone is taller than a 1080p screen leaves once the rest of the window is laid out.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(editor)
        split.addWidget(scroll)
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

    def current(self):
        """The selected item, or None."""
        row, items = self.lst.currentRow(), self.items()
        return items[row] if 0 <= row < len(items) else None

    def select_name(self, name: str) -> bool:
        """Select the item called ``name``; False if there is none."""
        names = [getattr(x, "name", None) for x in self.items()]
        if name not in names:
            return False
        self.lst.setCurrentRow(names.index(name))
        return True

    def _select(self, row):
        items = self.items()
        if 0 <= row < len(items):
            self.editor.setEnabled(True)
            self.editor.load(items[row])
        else:
            self.editor.setEnabled(False)
            self.editor.obj = None
        # Arrow keys in the list count as the user's pick; a reload does not.
        self.selected.emit(self.current(), self.lst.hasFocus())

    def _edited(self):
        row = self.lst.currentRow()
        if 0 <= row < len(self.items()):
            self.lst.item(row).setText(self.describe(self.items()[row]))
        self.changed.emit()
        self.selected.emit(self.current(), False)     # its name may have changed

    def _add(self):
        self.add(self.make_new())

    def add(self, obj):
        """Append ``obj`` (None: nothing) and select it."""
        if obj is None:
            return
        self.items().append(obj)
        self.reload()
        self.lst.setCurrentRow(self.lst.count() - 1)
        self.changed.emit()

    def _dup(self):
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


def new_vrm(snap: BoardSnapshot, setup: PISetup, nets: List[str], net: str) -> Vrm:
    """A new VRM on ``net``'s busiest part, named after it, with its voltage read from the
    net's name and the part's ground pins if it has any."""
    parts = parts_on_net(snap, net)
    out = PinRef(parts[0][0] if parts else "", net)
    return Vrm(_unique(out.ref or "VRM1", [x.name for x in setup.vrms]), out,
               guess_voltage(net), "source",
               ground=guess_ground_pins(snap, setup, nets, out))


def new_link(snap: BoardSnapshot, setup: PISetup, nets: List[str], net: str) -> DcLink:
    """A new series link from ``net``: on a part that bridges it to the next ticked net
    that isn't a ground (a sense resistor between them, say), else on its busiest part.
    A two-terminal part gets its other net as side B, and a resistor its value as the
    resistance."""
    grounds = setup_grounds(setup)
    others = [n for n in nets if n != net]
    other = next((n for n in others if not looks_like_ground(snap, n, grounds)),
                 others[0] if others else net)
    on_net = parts_on_net(snap, net)
    ref = on_net[0][0] if on_net else ""
    on_other = {r for r, _, _ in parts_on_net(snap, other)} if other != net else set()
    # A two-terminal part (a sense resistor, say) is likelier a series part than an IC,
    # and a capacitor is never one.
    bridges = [r for r, _, _ in on_net if r in on_other and not is_capacitor_ref(r)]
    bridge = next((r for r in bridges if other_net(snap, r, net) == other),
                  bridges[0] if bridges else None)
    a = PinRef(bridge or ref, net)
    b = PinRef(bridge, other) if bridge else guess_other_side(snap, a, grounds)
    if b is None:
        b_parts = parts_on_net(snap, other)
        b = PinRef(b_parts[0][0] if b_parts else "", other)
    ohms = resistor_ohms(snap, a.ref) if a.ref == b.ref else None
    return DcLink(_unique(a.ref or "R1", [x.name for x in setup.links]), a, b, ohms or 0.0)


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

class PartSelection:
    """The setup part selected in a panel's lists, for the 3D view's PI Models markers
    (#96). The panel has ``tabs``, a ``part_selected`` signal and ``_part_lists()``: the
    marker kind of each tab that lists parts, and its :class:`ItemList`."""

    def _watch_part_lists(self):
        for il in self._part_lists().values():
            il.selected.connect(lambda _obj, user, il=il: self._part_selection(user)
                                if self.tabs.currentWidget() is il else None)
        self.tabs.currentChanged.connect(lambda _: self._part_selection(False))

    def _part_selection(self, by_user: bool):
        """Say which part the shown list has selected; none on the other tabs."""
        kind = next((k for k, il in self._part_lists().items()
                     if il is self.tabs.currentWidget()), "")
        obj = self._part_lists()[kind].current() if kind else None
        self.part_selected.emit(kind if obj is not None else "",
                                obj.name if obj is not None else "", by_user)

    def select_part(self, kind: str, name: str) -> bool:
        """Show the part ``name`` of marker kind ``kind`` in its list and editor (#96);
        False if this panel lists no such part."""
        il = self._part_lists().get(kind)
        if il is None or name not in [x.name for x in il.items()]:
            return False
        self.tabs.setCurrentWidget(il)
        il.select_name(name)
        self._part_selection(False)
        return True


class PIPanel(QWidget, PartSelection):
    solve_requested = Signal(object)      # PISetup
    field_changed = Signal(str)
    scale_changed = Signal(object)         # (min, max) of the color scale, or None: auto
    group_changed = Signal(str)
    show_geometry = Signal()
    arrows_toggled = Signal(bool)
    via_selected = Signal(str, str)        # group name, via / PTH pad id
    # The VRM, load or series link selected in the setup: kind ("vrm", "load", "link"),
    # name, and whether the user picked it; ("", "", False) for none (#96).
    part_selected = Signal(str, str, bool)
    export_requested = Signal(str)         # "csv", "vtk" or "results"
    report_requested = Signal(str)         # a report of the shown results, "html" or "pdf"
    setup_changed = Signal()               # any edit to the setup

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot: Optional[BoardSnapshot] = None
        self.setup = PISetup()
        self._nets: List[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget(self)
        self.vrm_list = ItemList(lambda: self.setup.vrms, VrmEditor(self), self._new_vrm,
                                 self._describe_vrm, noun="VRM")
        self.load_list = ItemList(lambda: self.setup.loads, LoadEditor(self), self._new_load,
                                  self._describe_load, noun="load")
        self.link_list = ItemList(lambda: self.setup.links, LinkEditor(self), self._new_link,
                                  self._describe_link, noun="series link")
        # Load models (#17): a part's supplies in a file, for many loads at once.
        self.load_list.buttons.addWidget(model_button(
            self, "load", self.apply_load_model, self.add_loads_from_model,
            lambda: export_model(self, self.setup, self.snapshot)))
        # One part's total current over several of its nets (#182).
        b_split = QPushButton("Split across nets…", self)
        b_split.setToolTip(art_tip(
            "Share one part's total current out over several of its supply nets, as a "
            "load on each: e.g. when only a chip's total draw is known and some of its "
            "pins are on a ferrite bead filtered net", "split_nets", "nets"))
        b_split.clicked.connect(lambda: self.split_load_across_nets())
        self.load_list.buttons.addWidget(b_split)
        self.tabs.addTab(self.vrm_list, "VRMs")
        self.tabs.addTab(self.load_list, "Loads")
        self.tabs.addTab(self.link_list, "Series links")
        self.tabs.addTab(self._solve_tab(), "Solve")
        self.tabs.addTab(self._settings_tab(), "Mesh")
        self.tabs.addTab(self._thermal_tab(), "Thermal")
        for i, text in enumerate((
                "Regulators and supplies: where each rail is driven, and from where",
                "Consumers: the current each part draws, and its voltage tolerance",
                "Parts in series between two nets (sense resistors, ferrites, fuses, load "
                "switches, 0 Ω links)",
                "Choose which net groups to solve",
                "Mesh density and via barrel plating",
                "Electro-thermal co-simulation: losses heat the board, hot copper "
                "resists more")):
            self.tabs.setTabToolTip(i, text)
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.changed.connect(self._on_changed)
        self._watch_part_lists()
        lay.addWidget(self.tabs, 3)

        self.lbl_errors = QLabel(self)
        self.lbl_errors.setWordWrap(True)
        self.lbl_errors.setStyleSheet("color: #ff8080;")
        lay.addWidget(self.lbl_errors)
        row = QHBoxLayout()
        self.btn_solve = QPushButton("Solve all", self)
        self.btn_solve.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_solve.setToolTip("Save the setup, then mesh and solve every ticked net group. "
                                  "Problems that stop the solve are listed above in red.")
        self.btn_solve.clicked.connect(lambda: self.solve_requested.emit(self.setup))
        self.report_option = ReportOption("3D view, per-layer maps and tables",
                                          self.report_requested.emit, self._report_changed,
                                          self)
        self.btn_report = QPushButton("Open report", self)
        self.btn_report.setToolTip("Open the last report in your browser or PDF viewer")
        self.btn_report.setVisible(False)
        self.btn_report.clicked.connect(self._open_report)
        self.report_path = None
        row.addWidget(self.btn_solve, 1)
        row.addWidget(self.report_option)
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
        self.cmb_field.setToolTip("Result shown on the copper in 3D")
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
        b_export.setToolTip("Export the tables as CSV, the 3D results for ParaView, or save the "
                            "results to reopen later")
        self.btn_export = b_export
        b_export.setEnabled(False)
        b_geom = QPushButton("Show copper", g_res)
        b_geom.setToolTip("Back to the plain copper view. Pick a net group or field to see the "
                          "results again.")
        b_geom.clicked.connect(self.show_geometry.emit)
        row.addWidget(self.cmb_group, 2)
        row.addWidget(self.cmb_field, 2)
        row.addWidget(self.chk_arrows)
        row.addWidget(b_geom)
        row.addWidget(b_export)
        lr.addLayout(row)
        lr.addLayout(self._scale_row(g_res))
        self.res_tabs = QTabWidget(g_res)
        # The results' voltages in volts, or in mV or % from the target voltage (#177).
        self.cmb_vunit = QComboBox(g_res)
        for unit, label in vunits.UNITS:
            self.cmb_vunit.addItem(label, unit)
        self.cmb_vunit.setToolTip(
            "Show the voltages in the Loads and VRMs tables in mV from the target voltage "
            "(the VRM's), in % of it from the target, or in volts. From the target, negative "
            "is a drop below it and positive a boost above it.")
        self.cmb_vunit.currentIndexChanged.connect(self._vunit_changed)
        self.res_tabs.setCornerWidget(self.cmb_vunit)
        self.tbl_loads = self._table(self._load_headers(), sortable=True)
        self.tbl_loads.setToolTip(
            "Each load's mean voltage, its lowest and highest supply pins (less the ground "
            "pins), its tolerance and its margin: how far its pins are inside the tolerance, "
            "negative when outside. A load passes when every pin is within its tolerance.")
        self.tbl_vrms = self._table(self._vrm_headers(), sortable=True)
        self.tbl_vrms.setToolTip("Each VRM's solved output and input, conversion loss, the "
                                 "lowest voltage anywhere on the copper of the rail it drives "
                                 "(the ground's own drop not included), and PASS/FAIL against "
                                 "its output current limit; a row per phase under a "
                                 "multi-phase VRM")
        self.tbl_links = self._table(["Part", "Group", "R (mΩ)", "I (A)", "P (mW)"],
                                      sortable=True)
        self.tbl_links.setToolTip("Current through, and power in, each series link")
        self.txt_notes = QTextBrowser(g_res)
        self.res_tabs.addTab(self.tbl_loads, "Loads")
        self.res_tabs.addTab(self.tbl_vrms, "VRMs")
        self.res_tabs.addTab(self.tbl_links, "Series links")
        self.res_tabs.addTab(self._vias_tab(), "Vias")
        self.tbl_losses = self._table(["Group"], sortable=True)
        self.tbl_losses.setToolTip("Power lost in the copper of each net group, by layer, in "
                                   "its vias and series links, and in VRM conversion")
        self.res_tabs.addTab(self.tbl_losses, "Losses")
        self.thermal_res = self._thermal_results_tab()   # shown after an electro-thermal solve
        self.res_tabs.addTab(self.txt_notes, "Notes")
        self.txt_notes.setToolTip("Solve details: the net groups, pin groups, warnings and "
                                  "the thermal iteration history")
        self.tbl_loads.cellDoubleClicked.connect(self._row_to_group)
        lr.addWidget(self.res_tabs)
        lay.addWidget(g_res, 2)
        self.setEnabled(False)

    # ------------------------------------------------------------------ widgets
    def set_report(self, path):
        self.report_path = path
        self.btn_report.setVisible(path is not None)

    def _report_changed(self, fmt: str):
        self.setup.report_format = fmt

    def _open_report(self):
        if self.report_path:
            from kipi.gui.browser import open_in_browser
            open_in_browser(self.report_path)

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

    # ------------------------------------------------------------ color scale
    def _scale_row(self, parent) -> QHBoxLayout:
        """The 3D color scale's minimum and maximum (#101). Each starts at the result's
        whole range; typed limits are kept per net group and field (temperature: per
        field, as every group shares the board's temperature) until Auto."""
        self._scale_limits: Dict[tuple, tuple] = {}
        self._scale_key = None
        row = QHBoxLayout()
        self.lbl_scale = QLabel("Color scale", parent)
        self.spin_scale_min = _spin(-1e15, 1e15, 0.0, 3, "")
        self.spin_scale_max = _spin(-1e15, 1e15, 1.0, 3, "")
        self.spin_scale_min.setToolTip("Lowest value of the 3D color scale: copper at or below "
                                       "it takes the bottom color")
        self.spin_scale_max.setToolTip("Highest value of the 3D color scale: copper at or above "
                                       "it takes the top color")
        for sp in (self.spin_scale_min, self.spin_scale_max):
            sp.valueChanged.connect(self._scale_edited)
        self.btn_scale_auto = QPushButton("Auto", parent)
        self.btn_scale_auto.setToolTip("Fit the color scale to the whole result again")
        self.btn_scale_auto.clicked.connect(self._scale_auto)
        row.addWidget(self.lbl_scale)
        row.addWidget(self.spin_scale_min, 1)
        row.addWidget(QLabel("to", parent))
        row.addWidget(self.spin_scale_max, 1)
        row.addWidget(self.btn_scale_auto)
        self._enable_scale(False)
        return row

    @staticmethod
    def _limits_key(group: str, field: str) -> tuple:
        return (None if field == "temperature" else group), field

    def color_limits(self, group: str, field: str) -> Optional[tuple]:
        """The color scale (min, max) typed for this group and field, or None for the
        result's whole range."""
        return self._scale_limits.get(self._limits_key(group, field))

    def show_scale(self, group: str, field: str, lo: float, hi: float):
        """Show the color range the 3D view draws ``field`` of ``group`` over."""
        self._scale_key = self._limits_key(group, field)
        unit = FIELDS[field][3]
        decimals = scale_decimals(lo, hi)
        for sp, v in ((self.spin_scale_min, lo), (self.spin_scale_max, hi)):
            sp.blockSignals(True)
            sp.setSuffix(f" {unit}")
            sp.setShownDecimals(decimals)
            sp.setSingleStep(max(abs(hi - lo) / 10, 10.0 ** -decimals))
            sp.setValue(v)
            sp.blockSignals(False)
        self._enable_scale(True)
        self.btn_scale_auto.setEnabled(self._scale_key in self._scale_limits)

    def _enable_scale(self, on: bool):
        for w in (self.lbl_scale, self.spin_scale_min, self.spin_scale_max,
                  self.btn_scale_auto):
            w.setEnabled(on)

    def _scale_edited(self, *_):
        if self._scale_key is None:
            return
        lo, hi = self.spin_scale_min.value(), self.spin_scale_max.value()
        if hi <= lo:
            return              # not a range yet: wait for the other end
        self._scale_limits[self._scale_key] = (lo, hi)
        self.btn_scale_auto.setEnabled(True)
        self.scale_changed.emit((lo, hi))

    def _scale_auto(self):
        if self._scale_limits.pop(self._scale_key, None) is not None:
            self.scale_changed.emit(None)
        self.btn_scale_auto.setEnabled(False)

    def _thermal_results_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.lbl_thermal = QLabel()
        self.lbl_thermal.setWordWrap(True)
        self.lbl_thermal.setTextFormat(Qt.RichText)
        v.addWidget(self.lbl_thermal)
        self.tbl_temps = self._table(["Layer", "Peak (°C)", "Rise (°C)", "x (mm)", "y (mm)",
                                      "Group", ""], sortable=True)
        self.tbl_temps.setToolTip("The hottest solved copper on each layer. Double-click a "
                                  "row to show its group's temperature.")
        self.tbl_temps.cellDoubleClicked.connect(self._temp_row_clicked)
        v.addWidget(self.tbl_temps, 1)
        self.lbl_thermal_details = QLabel()
        self.lbl_thermal_details.setWordWrap(True)
        self.lbl_thermal_details.setTextFormat(Qt.RichText)
        self.lbl_thermal_details.setStyleSheet("color: #999;")
        v.addWidget(self.lbl_thermal_details)
        return w

    def _show_thermal_tab(self, on: bool):
        """The Thermal results tab is there only after an electro-thermal solve."""
        i = self.res_tabs.indexOf(self.thermal_res)
        if on and i < 0:
            self.res_tabs.insertTab(self.res_tabs.indexOf(self.txt_notes), self.thermal_res,
                                    "Thermal")
        elif not on and i >= 0:
            self.res_tabs.removeTab(i)

    def _fill_thermal(self):
        from kipi.dc.pi.report import layer_temps, limits_text, thermal_passed
        th = getattr(self._tree, "thermal", None)
        self._show_thermal_tab(th is not None)
        if th is None:
            return
        limits = self.setup.thermal
        rows = layer_temps(self._tree, self.snapshot, limits)
        t = self.tbl_temps
        reset_sort(t)
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            status = "" if row.passed is None else ("PASS" if row.passed else "FAIL")
            set_row(t, r, [row.layer, (f"{row.t_max:.1f}", row.t_max),
                           (f"{row.rise:.1f}", row.rise), f"{row.x:.1f}", f"{row.y:.1f}",
                           row.group, status])
            if row.passed is not None:
                t.item(r, 6).setForeground(QColor("#60d060" if row.passed else "#ff6060"))
        x, y, tmax = th.hottest()
        ambient = th.settings.ambient_c
        verdict = thermal_passed(rows)
        if verdict is None:
            judged = "No temperature limits set (see the Thermal settings tab)."
        else:
            color = "#60d060" if verdict else "#ff6060"
            judged = (f'<span style="color:{color}"><b>{"PASS" if verdict else "FAIL"}</b>'
                      f"</span>: {html.escape(limits_text(limits))}.")
        self.lbl_thermal.setText(
            f"Peak copper temperature <b>{tmax:.1f} °C</b> ({tmax - ambient:+.1f} °C) at "
            f"({x:.1f}, {y:.1f}) mm{self._peak_layer(th)}; {th.heat_w:.3g} W into the board{_load_heat(th)}, "
            f"ambient {ambient:g} °C, airflow {th.settings.airflow_m_s:g} m/s.<br>{judged}")
        details = ["Iterations: " + ", ".join(
            f"{h['iteration']}: {h['t_max']:.2f} °C (Δ {h['dt_max']:.3f})" for h in th.history)]
        if th.dielectrics:
            details.append("Dielectrics: " + "; ".join(
                f"{html.escape(m or '(unnamed)')} {d:.3f} mm → {html.escape(i.matched)}, "
                f"{i.k_w_mk:g} W/m·K in-plane, {i.k_through:g} through"
                f"{' ⚠ assumed' if i.assumed else ''}"
                for m, d, i in th.dielectrics))
        self.lbl_thermal_details.setText("<br>".join(details))

    def _peak_layer(self, th) -> str:
        st = getattr(getattr(self, "snapshot", None), "stackup", None)
        return th.peak_layer(st) if st is not None else ""

    def _temp_row_clicked(self, row, _col):
        it = self.tbl_temps.item(row, 5)
        if it is None:
            return
        self.cmb_group.setCurrentText(it.text())
        i = self.cmb_field.findData("temperature")
        if i >= 0:
            self.cmb_field.setCurrentIndex(i)

    def _vias_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Current limit"))
        self.spin_via_limit = _spin(0, 1e6, DEFAULT_VIA_CURRENT_LIMIT_A * 1e3, 1, " mA", 10)
        self.spin_via_limit.setSpecialValueText("none")
        self.spin_via_limit.setToolTip("Vias carrying more than this are marked FAIL")
        self.spin_via_limit.valueChanged.connect(self._via_limit_changed)
        row.addWidget(self.spin_via_limit)
        row.addStretch(1)
        row.addWidget(QLabel("double-click a row to show it"))
        v.addLayout(row)
        self.tbl_vias = self._table(["Group", "Via / pad", "x (mm)", "y (mm)", "Drill (mm)",
                                     "I (mA)", "P (mW)", ""], sortable=True)
        self.tbl_vias.setToolTip("Every via and plated-hole barrel, highest current first. "
                                 "Double-click a row to fly to it in 3D.")
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
        from kipi.dc.pi.report import via_rows
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
        from kipi.dc.pi.report import loss_rows
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
        from kipi.dc.gui.via_rules_editor import ViaRulesEditor
        self.via_rules = ViaRulesEditor(w)
        self.via_rules.set_rules(s.via_rules)
        f.addRow("Max edge", self.spin_edge)
        f.addRow("Via grading", self.spin_grading)
        f.addRow(QLabel("<b>Via plating</b>"))
        f.addRow(self.via_rules)
        tip(f, self.spin_edge, art_tip("Largest triangle edge of the copper mesh. Smaller is "
                                       "more accurate and slower.", "mesh", "max"))
        tip(f, self.spin_grading, art_tip("How fast mesh elements grow away from plated holes "
                                          "(mm of edge per mm of distance). Lower keeps the "
                                          "mesh fine further out from vias; 0 turns grading "
                                          "off.", "mesh", "grading"))
        for sp in (self.spin_edge, self.spin_grading):
            sp.valueChanged.connect(self._settings_changed)
        self.via_rules.changed.connect(self._settings_changed)
        return w

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
        f.addRow(self.chk_thermal)
        f.addRow("Ambient", self.spin_ambient)
        f.addRow("Iterations", self.spin_iters)
        f.addRow("Airflow", self.spin_airflow)
        f.addRow("Grid", self.spin_tgrid)
        self.chk_load_power = QCheckBox("Include load power dissipation")
        self.chk_load_power.setChecked(t.load_power)
        self.chk_load_power.setToolTip(
            "Heat the board with the power each load draws too: the sum over its supply and "
            "ground pins of pin current × pin voltage, spread over those pins. Off: only "
            "copper, via, DC connection and VRM conversion losses heat the board.")
        f.addRow(self.chk_load_power)
        tip(f, self.spin_ambient, "Temperature of the air around the board")
        tip(f, self.spin_iters, "Electrical and thermal solves to alternate. Three is usually "
                                "enough; the Notes tab shows how much each one changed.")
        tip(f, self.spin_airflow, "Air speed over both faces of the board. The air always "
                                  "flows parallel to the board, along its surface, not onto "
                                  "it, and its direction cannot be set: the model cools every "
                                  "point equally, as if the air stayed at ambient all the way "
                                  "across. Faster air cools the board more.")
        tip(f, self.spin_tgrid, "Cell size of the thermal grid. 'Auto' gives about 250 cells "
                                "across the board.")
        self.spin_max_t = _spin(0, 500, t.max_temp_c or 0.0, 1, " °C", 5)
        self.spin_max_t.setSpecialValueText("none")
        self.spin_max_t.setToolTip("Copper hotter than this, on any layer, is marked FAIL")
        self.spin_max_rise = _spin(0, 500, t.max_rise_c or 0.0, 1, " °C", 5)
        self.spin_max_rise.setSpecialValueText("none")
        self.spin_max_rise.setToolTip("Copper this much hotter than ambient, on any layer, is "
                                      "marked FAIL")
        f.addRow("Max copper temperature", self.spin_max_t)
        f.addRow("Max rise above ambient", self.spin_max_rise)
        for sp in (self.spin_max_t, self.spin_max_rise):
            f.labelForField(sp).setToolTip(sp.toolTip())
        note = QLabel("Heat from every net and layer (copper, vias, DC parts, VRM conversion "
                      "loss and, if ticked, load power) spreads through the board's copper "
                      "and laminate and leaves both faces by convection: h = 10 + 3.8·v W/m²K "
                      "per face. Each layer's thermal conductivity is in the Stackup window.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        f.addRow(note)
        self.chk_thermal.toggled.connect(self._thermal_changed)
        self.chk_load_power.toggled.connect(self._thermal_changed)
        for wdg in (self.spin_ambient, self.spin_airflow, self.spin_tgrid, self.spin_max_t,
                    self.spin_max_rise):
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
        t.load_power = self.chk_load_power.isChecked()
        t.max_temp_c = self.spin_max_t.value() or None
        t.max_rise_c = self.spin_max_rise.value() or None
        for wdg in (self.spin_ambient, self.spin_iters, self.spin_airflow, self.spin_tgrid,
                    self.chk_load_power, self.spin_max_t, self.spin_max_rise):
            wdg.setEnabled(t.enabled)
        self.btn_solve.setText(self._solve_label())
        if getattr(self, "_tree", None) is not None:
            self._fill_thermal()        # the limits grade the last solve straight away
        self._on_changed()

    def _solve_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        note = QLabel("Net groups to solve. Untick the ones that are not modeled or routed yet "
                      "to solve the rest.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        v.addWidget(note)
        self.lst_groups = QListWidget(w)
        self.lst_groups.setToolTip("Untick a net group to leave it out of the solve. VRMs "
                                   "feeding or fed by it are replaced by an ideal source or a "
                                   "constant load; the Notes tab lists what changed.")
        self.lst_groups.itemChanged.connect(self._store_ticks)
        v.addWidget(self.lst_groups, 1)
        row = QHBoxLayout()
        for text, on in (("All", True), ("None", False)):
            b = QPushButton(text, w)
            b.setToolTip("Tick every net group" if on else "Untick every net group")
            b.clicked.connect(partial(self._tick_all_groups, on))
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def _fill_groups(self):
        """One checkable row per net group in the setup: rails, then grounds, then the rest."""
        setup = self.setup
        groups = net_groups(setup)
        driver = {groups[v.output.net]: v for v in setup.vrms}
        grounds = {groups[r.net] for r in [*(v.ground for v in setup.vrms),
                                           *(v.input_ground for v in setup.vrms),
                                           *(l.ground for l in setup.loads)] if r}
        off = set(setup.excluded_nets)

        def role(g):
            if g in driver:
                return 0, f"rail from {driver[g].name}"
            if g in grounds:
                return 1, "ground"
            return 2, "no VRM drives it"
        rows = sorted((role(g), group_name(g), g) for g in set(groups.values()))
        self.lst_groups.blockSignals(True)
        self.lst_groups.clear()
        for (_, what), name, g in rows:
            it = QListWidgetItem(f"{name}  ({what})")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked if g & off else Qt.Checked)
            it.setData(Qt.UserRole, sorted(g))
            self.lst_groups.addItem(it)
        self.lst_groups.blockSignals(False)

    def _store_ticks(self, *_):
        """Exclude the nets of every unticked group."""
        lst = self.lst_groups
        self.setup.excluded_nets = sorted(
            n for i in range(lst.count()) if lst.item(i).checkState() != Qt.Checked
            for n in lst.item(i).data(Qt.UserRole))
        self._on_changed()

    def _tick_all_groups(self, on: bool):
        self.lst_groups.blockSignals(True)
        for i in range(self.lst_groups.count()):
            self.lst_groups.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.lst_groups.blockSignals(False)
        self._store_ticks()

    def _solve_label(self) -> str:
        n = self.lst_groups.count()
        on = sum(self.lst_groups.item(i).checkState() == Qt.Checked for i in range(n))
        text = "Solve all" if on == n else f"Solve {on} of {n} net groups"
        return text + (" (electro-thermal)" if self.setup.thermal.enabled else "")

    def _settings_changed(self, *_):
        s = self.setup.settings
        s.max_edge_mm = self.spin_edge.value()
        s.grading = self.spin_grading.value()
        s.via_rules = self.via_rules.rules()
        self._on_changed()

    # ------------------------------------------------------------------ data
    def set_board(self, snap: BoardSnapshot, setup: PISetup):
        self.snapshot = snap
        self.setup = setup
        self._scale_limits.clear()
        self._scale_key = None
        self._enable_scale(False)
        for sp, v in ((self.spin_edge, setup.settings.max_edge_mm),
                      (self.spin_grading, setup.settings.grading)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
        self.via_rules.blockSignals(True)
        self.via_rules.set_rules(setup.settings.via_rules)
        self.via_rules.blockSignals(False)
        self.clear_results()
        self.spin_via_limit.blockSignals(True)
        self.spin_via_limit.setValue((setup.via_current_limit_a or 0.0) * 1e3)
        self.spin_via_limit.blockSignals(False)
        t = setup.thermal
        for wdg, val in ((self.spin_ambient, t.ambient_c), (self.spin_iters, t.iterations),
                         (self.spin_airflow, t.airflow_m_s), (self.spin_tgrid, t.grid_mm),
                         (self.spin_max_t, t.max_temp_c or 0.0),
                         (self.spin_max_rise, t.max_rise_c or 0.0),
                         (self.chk_thermal, t.enabled), (self.chk_load_power, t.load_power)):
            wdg.blockSignals(True)
            (wdg.setChecked if isinstance(wdg, QCheckBox) else wdg.setValue)(val)
            wdg.blockSignals(False)
        self.report_option.set_value(setup.report_format)
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

    def reload_shared(self):
        """Show edits made in the AC panel to the shared VRMs and series links."""
        if self.snapshot is None:
            return
        self._refresh_pickers()
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.reload()
        self._on_changed()

    def _refresh_pickers(self):
        for il in (self.vrm_list, self.load_list, self.link_list):
            il.editor.refresh_nets(self._nets)

    def _part_lists(self):
        return {"vrm": self.vrm_list, "load": self.load_list, "link": self.link_list}

    def _on_changed(self):
        self._fill_groups()
        active, _ = active_setup(self.setup)
        errors = validate(active) if (active.vrms or active.loads) else []
        if not self.setup.vrms:
            errors.insert(0, "Add a VRM (the source of a net) to start")
        elif not active.vrms:
            errors.insert(0, "Tick a net group driven by a VRM in the Solve tab")
        self.lbl_errors.setText("\n".join(f"• {e}" for e in errors))
        self.btn_solve.setEnabled(not errors)
        self.btn_solve.setText(self._solve_label())
        self.setup_changed.emit()

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
        return None if net is None else new_vrm(self.snapshot, self.setup, self._nets, net)

    def _new_load(self):
        net = self._first_net()
        if net is None:
            return None
        ref = self._first_part(net)
        supply = PinRef(ref, net)
        return Load(_unique(ref or "LOAD1", [x.name for x in self.setup.loads]), supply, 0.1,
                    ground=guess_ground_pins(self.snapshot, self.setup, self._nets, supply))

    def split_load_across_nets(self, ref: Optional[str] = None, dialog=None):
        """Share a part's total current over the supply nets the user ticks, as a load on
        each (#182). ``dialog`` stands in for the dialog's run in tests."""
        from kipi.dc.gui.split_load import SplitLoadDialog
        from kipi.dc.pi.split import existing_load
        if self.snapshot is None:
            return
        parts: Dict[str, str] = {}
        for net in self._nets:
            for r, value, _ in parts_on_net(self.snapshot, net):
                parts.setdefault(r, value)
        current = self.load_list.current()
        ref = ref or (current.supply.ref if current is not None else None)
        dlg = SplitLoadDialog(self.snapshot, self.setup, sorted(parts.items()), self._nets,
                              ref, self)
        if not (dialog(dlg) if dialog else dlg.exec()):
            return
        ref = dlg.ref()
        for net, amps in dlg.chosen().items():
            load = existing_load(self.setup, ref, net)
            if load is None:
                supply = PinRef(ref, net)
                load = Load(_unique(f"{ref} {net}", [x.name for x in self.setup.loads]),
                            supply, amps, ground=guess_ground_pins(
                                self.snapshot, self.setup, self._nets, supply))
                self.setup.loads.append(load)
            load.current, load.groups = amps, []
        self.load_list.reload()
        self.load_list.changed.emit()

    def apply_load_model(self, model=None):
        """Apply a load model file (or ``model``) to the loads the user ticks."""
        model = model or open_model(self)
        if model is None:
            return
        loads = self.setup.loads
        if not loads:
            QMessageBox.information(self, "Load model", "There are no loads to apply it to; "
                                    "use Add loads from model file.")
            return
        dlg = ApplyDialog(model, [(l.name, l.supply) for l in loads], "load", self)
        if not dlg.exec():
            return
        for row, i in dlg.chosen():
            apply_to_load(loads[row], model.supplies[i])
        self.load_list.reload()
        self.load_list.changed.emit()

    def add_loads_from_model(self, model=None):
        """Add a load model's loads on the parts the user ticks."""
        model = model or open_model(self)
        if model is None:
            return
        parts = candidate_parts(model, self.snapshot)
        if not parts:
            QMessageBox.information(self, "Load model", "No part on the board has the pins of "
                                    "any of the model's supplies (adding loads needs its pins).")
            return
        dlg = PartsDialog(model, parts, "load", self)
        if not dlg.exec():
            return
        added = 0
        for ref in dlg.chosen():
            for s, rail, gnd in new_items(model, ref, part_nets(self.snapshot, ref)):
                gnd = gnd or guess_ground_pins(self.snapshot, self.setup, self._nets, rail)
                load = Load(_unique(f"{ref} {s.name}", [x.name for x in self.setup.loads]),
                            rail, s.current_a or 0.0, ground=gnd)
                apply_to_load(load, s)
                self.setup.loads.append(load)
                added += 1
        if added:
            self.load_list.reload()
            self.load_list.lst.setCurrentRow(self.load_list.lst.count() - 1)
            self.load_list.changed.emit()

    def _new_link(self):
        net = self._first_net()
        return None if net is None else new_link(self.snapshot, self.setup, self._nets, net)

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
        kind = ", diode A→B" if k.is_diode else ""
        kind += f", SPICE {k.spice.subckt}" if k.uses_spice else ""
        if k.is_multi:
            r = f"{len(k.sides)} pins"
        return f"{k.name}: {' ↔ '.join(p.net for p in k.sides)} ({r}{kind})"

    # ------------------------------------------------------------------ results
    def set_busy(self, busy: bool, queued: bool = False):
        self.btn_solve.setEnabled(not busy and not self.lbl_errors.text())
        idle = self._solve_label()
        self.btn_solve.setText(("Queued…" if queued else "Solving…") if busy else idle)

    def current_field(self) -> str:
        return self.cmb_field.currentData()

    def clear_results(self):
        self._tree = None
        if hasattr(self, "btn_export"):
            self.btn_export.setEnabled(False)
        if hasattr(self, "report_option"):
            self.report_option.set_available(False)
        for t in (self.tbl_loads, self.tbl_vrms, self.tbl_links, self.tbl_vias, self.tbl_losses,
                  self.tbl_temps):
            t.setRowCount(0)
        self._show_thermal_tab(False)
        self.txt_notes.clear()
        self.cmb_group.blockSignals(True)
        self.cmb_group.clear()
        self.cmb_group.blockSignals(False)

    def show_error(self, message: str):
        self.res_tabs.setCurrentWidget(self.txt_notes)
        self.txt_notes.setHtml(f'<span style="color:#ff7070"><b>Cannot solve:</b><br>'
                               f'{html.escape(message).replace(chr(10), "<br>")}</span>'
                               "<br><br>To solve the other nets first, untick the net group "
                               "with the problem in the Solve tab.")

    def vunit(self) -> str:
        """The unit the results tables show voltages in (#177)."""
        return self.cmb_vunit.currentData() or vunits.DEFAULT

    def _load_headers(self) -> List[str]:
        u = self.vunit() if hasattr(self, "cmb_vunit") else vunits.DEFAULT
        return ["Load", "Group", "I (A)",
                *[vunits.header(n, u) for n in vunits.LOAD_COLUMNS], ""]

    def _vrm_headers(self) -> List[str]:
        u = self.vunit() if hasattr(self, "cmb_vunit") else vunits.DEFAULT
        return ["VRM", "Type", vunits.header("V out", u), "I out (A)", "V in", "I in (A)",
                "P in (W)", "Loss (W)", vunits.header("Rail lowest", u), "I limit (A)", ""]

    def _vunit_changed(self, *_):
        self.tbl_loads.setHorizontalHeaderLabels(self._load_headers())
        self.tbl_vrms.setHorizontalHeaderLabels(self._vrm_headers())
        if getattr(self, "_tree", None) is not None:
            self._fill_loads_vrms(self._tree)

    def _fill_loads_vrms(self, tree):
        """The Loads and VRMs tables, their voltages in the chosen unit."""
        u = self.vunit()
        for t in (self.tbl_loads, self.tbl_vrms):
            reset_sort(t)
        key = lambda text: (text, float(text) if text else None)  # noqa: E731
        t = self.tbl_loads
        t.setRowCount(len(tree.loads))
        for r, l in enumerate(tree.loads):
            mean, low, high, tol, margin = vunits.load_cells(l, u)
            set_row(t, r, [l.name, l.group, f"{l.current:.4g}", key(mean), key(low),
                           key(high), tol, key(margin), l.verdict])
            color = (("#ffb040" if l.status else None) if l.passed is None
                     else ("#60d060" if l.passed else "#ff6060"))
            if color:
                t.item(r, 8).setForeground(QColor(color))
            m = vunits.margin(l)
            if m is not None and m < 0:
                t.item(r, 7).setForeground(QColor("#ff6060"))
        f = lambda x, fmt: "" if x is None else format(x, fmt)  # noqa: E731
        t = self.tbl_vrms
        t.setRowCount(sum(1 + len(v.phases) for v in tree.vrms))
        r = 0
        for v in tree.vrms:                       # each VRM, then a row per phase under it
            kind = v.kind if not v.phases else f"{v.kind}, {len(v.phases)} phases"
            status = "" if v.passed is None else ("PASS" if v.passed else "FAIL")
            target = tree.rail_target(v)
            drop, _pct = tree.rail_drop(v)         # the rail's largest drop (#188)
            ref = v.v_set if target is None else target
            lowest = None if drop is None else ref - drop
            set_row(t, r, [v.name, kind, key(vunits.show(v.v_out, ref, u)), f"{v.i_out:.4g}",
                           f(v.v_in, ".5f"), f(v.i_in, ".4g"), f(v.p_in, ".4g"),
                           f(v.p_loss, ".4g"), key(vunits.show(lowest, ref, u)),
                           f(v.current_limit_a, "g"), status])
            if v.passed is not None:
                t.item(r, 10).setForeground(QColor("#60d060" if v.passed else "#ff6060"))
            parent = r
            for p in v.phases:
                r += 1
                set_row(t, r, [f"    phase {p.index}", "", vunits.show(p.v_out, ref, u),
                               f"{p.i_out:.4g}", f(p.v_in, ".5f"), f(p.i_in, ".4g"),
                               f(p.p_in, ".4g"), f(p.p_loss, ".4g")],
                        parent_row=parent, sub=p.index)
                for c in range(t.columnCount()):
                    t.item(r, c).setForeground(QColor("#a0a0a0"))
            r += 1

    def show_results(self, tree, initial_group: str):
        reset_sort(self.tbl_links)                # new results show in their natural order
        self._fill_loads_vrms(tree)
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
            notes.insert(0, f"<b>Electro-thermal:</b> {th.heat_w:.3g} W into the board"
                            f"{_load_heat(th)}, peak "
                            f"<b>{tmax:.1f} °C</b> at ({x:.1f}, {y:.1f}) mm"
                            f"{self._peak_layer(th)}, ambient "
                            f"{th.settings.ambient_c:g} °C, airflow {th.settings.airflow_m_s:g} m/s "
                            "(details in the Thermal tab)")
            if getattr(th, "heat_by_kind", None):
                notes.insert(1, "&nbsp;&nbsp;heat by source: "
                                + html.escape(th.heat_breakdown()))
        for g in tree.groups.values():
            role = f"driven by {g.driver}" if g.kind == "rail" else f"ground, referenced at {g.driver}"
            notes.append(f"&nbsp;&nbsp;<b>{html.escape(g.name)}</b> ({role}): "
                         f"{html.escape(', '.join(g.nets))}")
        for v in tree.vrms:
            if getattr(v, "load_line_mohm", 0.0):
                notes.append(f"<b>{html.escape(v.name)}</b> load line: "
                             f"{v.load_line_mohm:g} mΩ × {v.i_out:.4g} A = "
                             f"{v.v_droop * 1e3:.4g} mV droop, regulating to "
                             f"{v.v_target:.5f} V (set {v.v_set:g} V)")
        for l in tree.loads:
            if l.pin_groups:
                notes.append(f"<b>{html.escape(l.name)}</b> pin groups:")
                for name, amps, vmean, vmin, vmax in l.pin_groups:
                    if math.isnan(vmean):
                        notes.append(f"&nbsp;&nbsp;{html.escape(name)}: not connected, "
                                     "draws nothing")
                        continue
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
        self._fill_thermal()
        self.btn_export.setEnabled(True)
        self.report_option.set_available(True)
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
