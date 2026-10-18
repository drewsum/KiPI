"""The AC setup panel, shown in place of the DC power-tree panel in AC mode.

It edits the same :class:`~kipi.dc.pi.model.PISetup` as the DC panel. The VRMs and series
links are shared (every VRM field editable in both); capacitor models, probes and
the sweep belong to the AC analysis. A solve covers one rail (picked by its VRM) against
one ground net.
"""

from __future__ import annotations

import copy
import html
import re
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QColor
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSlider, QSpinBox,
    QTableWidget, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from kipi.ac.checks import capacitor_candidates, default_ground, validate_ac
from kipi.ac.model import AGING_PCT_PER_DECADE, CapModel, Probe
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.nets import classify_nets, sorted_nets
from kipi.core.model.parts import (
    is_class2, parse_capacitance, parse_dielectric, parse_voltage_rating,
)
from kipi.dc.gui.pi_panel import (
    ItemList, LinkEditor, PartSelection, PIPanel, VrmEditor, _Editor, _spin, _unique,
    make_searchable, new_link, new_vrm, parts_on_net, tip,
)
from kipi.dc.pi.autofill import guess_ground_pins, part_nets
from kipi.dc.pi.load_model import apply_to_probe, new_items
from kipi.ac.target import vrm_input_name, vrm_inputs
from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm
from kipi.gui.load_model_dialogs import (
    ApplyDialog, PartsDialog, candidate_parts, export_model, model_button, open_model,
)
from kipi.gui.report_option import ReportOption
from kipi.gui.sortable import (
    SortItem, make_sortable, reset_sort, resort, set_row, sorted_rows,
)
from kipi.gui.tooltip_art import art_tip

TITLE = "AC setup"

# The hours slider (#109) runs on a log scale from 1000 hours, where aging starts, to
# 1,000,000; the spin box beside it takes any value.
HOURS_SLIDER_DECADES = (3.0, 6.0)
HOURS_SLIDER_STEPS = 300
HOURS_PER_YEAR = 8760.0


# ---------------------------------------------------------------------------
# Capacitor models
# ---------------------------------------------------------------------------

