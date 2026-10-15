"""Table editor for the via rules: plating (and the fill of a filled hole) by hole type
and drill size. First matching row wins."""

from __future__ import annotations

from typing import List

from qtpy.QtCore import Signal
from qtpy.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QVBoxLayout, QWidget,
)

from kipi.solver.via_barrel import FILLS, VIA_KINDS, ViaRule, default_via_rules

KIND_LABELS = {"through": "Through", "blind": "Blind", "buried": "Buried",
               "blind_buried": "Blind/buried", "micro": "Microvia", "pth": "Component hole",
               "any": "Any"}
FILL_LABELS = {"nonconductive": "Non-conductive epoxy", "copper": "Copper",
               "conductive": "Conductive epoxy"}
COLUMNS = ["Hole", "Drill from", "Drill to", "Plating", "If filled", "Fill resistivity"]


def _spin(lo, hi, value, decimals, suffix, empty=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    if empty:
        s.setSpecialValueText(empty)
    s.setValue(value)
    s.setKeyboardTracking(False)
    return s


def _combo(labels: dict, current: str) -> QComboBox:
    c = QComboBox()
    for key, text in labels.items():
        c.addItem(text, key)
    c.setCurrentIndex(max(0, c.findData(current)))
    return c


class ViaRulesEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("Drill = the hole in the laminate; plating grows inward. First matching "
                      "row wins. Defaults are IPC-6012 Class 2 minimum averages: set what your "
                      "fabricator states.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(160)
        buttons = QHBoxLayout()
        for text, slot in (("Add row", self._add), ("Remove row", self._remove),
                           ("Defaults", self._defaults)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        lay.addWidget(hint)
        lay.addWidget(self.table)
        lay.addLayout(buttons)

    # ------------------------------------------------------------ rows
    def _add_row(self, r: ViaRule):
        row = self.table.rowCount()
        self.table.insertRow(row)
        widgets = [
            _combo(KIND_LABELS, r.kind),
            _spin(0.0, 10.0, r.drill_min_mm, 3, " mm"),
            _spin(-0.001, 10.0, -0.001 if r.drill_max_mm is None else r.drill_max_mm, 3,
                  " mm", "no limit"),
            _spin(0.1, 200.0, r.plating_mm * 1000, 1, " µm"),
            _combo(FILL_LABELS, r.fill),
            _spin(0.0, 1.0, (r.fill_rho_ohm_mm or 0.0), 8, " Ω·mm", "not set"),
        ]
        for col, w in enumerate(widgets):
            sig = getattr(w, "valueChanged", None) or w.currentIndexChanged
            sig.connect(lambda *_: self.changed.emit())
            self.table.setCellWidget(row, col, w)

    def _cell(self, row: int, col: int):
        return self.table.cellWidget(row, col)

    def rule_at(self, row: int) -> ViaRule:
        dmax = self._cell(row, 2).value()
        rho = self._cell(row, 5).value()
        return ViaRule(kind=self._cell(row, 0).currentData(),
                       plating_mm=self._cell(row, 3).value() / 1000,
                       drill_min_mm=self._cell(row, 1).value(),
                       drill_max_mm=None if dmax < 0 else dmax,
                       fill=self._cell(row, 4).currentData(),
                       fill_rho_ohm_mm=rho or None)

    # ------------------------------------------------------------ public
    def set_rules(self, rules: List[ViaRule]):
        self.table.setRowCount(0)
        for r in rules:
            self._add_row(r)

    def rules(self) -> List[ViaRule]:
        return [self.rule_at(i) for i in range(self.table.rowCount())]

    # ------------------------------------------------------------ buttons
    def _add(self):
        self._add_row(ViaRule("any", 0.020))
        self.changed.emit()

    def _remove(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.changed.emit()

    def _defaults(self):
        self.set_rules(default_via_rules())
        self.changed.emit()


assert set(KIND_LABELS) == set(VIA_KINDS) and set(FILL_LABELS) == set(FILLS)