class CapEditor(_Editor):
    """A capacitor model's values, and which capacitors use it.

    The table lists the capacitors a model can apply to: parts C<n> whose pads are all on
    nets ticked in the Board panel (so, between two power/ground nets). Ticking a part
    assigns it to this model (moving it from another model if it had one).
    """
    COLS = ("", "Part", "Value", "Footprint", "Nets", "Model")
    C_VALUE, C_FOOTPRINT = 2, 3

    def __init__(self, panel: "ACPanel", parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.cap = _spin(0, 1e6, 0, 4, " µF", 0.1)
        self.cap.setSpecialValueText("required")
        self.esl = _spin(0, 1e4, 0, 3, " nH", 0.05)
        self.esl.setSpecialValueText("required")
        self.esr = _spin(0, 1e6, 0, 3, " mΩ", 0.5)
        self.esr.setSpecialValueText("required")
        f.addRow("Name", self.name)
        f.addRow("Capacitance", self.cap)
        f.addRow("ESL", self.esl)
        f.addRow("ESR", self.esr)
        self.rated = _spin(0, 1e4, 0, 3, " V", 1)
        self.rated.setSpecialValueText("not set")
        self.kept = _spin(1, 100, 40, 1, " %", 5)
        f.addRow("Voltage rating", self.rated)
        f.addRow("Kept at rating", self.kept)
        self.dielectric = QComboBox()
        self.dielectric.addItem("not set", "")
        for d in AGING_PCT_PER_DECADE:
            self.dielectric.addItem(d, d)
        self.aging = _spin(0, 50, 0, 2, " %/decade", 0.5)
        f.addRow("Dielectric", self.dielectric)
        f.addRow("Aging", self.aging)
        self.mfg_tol = _spin(0, 100, 0, 1, " %", 1)
        self.mfg_tol.setSpecialValueText("not set")
        f.addRow("Manufacturing tolerance", self.mfg_tol)
        # Drawings under the tooltips (#116): the series model and its impedance curve with
        # the field's element picked out, the DC bias curve and aging.
        art = {self.cap: ("cap_model", "c"), self.esl: ("cap_model", "l"),
               self.esr: ("cap_model", "r"), self.rated: ("dc_bias", "rated"),
               self.kept: ("dc_bias", "kept"), self.aging: ("aging", "aging"),
               self.dielectric: ("dielectric", "x7r")}
        for w, text in (
                (self.name, "A name for this model, e.g. the part number or value"),
                (self.cap, "Capacitance of one part, at no DC bias. For a ceramic (X5R, "
                           "X7R…), enter its voltage rating to estimate DC bias, or enter "
                           "the effective value at the rail's voltage if the datasheet "
                           "gives it."),
                (self.rated, "The part's DC voltage rating. With one, the capacitance is "
                             "derated by the DC voltage across each part, as class II "
                             "ceramics (X5R, X7R…) lose capacitance with voltage: the last "
                             "DC solve's voltage across the part, or else the rail's VRM set "
                             "point. Leave it 'not set' for polymer and electrolytic parts; "
                             "a C0G dielectric is never derated."),
                (self.kept, "How much of its capacitance the part keeps at its rated "
                            "voltage, from the datasheet's DC bias curve (typically 30 to "
                            "50 %; smaller cases lose more). The curve is shaped like a "
                            "typical X5R/X7R part's."),
                (self.dielectric, "The ceramic dielectric, for aging and temperature. With "
                                  "Capacitor aging ticked in the Sweep tab, the model loses "
                                  "its aging rate for every decade of hours operated past "
                                  "1000; with a Temperature there, its capacitance follows "
                                  "the dielectric's typical temperature curve. Leave it unset "
                                  "for polymer and electrolytic parts."),
                (self.aging, "Capacitance lost per decade of hours (e.g. from 1000 to 10000 "
                             "hours). Picking a dielectric fills in its typical rate; the "
                             "datasheet's is better."),
                (self.mfg_tol, "The part's capacitance tolerance, from its datasheet or "
                                "value code (K is ±10 %, M ±20 %, J ±5 %). Optional. With "
                                "Capacitor manufacturing tolerance ticked in the Sweep tab, "
                                "the model is solved at the low end of it, the worst case for "
                                "a PDN: a ±10 % part keeps 90 % of its capacitance."),
                (self.esl, "Equivalent series inductance of the part itself (from its "
                           "datasheet); the board's mounting inductance is simulated"),
                (self.esr, "Equivalent series resistance of one part, from its datasheet")):
            tip(f, w, text, art.get(w))
        self._watch(self.name, self.cap, self.esl, self.esr, self.rated, self.kept,
                    self.mfg_tol)
        self.rated.valueChanged.connect(lambda v: self.kept.setEnabled(v > 0))
        self.dielectric.currentIndexChanged.connect(self._dielectric_changed)
        self._watch(self.aging)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter parts (reference, value, footprint or net)…")
        self.filter.setToolTip("Show only the parts whose reference, value, footprint, nets "
                               "or model contain this text")
        self.filter.textChanged.connect(self._apply_filter)
        f.addRow(self.filter)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(140)
        self.table.itemChanged.connect(self._item_changed)
        self.table.setToolTip("Capacitors between nets ticked in the Board panel. Tick a part "
                              "to give it this model (taking it from any other model); grayed "
                              "rows already use another model. Click a column header to "
                              "sort by it.")
        make_sortable(self.table)
        f.addRow(self.table)
        same = QHBoxLayout()
        for label, cols, text in (
                ("Tick same value", (self.C_VALUE,), "the value"),
                ("Tick same footprint", (self.C_FOOTPRINT,), "the footprint"),
                ("Tick same value + footprint", (self.C_VALUE, self.C_FOOTPRINT),
                 "both the value and the footprint")):
            b = QPushButton(label)
            b.setToolTip(f"Tick every listed part with {text} of the selected row")
            b.clicked.connect(lambda _=False, c=cols: self._tick_same(c))
            same.addWidget(b)
        f.addRow(same)
        row = QHBoxLayout()
        b_all = QPushButton("Tick shown")
        b_all.setToolTip("Give this model to every part the filter shows")
        b_all.clicked.connect(lambda: self._tick_shown(True))
        b_none = QPushButton("Untick shown")
        b_none.setToolTip("Take this model off every part the filter shows")
        b_none.clicked.connect(lambda: self._tick_shown(False))
        for b in (b_all, b_none):
            row.addWidget(b)
        f.addRow(row)
        self.lbl_count = QLabel()
        self.lbl_count.setStyleSheet("color: #999;")
        f.addRow(self.lbl_count)
        self._filling = False

    def _dielectric_changed(self, _i):
        d = self.dielectric.currentData() or ""
        self.aging.setEnabled(bool(d))
        if not self._loading:
            self.aging.blockSignals(True)
            self.aging.setValue(AGING_PCT_PER_DECADE.get(d, 0.0))
            self.aging.blockSignals(False)
        self._store()

    # ------------------------------------------------------------ values
    def _load(self, m: CapModel):
        self.name.setText(m.name)
        self.cap.setValue(m.capacitance_f * 1e6)
        self.esl.setValue(m.esl_h * 1e9)
        self.esr.setValue(m.esr_ohm * 1e3)
        self.rated.setValue(m.rated_v or 0.0)
        self.kept.setValue(m.kept_at_rated_pct)
        self.kept.setEnabled(bool(m.rated_v))
        i = self.dielectric.findData(m.dielectric)
        if i < 0:                                  # one KiPI does not list: keep it
            self.dielectric.addItem(m.dielectric, m.dielectric)
            i = self.dielectric.count() - 1
        self.dielectric.setCurrentIndex(i)
        self.aging.setValue(m.aging_pct_per_decade)
        self.aging.setEnabled(bool(m.dielectric))
        self.mfg_tol.setValue(m.mfg_tolerance_pct or 0.0)
        self.fill_table()

    def _save(self, m: CapModel):
        m.name = self.name.text().strip() or m.name
        m.capacitance_f = self.cap.value() * 1e-6
        m.esl_h = self.esl.value() * 1e-9
        m.esr_ohm = self.esr.value() * 1e-3
        m.rated_v = self.rated.value() or None
        m.kept_at_rated_pct = self.kept.value()
        m.dielectric = self.dielectric.currentData() or ""
        m.aging_pct_per_decade = self.aging.value()
        m.mfg_tolerance_pct = self.mfg_tol.value() or None

    # ------------------------------------------------------------ parts
    def fill_table(self):
        panel: ACPanel = self.panel
        t = self.table
        self._filling = True
        t.setRowCount(0)
        snap = panel.snapshot
        cands = capacitor_candidates(snap, panel.nets()) if snap is not None else []
        owner = {ref: m for m in panel.setup.caps for ref in m.refs}
        t.setRowCount(len(cands))
        for r, c in enumerate(cands):
            chk = SortItem()
            chk.row0 = r
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            mine = self.obj is not None and owner.get(c.ref) is self.obj
            chk.setCheckState(Qt.Checked if mine else Qt.Unchecked)
            chk.setData(Qt.UserRole, c.ref)
            t.setItem(r, 0, chk)
            other = owner.get(c.ref)
            cells = (c.ref, c.value, c.footprint.split(":")[-1], " / ".join(c.nets),
                     other.name if other else "")
            for col, text in enumerate(cells, start=1):
                it = SortItem(text)
                it.row0 = r
                if col == self.C_FOOTPRINT:
                    it.setData(Qt.UserRole, c.footprint)
                    it.setToolTip(c.footprint or "Footprint not known: re-read the board "
                                                 "from KiCad to fill it in")
                if other is not None and not mine:
                    it.setForeground(QColor("#999"))
                t.setItem(r, col, it)
        resort(t)                    # ticking a part keeps the order the user sorted
        self._filling = False
        self._apply_filter()
        self._update_count(len(cands))

    def _update_count(self, n_cands: Optional[int] = None):
        if n_cands is None:
            n_cands = self.table.rowCount()
        n = len(self.obj.refs) if self.obj is not None else 0
        self.lbl_count.setText(f"{n} part(s) use this model; {n_cands} capacitor(s) between "
                               "ticked nets")

    def _apply_filter(self, *_):
        text = self.filter.text().strip().lower()
        for r in range(self.table.rowCount()):
            row_text = " ".join(self.table.item(r, c).text().lower()
                                for c in range(1, len(self.COLS)))
            self.table.setRowHidden(r, bool(text) and text not in row_text)

    def _item_changed(self, item):
        if self._filling or item.column() != 0 or self.obj is None:
            return
        self._assign([item.data(Qt.UserRole)], item.checkState() == Qt.Checked)

    def _assign(self, refs: List[str], on: bool):
        """Put ``refs`` in this model (taking them from any other) or take them out."""
        for m in self.panel.setup.caps:
            if m is not self.obj or not on:
                m.refs = [r for r in m.refs if r not in refs]
        if on:
            self.obj.refs = sorted(set(self.obj.refs) | set(refs), key=_natural)
        self.fill_table()
        self.changed.emit()

    def _tick_shown(self, on: bool):
        if self.obj is None:
            return
        refs = [self.table.item(r, 0).data(Qt.UserRole) for r in range(self.table.rowCount())
                if not self.table.isRowHidden(r)]
        self._assign(refs, on)

    def _tick_same(self, cols):
        """Tick every part matching the selected row in columns ``cols`` (value, the full
        library footprint)."""
        if self.obj is None:
            return
        r = self.table.currentRow()
        if r < 0:
            QMessageBox.information(self, "KiPI", "Select a part in the list first.")
            return
        t = self.table

        def key(row):
            return tuple(t.item(row, c).data(Qt.UserRole) if c == self.C_FOOTPRINT
                         else t.item(row, c).text() for c in cols)
        want = key(r)
        if self.C_FOOTPRINT in cols and not t.item(r, self.C_FOOTPRINT).data(Qt.UserRole):
            QMessageBox.information(self, "KiPI", "This part's footprint is not known. "
                                    "Re-read the board from KiCad to fill footprints in.")
            return
        refs = [t.item(k, 0).data(Qt.UserRole) for k in range(t.rowCount())
                if key(k) == want]
        self._assign(refs, True)


def _fmt_farads(f: float) -> str:
    for scale, unit in ((1e-3, "mF"), (1e-6, "µF"), (1e-9, "nF"), (1e-12, "pF")):
        if f >= scale * 0.999:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.3g} F"


def _target_text(c) -> str:
    """A result's target for the table: its level, or a custom mask's range."""
    if c.target_points:
        zs = [z * 1e3 for _, z in c.target_points]
        lo, hi = min(zs), max(zs)
        text = f"{lo:g} (custom)" if lo == hi else f"{lo:g}–{hi:g} (custom)"
    elif c.target_ohm:
        text = f"{c.target_ohm * 1e3:g}"
    else:
        return ""
    above = getattr(c, "above_hz", None)
    return f"{text} above {_fmt_hz(above)}" if above else text


def _fmt_hz(f: float) -> str:
    for scale, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


def _natural(ref: str):
    m = re.match(r"([A-Za-z_]*)(\d*)(.*)", ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else -1, m.group(3))


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

class ProbeEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.rail = self._picker()
        self.ground = self._picker()
        self.target = _spin(0, 1e6, 0, 3, " mΩ", 1)
        self.target.setSpecialValueText("none")
        self.target_f = _spin(0, 1e7, 0, 1, " kHz", 100)
        self.target_f.setSpecialValueText("none")
        self.btn_calc = QPushButton("Target Impedance/Frequency Calculator…")
        self.btn_calc.setToolTip("Work out a target impedance and frequency from the "
                                 "device's supply tolerance and transient current")
        self.btn_calc.clicked.connect(self._calculator)
        self.target_kind = QComboBox()
        self.target_kind.addItem("Flat, up to a frequency", "flat")
        self.target_kind.addItem("Custom (piecewise linear)", "custom")
        self.points = QTableWidget(0, 3)
        self.points.setHorizontalHeaderLabels(["Frequency", "Target |Z|", ""])
        hdr = self.points.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.points.setColumnWidth(2, 28)
        self.points.verticalHeader().setVisible(False)
        self.points.setMinimumHeight(130)
        self.points.setToolTip(art_tip(
            "Corner points of the custom target mask: a frequency and the target |Z| there. "
            "Click a column header to sort by it.", "target_types", "custom"))
        make_sortable(self.points, self._sort_points, columns=(0, 1))
        self.btn_point = QPushButton("Add point")
        self.btn_point.setToolTip("Add a point a decade above the last one")
        self.btn_point.clicked.connect(self._add_point)
        self.lbl_points = QLabel("Points are joined by straight lines on the log-log plot; "
                                 "the target holds the first point's |Z| below it and the "
                                 "last point's |Z| above it.")
        self.lbl_points.setWordWrap(True)
        self.lbl_points.setStyleSheet("color: #999;")
        f.addRow("Name", self.name)
        f.addRow("Supply pins", self.rail)
        f.addRow("Ground pins", self.ground)
        f.addRow("Target", self.target_kind)
        f.addRow("Target |Z|", self.target)
        f.addRow("Target frequency", self.target_f)
        f.addRow(self.btn_calc)
        f.addRow(self.points)
        f.addRow(self.btn_point)
        f.addRow(self.lbl_points)
        hint = QLabel("The PDN impedance solution is the impedance from the probe point "
                      "looking out into the power distribution network through all loop "
                      "paths (power planes, vias, capacitors, series links).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        f.addRow(hint)
        # Drawings under the tooltips (#116): the flat and custom targets on the |Z| plot.
        art = {self.target_kind: ("target_types", "flat", "custom"),
               self.target: ("target_types", "flat"), self.target_f: ("target_types", "tf")}
        for w, text in (
                (self.name, "A name for this probe, shown in the plot legend and the table"),
                (self.rail, "The device's supply pins: where the impedance is looked into"),
                (self.ground, "The device's ground pins, on the ground net being simulated. "
                              "Picking the supply pins fills in the same part's ground pins."),
                (self.target_kind, "Flat: one impedance up to a frequency. Custom: a target "
                                   "mask through frequency/impedance points."),
                (self.target, "Target impedance: PASS if |Z| stays at or below this from "
                              "the VRM's control loop bandwidth (if set) up to the target "
                              "frequency"),
                (self.target_f, "Frequency up to which the target impedance applies "
                                "(none: the whole sweep)")):
            tip(f, w, text, art.get(w))
        self._watch(self.name, self.target, self.target_f, self.target_kind)
        self.target_kind.currentIndexChanged.connect(self._target_rows)
        self._target_rows()

    def _target_rows(self, *_):
        custom = self.target_kind.currentData() == "custom"
        for w in (self.target, self.target_f, self.btn_calc):
            self.form.setRowVisible(w, not custom)
        for w in (self.points, self.btn_point, self.lbl_points):
            self.form.setRowVisible(w, custom)

    def _add_point_row(self, f_hz: float, z_ohm: float):
        r = self.points.rowCount()
        self.points.insertRow(r)
        sf = _spin(0, 1e7, f_hz / 1e3, 3, " kHz", 100)
        sz = _spin(0, 1e6, z_ohm * 1e3, 3, " mΩ", 1)
        for col, sp in ((0, sf), (1, sz)):
            sp.setSpecialValueText("required")
            sp.valueChanged.connect(self._store)
            self.points.setCellWidget(r, col, sp)
        btn = QToolButton()
        btn.setText("✕")
        btn.setAutoRaise(True)
        btn.setToolTip("Remove this point")
        btn.clicked.connect(lambda _=False, b=btn: self._remove_point_of(b))
        self.points.setCellWidget(r, 2, btn)

    def _add_point(self):
        pts = self._points_value()
        f_hz, z_ohm = max(pts) if pts else (1e5, self.target.value() / 1e3 or 0.01)
        self._add_point_row(min(f_hz * 10, 1e10), z_ohm)
        reset_sort(self.points)
        self._store()

    def _sort_points(self, col: int, descending: bool):
        """Rebuild the point rows ordered by frequency (0) or |Z| (1)."""
        pts = sorted_rows(self._points_value(), col, descending, keys={0: float, 1: float})
        self.points.setRowCount(0)
        for f_hz, z_ohm in pts:
            self._add_point_row(f_hz, z_ohm)
        self._store()

    def _remove_point_of(self, widget):
        for r in range(self.points.rowCount()):
            if self.points.cellWidget(r, 2) is widget:
                self.points.removeRow(r)
                self._store()
                return

    def _points_value(self):
        return [(self.points.cellWidget(r, 0).value() * 1e3,
                 self.points.cellWidget(r, 1).value() / 1e3)
                for r in range(self.points.rowCount())]

    def _calculator(self):
        from kipi.ac.gui.target_calc import TargetCalculator
        from kipi.ac.target import probe_loads, vrm_accuracies
        rail = self.rail.value() or self.obj.rail
        ground = self.ground.value() or self.obj.ground
        dlg = TargetCalculator(self, self.name.text().strip(),
                               probe_loads(self.panel.setup, rail, ground,
                                           self.panel.dc_result),
                               vrm_accuracies(self.panel.setup, rail))
        if dlg.exec() and dlg.target() is not None:
            self.apply_target(dlg.target())

    def apply_target(self, t):
        """Fill the target fields from a calculator result (which saves them)."""
        self.target.setValue(t.z_ohm * 1e3)
        self.target_f.setValue(t.f_hz / 1e3)

    def _load(self, p: Probe):
        self.name.setText(p.name)
        self.rail.set_value(p.rail)
        self.ground.set_value(p.ground)
        self.target.setValue((p.target_ohm or 0.0) * 1e3)
        self.target_f.setValue((p.target_hz or 0.0) / 1e3)
        self.points.setRowCount(0)
        reset_sort(self.points)
        for f_hz, z_ohm in sorted(p.target_points):
            self._add_point_row(f_hz, z_ohm)
        self.target_kind.setCurrentIndex(1 if p.target_custom else 0)
        self._target_rows()

    def _save(self, p: Probe):
        old = p.rail
        new_rail = self.rail.value() or p.rail
        typed = self.name.text().strip() or p.name
        if typed == p.rail.ref and new_rail.ref != p.rail.ref:
            typed = new_rail.ref              # the default name follows the picked part
            self.name.setText(typed)
        p.name = typed
        p.rail = new_rail
        p.ground = self.ground.value() or p.ground
        # New supply pins bring the same part's ground pins, unless the user picked
        # another part's: on the probe's ground net or the simulated one if it can, else
        # on the part's own ground net (kipi.dc.pi.autofill.ground_net).
        if (new_rail.ref, new_rail.net) != (old.ref, old.net) and p.ground.ref == old.ref:
            gnd = self._guess_ground(new_rail, prefer=[p.ground.net, self.panel.ground()])
            if gnd is not None and gnd != p.ground:
                p.ground = gnd
                self._show(self.ground, gnd)
        p.target_ohm = self.target.value() / 1e3 or None
        p.target_hz = self.target_f.value() * 1e3 or None
        p.target_custom = self.target_kind.currentData() == "custom"
        if p.target_custom and self.points.rowCount() == 0:
            # Switching to custom: start from the flat target's level.
            self._add_point_row(p.target_hz or 1e6, p.target_ohm or 0.01)
        p.target_points = self._points_value()


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class ACPanel(QWidget, PartSelection):
    solve_requested = Signal(object, str, str)     # PISetup, VRM name, ground net
    save_requested = Signal()
    result_selected = Signal(object)               # ACResult shown in the plot; None clears it
    setup_changed = Signal()                       # any edit to the setup
    results_saved = Signal(str)                    # path of a saved results file
    # The VRM, series link or probe selected in the setup: kind ("vrm", "link", "probe"),
    # name, and whether the user picked it; ("", "", False) for none (#96).
    part_selected = Signal(str, str, bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.snapshot: Optional[BoardSnapshot] = None
        self.setup = PISetup()
        self._nets: List[str] = []
        self.result = None
        self._results: Dict[str, object] = {}     # the last ACResult of each rail (VRM name)
        # The last DC solve, for the target calculator's DC drop (None until one is run).
        self.dc_result = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        row = QHBoxLayout()
        self.lbl_file = QLabel("No board", self)
        self.lbl_file.setStyleSheet("color: #999;")
        self.lbl_file.setWordWrap(True)
        b_save = QPushButton("Save", self)
        b_save.setToolTip("Save the setup next to the board (DC and AC share one file)")
        b_save.clicked.connect(self.save_requested.emit)
        row.addWidget(self.lbl_file, 1)
        row.addWidget(b_save)
        lay.addLayout(row)

        self.tabs = QTabWidget(self)
        self.vrm_list = ItemList(lambda: self.setup.vrms, VrmEditor(self, mode="ac"),
                                 self._new_vrm, PIPanel._describe_vrm, noun="VRM")
        self.link_list = ItemList(lambda: self.setup.links, LinkEditor(self, mode="ac"),
                                  self._new_link, self._describe_link, noun="series link")
        self.cap_list = ItemList(lambda: self.setup.caps, CapEditor(self), self._new_cap,
                                 self._describe_cap, noun="capacitor model")
        b_values = QPushButton("Models from values")
        b_values.setToolTip("Make one model per distinct value of the capacitors that have "
                            "none yet (capacitance read from the value; enter ESL and ESR)")
        b_values.clicked.connect(self._models_from_values)
        self.cap_list.layout().insertWidget(0, b_values)
        self.probe_list = ItemList(lambda: self.setup.probes, ProbeEditor(self),
                                   self._new_probe, self._describe_probe, noun="probe")
        # A probe on a DC load's pins, picked from a dropdown of the setup's loads and
        # its LDOs' and switchers' inputs.
        b_load = QPushButton("From DC load")
        b_load.setToolTip("Add a probe on the same supply and ground pins as a DC load, or "
                          "on an LDO's or switcher's input pins (one per phase)")
        menu = QMenu(b_load)
        menu.aboutToShow.connect(lambda: self._fill_load_menu(menu))
        b_load.setMenu(menu)
        self.probe_list.buttons.insertWidget(1, b_load)
        # Load models (#17): a part's targets in a file, for many probes at once.
        self.probe_list.buttons.addWidget(model_button(
            self, "probe", self.apply_load_model, self.add_probes_from_model,
            lambda: export_model(self, self.setup, self.snapshot)))
        self.tabs.addTab(self.vrm_list, "VRMs")
        self.tabs.addTab(self.link_list, "Series links")
        self.tabs.addTab(self.cap_list, "Capacitors")
        self.tabs.addTab(self.probe_list, "Probes")
        self.tabs.addTab(self._sweep_tab(), "Sweep")
        self.tabs.addTab(self._mesh_tab(), "Mesh")
        for i, text in enumerate((
                "The regulators, shared with DC. In AC they pick the rail to simulate.",
                "Parts in series between two nets, shared with DC; AC adds their ESL",
                "RLC models for the decoupling capacitors, and which parts use each",
                "Where the impedance is looked into (usually a device's power pins), "
                "with its target",
                "Frequency range and resolution of the sweep; capacitor DC bias, "
                "temperature and aging",
                "Mesh density and dielectric losses for the AC field solve")):
            self.tabs.setTabToolTip(i, text)
        for il in self._lists():
            il.changed.connect(self._on_changed)
        self._watch_part_lists()
        lay.addWidget(self.tabs, 3)

        self.lbl_errors = QLabel(self)
        self.lbl_errors.setWordWrap(True)
        self.lbl_errors.setStyleSheet("color: #ff8080;")
        lay.addWidget(self.lbl_errors)

        row = QHBoxLayout()
        self.cmb_rail = QComboBox(self)
        self.cmb_rail.setToolTip("The rail to simulate: the nets its VRM drives, plus the "
                                 "nets series links join to them")
        self.cmb_rail.currentIndexChanged.connect(self._rail_changed)
        self.cmb_gnd = QComboBox(self)
        make_searchable(self.cmb_gnd)
        self.cmb_gnd.setToolTip("The ground net the rail is simulated against")
        self.cmb_gnd.currentIndexChanged.connect(self._gnd_changed)
        self.btn_solve = QPushButton("Solve rail", self)
        self.btn_solve.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_solve.setToolTip("Save the setup, then sweep the impedance at every probe on "
                                  "this rail. Problems that stop the solve are listed above "
                                  "in red.")
        self.btn_solve.clicked.connect(self._solve)
        self.report_option = ReportOption("impedance plot, board map and tables, after "
                                          "each rail's solve", self._report_changed, self)
        self.chk_report = self.report_option.chk
        self.btn_report = QPushButton("Open report", self)
        self.btn_report.setToolTip("Open the shown rail's report in your browser or PDF "
                                   "viewer")
        self.btn_report.setVisible(False)
        self.btn_report.clicked.connect(self._open_report)
        row.addWidget(QLabel("Rail"))
        row.addWidget(self.cmb_rail, 2)
        row.addWidget(QLabel("Ground"))
        row.addWidget(self.cmb_gnd, 2)
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.btn_solve, 1)
        row.addWidget(self.report_option)
        row.addWidget(self.btn_report)
        lay.addLayout(row)

        g_res = QGroupBox("Results", self)
        lr = QVBoxLayout(g_res)
        self.tbl_probes = QTableWidget(0, 6, g_res)
        self.tbl_probes.setHorizontalHeaderLabels(
            ["Probe", "Min |Z| (mΩ)", "Worst |Z| (mΩ)", "at (MHz)", "Target (mΩ)", ""])
        self.tbl_probes.verticalHeader().setVisible(False)
        self.tbl_probes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_probes.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_probes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_probes.setToolTip("Worst |Z|: where the impedance comes closest to (or "
                                   "furthest over) the probe's target; the highest impedance "
                                   "in the sweep without one")
        make_sortable(self.tbl_probes)
        self.txt_notes = QLabel(g_res)
        self.txt_notes.setWordWrap(True)
        self.txt_notes.setTextFormat(Qt.RichText)
        self.txt_notes.setStyleSheet("color: #bbb;")
        row = QHBoxLayout()
        self.btn_export = QPushButton("Export CSV…", g_res)
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip("Write |Z|, R and X at every probe and sweep frequency to a "
                                   "CSV file")
        self.btn_export.clicked.connect(self._export_csv)
        self.btn_save = QPushButton("Save results…", g_res)
        self.btn_save.setEnabled(False)
        self.btn_save.setToolTip("Save every solved rail's results to reopen later with "
                                 "Open results… (in AC mode), without solving again")
        self.btn_save.clicked.connect(self._save_results)
        row.addStretch(1)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_export)
        lr.addWidget(self.tbl_probes)
        lr.addWidget(self.txt_notes)
        lr.addLayout(row)
        lay.addWidget(g_res, 1)
        self.setEnabled(False)

    def _lists(self):
        return (self.vrm_list, self.link_list, self.cap_list, self.probe_list)

    def _part_lists(self):
        return {"vrm": self.vrm_list, "link": self.link_list, "probe": self.probe_list}

    # ------------------------------------------------------------------ tabs
    def _sweep_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.setup.ac
        self.spin_fstart = _spin(0.001, 1e6, s.f_start_hz / 1e3, 3, " kHz", 1)
        self.spin_fstop = _spin(0.001, 1e5, s.f_stop_hz / 1e6, 3, " MHz", 10)
        self.spin_ppd = QSpinBox()
        self.spin_ppd.setRange(1, 2000)
        self.spin_ppd.setValue(s.points_per_decade)
        self.spin_fpd = QSpinBox()
        self.spin_fpd.setRange(0, 2000)
        self.spin_fpd.setValue(s.field_points_per_decade)
        self.spin_fpd.setSpecialValueText("every point")
        f.addRow("Start", self.spin_fstart)
        f.addRow("Stop", self.spin_fstop)
        f.addRow("Points per decade", self.spin_ppd)
        f.addRow("Field solves per decade", self.spin_fpd)
        tip(f, self.spin_fstart, "Lowest frequency of the sweep")
        tip(f, self.spin_fstop, "Highest frequency of the sweep")
        tip(f, self.spin_ppd, "Frequencies per decade in the plotted sweep")
        tip(f, self.spin_fpd, "Copper (field) solves per decade. The parts are evaluated "
                              "at every sweep point; the copper's response in between "
                              "is interpolated. 'Every point' solves the copper at each "
                              "sweep frequency (slowest, exact).")
        for wdg in (self.spin_fstart, self.spin_fstop, self.spin_ppd, self.spin_fpd):
            wdg.valueChanged.connect(self._ac_settings_changed)
        self.chk_bias = QCheckBox("Capacitor DC bias")
        self.chk_bias.setChecked(s.dc_bias)
        f.addRow(self.chk_bias)
        tip(f, self.chk_bias, "Derate the capacitor models that have a voltage rating "
                              "by the DC voltage across each part (from the last DC solve, "
                              "or else the rail's VRM set point). Untick to solve with every "
                              "model's capacitance as entered.")
        self.chk_bias.toggled.connect(self._ac_settings_changed)
        self.chk_mfg = QCheckBox("Capacitor manufacturing tolerance")
        self.chk_mfg.setChecked(s.mfg_tolerance)
        f.addRow(self.chk_mfg)
        tip(f, self.chk_mfg, "Solve the capacitor models that have a Manufacturing tolerance "
                             "at the low end of it, the worst case for a PDN: a model with a "
                             "±10 % tolerance keeps 90 % of its capacitance. Models without "
                             "one are unchanged.")
        self.chk_mfg.toggled.connect(self._ac_settings_changed)
        self.cmb_temp = QComboBox()
        for text, key in (("Not modeled", "none"), ("Fixed", "fixed"),
                          ("From DC thermal co-simulation", "thermal")):
            self.cmb_temp.addItem(text, key)
        self.cmb_temp.setCurrentIndex(max(self.cmb_temp.findData(s.temperature), 0))
        self.spin_temp = _spin(-55, 200, s.temperature_c, 1, " °C", 5)
        f.addRow("Temperature", self.cmb_temp)
        f.addRow("Board temperature", self.spin_temp)
        tip(f, self.cmb_temp, "The temperature of the copper (its resistivity) and of the "
                              "capacitors with a dielectric (their capacitance). Fixed: one "
                              "temperature for the whole board. From DC thermal "
                              "co-simulation: the copper temperatures of the last DC solve "
                              "with thermal co-simulation, or of DC results opened from a "
                              "file. Not modeled: copper at 20 °C and capacitors at their "
                              "rated values.")
        tip(f, self.spin_temp, "The board's temperature when Fixed, and where the thermal "
                               "co-simulation has none")
        self.cmb_temp.currentIndexChanged.connect(self._ac_settings_changed)
        self.spin_temp.valueChanged.connect(self._ac_settings_changed)
        self.chk_aging = QCheckBox("Capacitor aging")
        self.chk_aging.setChecked(s.aging)
        self.spin_hours = _spin(1, 1e7, s.aging_hours, 0, " h", 1000)
        self.spin_hours.setEnabled(s.aging)
        self.sld_hours = QSlider(Qt.Horizontal)
        self.sld_hours.setRange(0, HOURS_SLIDER_STEPS)
        self.lbl_years = QLabel()
        self.lbl_years.setMinimumWidth(80)
        self.w_hours = QWidget()
        row = QHBoxLayout(self.w_hours)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.sld_hours, 1)
        row.addWidget(self.lbl_years)
        f.addRow(self.chk_aging)
        f.addRow("Hours operated", self.spin_hours)
        f.addRow(self.w_hours)
        self.sld_hours.setToolTip("Drag to set the hours operated, from 1000 hours (no aging "
                                  "yet) to 1,000,000 on a log scale")
        tip(f, self.chk_aging, "Age the capacitor models that have a dielectric: each loses "
                               "its aging rate per decade of hours operated past 1000 hours")
        tip(f, self.spin_hours, "How long the capacitors have operated (8760 hours a year; "
                                "87600 is ten years). Heating a ceramic past its Curie point, "
                                "as in reflow, restarts its aging.")
        self.chk_aging.toggled.connect(self._show_aging)
        self.chk_aging.toggled.connect(self._ac_settings_changed)
        self.spin_hours.valueChanged.connect(self._ac_settings_changed)
        self.spin_hours.valueChanged.connect(self._sync_hours_slider)
        self.sld_hours.valueChanged.connect(self._hours_slid)
        self._sync_hours_slider()
        self._show_aging(s.aging)
        self.spin_temp.setEnabled(s.temperature != "none")
        return w

    def _show_aging(self, on: bool):
        """The hours operated count only while aging is ticked; the slider shows only then."""
        self.spin_hours.setEnabled(on)
        self.w_hours.setVisible(on)

    def _sync_hours_slider(self, *_):
        """Put the slider where the hours operated are, and give them in years."""
        h = self.spin_hours.value()
        lo, hi = HOURS_SLIDER_DECADES
        x = (np.log10(max(h, 1.0)) - lo) / (hi - lo)
        self.sld_hours.blockSignals(True)
        self.sld_hours.setValue(int(round(min(max(x, 0.0), 1.0) * HOURS_SLIDER_STEPS)))
        self.sld_hours.blockSignals(False)
        years = h / HOURS_PER_YEAR
        self.lbl_years.setText(f"{years:.3g} year{'' if years == 1 else 's'}")

    def _hours_slid(self, pos: int):
        lo, hi = HOURS_SLIDER_DECADES
        h = 10 ** (lo + (hi - lo) * pos / HOURS_SLIDER_STEPS)
        digits = int(np.floor(np.log10(h))) - 2              # three significant figures
        self.spin_hours.setValue(round(h, -digits))         # syncs the slider back

    def _mesh_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.setup.ac
        self.spin_edge = _spin(0.05, 10.0, s.max_edge_mm, 2, " mm", 0.1)
        self.spin_grading = _spin(0.0, 2.0, s.grading, 2, "", 0.05)
        self.spin_min_edge = _spin(0.005, 1.0, s.min_edge_mm, 3, " mm", 0.01)
        f.addRow("Max edge", self.spin_edge)
        f.addRow("Via grading", self.spin_grading)
        f.addRow("Min edge (at holes)", self.spin_min_edge)
        tip(f, self.spin_edge, art_tip("Largest triangle edge of the copper mesh. Smaller is "
                                       "more accurate and slower.", "mesh", "max"))
        tip(f, self.spin_grading, art_tip("How fast mesh elements grow away from plated holes "
                                          "(mm of edge per mm of distance)", "mesh", "grading"))
        tip(f, self.spin_min_edge, art_tip("Mesh edge length at plated holes, where via "
                                           "inductance is decided", "mesh", "min"))
        note = QLabel("The mesh decides the plane spreading and via inductance: elements grow "
                      "from 'min edge' at plated holes by 'via grading' per mm, up to 'max "
                      "edge'. Copper resistivity and barrel plating come from the DC setup; the "
                      "dielectric loss tangent and Default εr from the Stackup window.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        f.addRow(note)
        for wdg in (self.spin_edge, self.spin_grading, self.spin_min_edge):
            wdg.valueChanged.connect(self._ac_settings_changed)
        return w

    def _ac_settings_changed(self, *_):
        s = self.setup.ac
        s.f_start_hz = self.spin_fstart.value() * 1e3
        s.f_stop_hz = self.spin_fstop.value() * 1e6
        s.points_per_decade = self.spin_ppd.value()
        s.field_points_per_decade = self.spin_fpd.value()
        s.max_edge_mm = self.spin_edge.value()
        s.grading = self.spin_grading.value()
        s.min_edge_mm = self.spin_min_edge.value()
        s.dc_bias = self.chk_bias.isChecked()
        s.mfg_tolerance = self.chk_mfg.isChecked()
        s.aging = self.chk_aging.isChecked()
        s.aging_hours = self.spin_hours.value()
        s.temperature = self.cmb_temp.currentData() or "none"
        s.temperature_c = self.spin_temp.value()
        self.spin_temp.setEnabled(s.temperature != "none")
        self._on_changed()

    def _load_ac_settings(self):
        s = self.setup.ac
        for wdg, v in ((self.spin_fstart, s.f_start_hz / 1e3), (self.spin_fstop, s.f_stop_hz / 1e6),
                       (self.spin_ppd, s.points_per_decade),
                       (self.spin_fpd, s.field_points_per_decade),
                       (self.spin_edge, s.max_edge_mm), (self.spin_grading, s.grading),
                       (self.spin_min_edge, s.min_edge_mm), (self.spin_hours, s.aging_hours),
                       (self.spin_temp, s.temperature_c)):
            wdg.blockSignals(True)
            wdg.setValue(v)
            wdg.blockSignals(False)
        self.report_option.set_value(s.html_report, s.report_format)
        for chk, on in ((self.chk_bias, s.dc_bias),
                        (self.chk_mfg, s.mfg_tolerance), (self.chk_aging, s.aging)):
            chk.blockSignals(True)
            chk.setChecked(on)
            chk.blockSignals(False)
        self._sync_hours_slider()
        self._show_aging(s.aging)
        self.cmb_temp.blockSignals(True)
        self.cmb_temp.setCurrentIndex(max(self.cmb_temp.findData(s.temperature), 0))
        self.cmb_temp.blockSignals(False)
        self.spin_temp.setEnabled(s.temperature != "none")

    # ------------------------------------------------------------------ data
    def nets(self) -> List[str]:
        return list(self._nets)

    def set_dc_result(self, tree):
        """The power-tree result the target calculator reads its DC drops from, or None."""
        self.dc_result = tree

    def set_board(self, snap: BoardSnapshot, setup: PISetup, file_label: str):
        self.snapshot = snap
        self.setup = setup
        self.dc_result = None
        self.lbl_file.setText(file_label)
        self._load_ac_settings()
        self.clear_results()
        self.setEnabled(True)
        self.reload()

    def set_label(self, text: str):
        self.lbl_file.setText(text)

    def set_nets(self, nets: List[str]):
        """Nets offered in the pin pickers (the ones ticked in the Board panel)."""
        if self.snapshot is None:
            return
        order = {n: i for i, n in enumerate(sorted_nets(self.snapshot))}
        nets = sorted(set(nets), key=lambda n: order.get(n, len(order)))
        if nets == self._nets:
            return
        self._nets = nets
        self.reload()

    def reload(self):
        """Refill everything from the setup (the DC panel may have edited the shared VRMs
        and series links)."""
        for il in self._lists():
            il.editor.refresh_nets(self._nets)
        for il in self._lists():
            il.reload()
        self._fill_rails()
        self._on_changed()

    def _fill_rails(self):
        s = self.setup.ac
        was = self.cmb_rail.currentData()
        want = s.rail or was
        self.cmb_rail.blockSignals(True)
        self.cmb_rail.clear()
        for v in self.setup.vrms:
            self.cmb_rail.addItem(f"{v.name}: {v.output.net}", v.name)
        i = self.cmb_rail.findData(want)
        self.cmb_rail.setCurrentIndex(i if i >= 0 else (0 if self.setup.vrms else -1))
        self.cmb_rail.blockSignals(False)
        names = {v.name for v in self.setup.vrms}
        for gone in set(self._results) - names:        # a VRM was removed or renamed
            del self._results[gone]
        self.btn_save.setEnabled(bool(self._results))
        self._fill_grounds()
        if self.cmb_rail.currentData() != was or (self.result is not None
                                                  and self.result.rail not in names):
            self._show_rail_result()

    def _fill_grounds(self):
        s = self.setup.ac
        vrm = self._vrm()
        want = s.ground or default_ground(vrm)
        kinds = classify_nets(self.snapshot) if self.snapshot is not None else {}
        nets = sorted(self._nets, key=lambda n: (kinds.get(n) != "ground",
                                                 self._nets.index(n)))
        if want and want not in nets:
            nets.append(want)
        self.cmb_gnd.blockSignals(True)
        self.cmb_gnd.clear()
        self.cmb_gnd.addItems(nets)
        i = self.cmb_gnd.findText(want, Qt.MatchFixedString) if want else -1
        if i < 0:
            i = next((k for k, n in enumerate(nets) if kinds.get(n) == "ground"), -1)
        self.cmb_gnd.setCurrentIndex(i)
        self.cmb_gnd.blockSignals(False)
        s.ground = self.ground()
        s.rail = self.cmb_rail.currentData() or ""

    def _vrm(self):
        name = self.cmb_rail.currentData()
        return next((v for v in self.setup.vrms if v.name == name), None)

    def ground(self) -> str:
        i = self.cmb_gnd.currentIndex()
        return self.cmb_gnd.itemText(i) if i >= 0 else ""

    def _rail_changed(self, _i):
        self.setup.ac.rail = self.cmb_rail.currentData() or ""
        self.setup.ac.ground = ""                      # follow the new VRM's ground
        self._fill_grounds()
        self._on_changed()
        self._show_rail_result()

    def _gnd_changed(self, _i):
        self.setup.ac.ground = self.ground()
        self._on_changed()

    def _on_changed(self):
        self.setup_changed.emit()
        if self.cmb_rail.count() != len(self.setup.vrms) or any(
                self.cmb_rail.itemData(i) != v.name for i, v in enumerate(self.setup.vrms)):
            self._fill_rails()                         # a VRM was added, removed or renamed
            return
        errors = validate_ac(self.setup, self.cmb_rail.currentData() or "", self.ground())
        self.lbl_errors.setText("\n".join(f"• {e}" for e in errors))
        self.btn_solve.setEnabled(not errors)

    def _solve(self):
        self.solve_requested.emit(self.setup, self.cmb_rail.currentData() or "",
                                  self.ground())

    def set_busy(self, busy: bool, queued: bool = False):
        self.btn_solve.setEnabled(not busy and not self.lbl_errors.text())
        self.btn_solve.setText(("Queued…" if queued else "Solving…") if busy else "Solve rail")

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

    def _new_link(self):
        net = self._first_net()
        return None if net is None else new_link(self.snapshot, self.setup, self._nets, net)

    def _new_cap(self):
        return CapModel(_unique("Model1", [m.name for m in self.setup.caps]))

    def _new_probe(self):
        vrm = self._vrm()
        net = vrm.output.net if vrm is not None else self._first_net()
        if net is None:
            return None
        gnd = self.ground() or next((n for n in self._nets if n != net), net)
        ref = self._first_part(net)
        gnd_parts = {r for r, _, _ in parts_on_net(self.snapshot, gnd)}
        # The busiest part on the rail that also has ground pins: most likely the load.
        for r, _, _ in parts_on_net(self.snapshot, net):
            if r in gnd_parts:
                ref = r
                break
        gref = ref if ref in gnd_parts else self._first_part(gnd)
        return Probe(_unique(ref or "PROBE1", [p.name for p in self.setup.probes]),
                     PinRef(ref, net), PinRef(gref, gnd))

    def apply_load_model(self, model=None):
        """Apply a load model file's targets (or ``model``'s) to the probes the user
        ticks."""
        model = model or open_model(self)
        if model is None:
            return
        probes = self.setup.probes
        if not probes:
            QMessageBox.information(self, "Load model", "There are no probes to apply it to; "
                                    "use Add probes from model file.")
            return
        dlg = ApplyDialog(model, [(p.name, p.rail) for p in probes], "probe", self)
        if not dlg.exec():
            return
        missed = [probes[row].name for row, i in dlg.chosen()
                  if not apply_to_probe(probes[row], model.supplies[i], self.setup)]
        self.probe_list.reload()
        self.probe_list.changed.emit()
        if missed:
            QMessageBox.information(
                self, "Load model", "No target was set for " + ", ".join(missed) + ": their "
                "supply in the model has no target, or a load step without an AC tolerance "
                "and no VRM driving the rail to take one from its DC tolerance.")

    def add_probes_from_model(self, model=None):
        """Add a load model's probes on the parts the user ticks."""
        model = model or open_model(self)
        if model is None:
            return
        parts = candidate_parts(model, self.snapshot)
        if not parts:
            QMessageBox.information(self, "Load model", "No part on the board has the pins of "
                                    "any of the model's supplies (adding probes needs its "
                                    "pins).")
            return
        dlg = PartsDialog(model, parts, "probe", self)
        if not dlg.exec():
            return
        added, no_ground = 0, []
        for ref in dlg.chosen():
            for s, rail, gnd in new_items(model, ref, part_nets(self.snapshot, ref)):
                gnd = gnd or guess_ground_pins(self.snapshot, self.setup, self._nets, rail)
                if gnd is None:
                    no_ground.append(f"{ref} {s.name}")
                    continue
                probe = Probe(_unique(f"{ref} {s.name}", [p.name for p in self.setup.probes]),
                              rail, gnd)
                apply_to_probe(probe, s, self.setup)
                self.setup.probes.append(probe)
                added += 1
        if added:
            self.probe_list.reload()
            self.probe_list.lst.setCurrentRow(self.probe_list.lst.count() - 1)
            self.probe_list.changed.emit()
        if no_ground:
            QMessageBox.information(self, "Load model", "No probe for " + ", ".join(no_ground)
                                    + ": no ground pins in the model, and none found on the "
                                    "part.")

    def _fill_load_menu(self, menu: QMenu):
        menu.clear()
        for load in self.setup.loads:
            menu.addAction(load.name, lambda load=load: self.probe_list.add(
                self.probe_from_load(load)))
        # An LDO's or switcher's input loads the rail feeding it (#117): one per phase.
        inputs = vrm_inputs(self.setup)
        if self.setup.loads and inputs:
            menu.addSeparator()
        for name, vrm, k in inputs:
            menu.addAction(name, lambda vrm=vrm, k=k: self.probe_list.add(
                self.probe_from_vrm_input(vrm, k)))
        if not self.setup.loads and not inputs:
            menu.addAction("No DC loads or regulator inputs in the setup").setEnabled(False)

    def probe_from_load(self, load: Load) -> Probe:
        """A probe on ``load``'s supply and ground pins, named after it. A load without
        ground pins gets its part's pins on its ground net, else the busiest part on the
        panel's ground."""
        return self._probe_on(load.name, load.supply, load.ground)

    def probe_from_vrm_input(self, vrm: Vrm, phase: int) -> Probe:
        """A probe on the input and ground pins of ``vrm``'s phase ``phase`` (from 0),
        named after it, as :meth:`probe_from_load` makes one for a DC load."""
        phases = vrm.all_phases()
        ph = phases[phase]
        return self._probe_on(vrm_input_name(vrm.name, phase, len(phases)), ph.input,
                              ph.ground)

    def _probe_on(self, name: str, supply: PinRef, ground: Optional[PinRef]) -> Probe:
        gnd = ground or guess_ground_pins(self.snapshot, self.setup, self._nets,
                                          supply, [self.ground()])
        if gnd is None:
            net = self.ground() or next((n for n in self._nets if n != supply.net), "")
            gnd = PinRef(self._first_part(net) if self.snapshot and net else "", net)
        return Probe(_unique(name, [p.name for p in self.setup.probes]),
                     copy.deepcopy(supply), copy.deepcopy(gnd))

    def _models_from_values(self):
        if self.snapshot is None:
            return
        assigned = {ref for m in self.setup.caps for ref in m.refs}
        by_value = {}
        for c in capacitor_candidates(self.snapshot, self._nets):
            if c.ref not in assigned:
                by_value.setdefault(c.value or "(no value)", []).append(c.ref)
        if not by_value:
            QMessageBox.information(self, "KiPI", "Every capacitor between ticked nets "
                                                  "already has a model.")
            return
        taken = [m.name for m in self.setup.caps]
        for value, refs in sorted(by_value.items()):
            name = _unique(value, taken)
            taken.append(name)
            farads = parse_capacitance(value) or 0.0
            rated = parse_voltage_rating(value)
            diel = parse_dielectric(value)
            # A rating turns on the DC bias estimate: only for class II ceramics.
            self.setup.caps.append(CapModel(name, farads, 0.0, 0.0, refs,
                                            rated_v=rated if is_class2(value) else None,
                                            dielectric=diel,
                                            aging_pct_per_decade=AGING_PCT_PER_DECADE.get(
                                                diel, 0.0)))
        self.cap_list.reload()
        self._on_changed()

    # ------------------------------------------------------------------ describe
    @staticmethod
    def _describe_link(k) -> str:
        r = "short" if k.resistance <= 0 else f"{k.resistance * 1e3:g} mΩ"
        esl = f", {k.esl_h * 1e9:g} nH" if k.esl_h else ""
        return f"{k.name}: {k.side_a.net} ↔ {k.side_b.net} ({r}{esl})"

    @staticmethod
    def _describe_cap(m: CapModel) -> str:
        vals = (f"{_fmt_farads(m.capacitance_f)}, {m.esl_h * 1e9:g} nH, {m.esr_ohm * 1e3:g} mΩ"
                if m.capacitance_f and m.esl_h and m.esr_ohm else "values needed")
        bias = f", DC bias {m.rated_v:g} V rated" if m.dc_bias else ""
        bias += f", {m.dielectric}" if m.dielectric else ""
        bias += f", ±{m.mfg_tolerance_pct:g} %" if m.mfg_tolerance_pct else ""
        return (f"{m.name}: {vals}{bias} "
                f"({len(m.refs)} part{'s' if len(m.refs) != 1 else ''})")

    @staticmethod
    def _describe_probe(p: Probe) -> str:
        if p.target_custom:
            n = len(p.target_points)
            target = f", custom target ({n} point{'s' if n != 1 else ''})"
        else:
            target = f", target {p.target_ohm * 1e3:g} mΩ" if p.target_ohm else ""
        return f"{p.name}: {p.rail.net} / {p.ground.net} ({p.rail.label()}){target}"

    # ------------------------------------------------------------------ results
    def clear_results(self):
        """Forget every rail's results (a new board)."""
        self._results.clear()
        self.btn_save.setEnabled(False)
        self._clear_table()

    def _clear_table(self):
        self.result = None
        reset_sort(self.tbl_probes)
        self.tbl_probes.setRowCount(0)
        self.txt_notes.clear()
        self.btn_export.setEnabled(False)
        self.btn_report.setVisible(False)

    def show_error(self, message: str):
        self.txt_notes.setText(f'<span style="color:#ff7070"><b>Cannot solve:</b><br>'
                               f'{html.escape(message).replace(chr(10), "<br>")}</span>')

    def _show_rail_result(self):
        """Show the selected rail's last results, or nothing if it has not been solved."""
        result = self._results.get(self.cmb_rail.currentData() or "")
        if result is not None:
            self._display(result)
        elif self.result is not None or self.txt_notes.text():
            self._clear_table()
            self.result_selected.emit(None)

    def show_result(self, result):
        """A finished solve: kept for its rail, and shown if that rail is still selected."""
        self._results[result.rail] = result
        self.btn_save.setEnabled(True)
        if result.rail == (self.cmb_rail.currentData() or ""):
            self._display(result)
        else:                                          # the rail was changed while solving
            note = (f"<b>{html.escape(result.rail)}</b> solved: select it under Rail to see "
                    f"its results.")
            text = self.txt_notes.text() if self.result is not None else ""
            self.txt_notes.setText(f"{text}<br>{note}" if text else note)

    def load_results(self, results: Dict[str, object]) -> List[str]:
        """Results opened from a file, in place of the ones held. Rails whose VRM is not in
        the setup cannot be selected, so they are left out; their names are returned. The
        selected rail's results are shown, or else the first opened rail is selected."""
        names = {v.name for v in self.setup.vrms}
        self._results = {k: r for k, r in results.items() if k in names}
        self.btn_save.setEnabled(bool(self._results))
        rail = self.cmb_rail.currentData() or ""
        if rail not in self._results and self._results:
            first = next(v.name for v in self.setup.vrms if v.name in self._results)
            self.cmb_rail.setCurrentIndex(self.cmb_rail.findData(first))   # shows it
        else:
            self._show_rail_result()
        return sorted(set(results) - names)

    def _display(self, result):
        self.result = result
        t = self.tbl_probes
        reset_sort(t)
        t.setRowCount(len(result.probes))
        for r, c in enumerate(result.probes):
            zmax, fmax = c.worst(result.freqs)
            ok = c.passed(result.freqs)
            set_row(t, r, [c.name, (f"{np.min(c.mag) * 1e3:.4g}", float(np.min(c.mag))),
                           (f"{zmax * 1e3:.4g}", zmax), (f"{fmax / 1e6:.4g}", fmax),
                           _target_text(c),
                           {None: "", True: "PASS", False: "FAIL"}[ok]])
            if ok is not None:
                t.item(r, 5).setForeground(QColor("#60d060" if ok else "#ff6060"))
        st = result.stats
        notes = [f"<b>{html.escape(result.rail)}</b>: {html.escape(', '.join(result.rail_nets))} "
                 f"against {html.escape(result.ground)}; {len(result.caps)} capacitor(s), "
                 f"{len(result.links)} series link(s); plane capacitance "
                 f"{result.plane_capacitance_f * 1e9:.3g} nF",
                 f"{st.n_unknowns} unknowns, {len(result.field_freqs)} field solves "
                 f"({st.method}), {len(result.freqs)} sweep points in {result.solve_s:.1f} s"]
        if getattr(result, "bias_note", ""):
            notes.append(html.escape(result.bias_note))
        if result.missing_caps:
            notes.append(f"⚠ {len(result.missing_caps)} capacitor(s) on this rail have no "
                         f"model and were left out: "
                         f"{html.escape(', '.join(result.missing_caps[:12]))}"
                         f"{'…' if len(result.missing_caps) > 12 else ''}")
        notes += [f"⚠ {html.escape(w)}" for w in result.warnings]
        self.txt_notes.setText("<br>".join(notes))
        self.btn_export.setEnabled(True)
        self.btn_report.setVisible(getattr(result, "report_path", None) is not None)
        self.result_selected.emit(result)

    def _report_changed(self, on: bool, fmt: str):
        self.setup.ac.html_report, self.setup.ac.report_format = on, fmt

    def _open_report(self):
        path = getattr(self.result, "report_path", None)
        if path:
            from kipi.gui.browser import open_in_browser
            open_in_browser(path)

    def _save_results(self):
        if not self._results or self.snapshot is None:
            return
        from kipi.ac.report import RESULTS_SUFFIX, save_results
        from kipi.core.naming import stamped
        start = self.snapshot.project_dir
        stem = f"{stamped(Path(self.snapshot.board_name).stem or 'kipi')}{RESULTS_SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(self, "Save AC results", f"{start}/{stem}"
                                              if start else stem,
                                              f"KiPI AC results (*{RESULTS_SUFFIX})")
        if not path:
            return
        try:
            save_results(path, self._results, self.snapshot.board_name, self.setup)
        except OSError as e:
            QMessageBox.warning(self, "KiPI", f"Saving failed: {e}")
            return
        self.results_saved.emit(path)

    def _export_csv(self):
        if self.result is None:
            return
        from kipi.ac.report import write_csv
        start = self.snapshot.project_dir if self.snapshot is not None else ""
        stem = f"{self.result.rail}_impedance.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export impedance", f"{start}/{stem}"
                                              if start else stem, "CSV (*.csv)")
        if path:
            try:
                write_csv(path, self.result)
            except OSError as e:
                QMessageBox.warning(self, "KiPI", f"Export failed: {e}")
