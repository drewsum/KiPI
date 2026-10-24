"""The Split across nets dialog (#182): one part's total current, shared out as a load on
each of the supply nets the user ticks."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.model import PISetup
from kipi.dc.pi.split import PER_NET, PER_PIN, existing_load, split_current, supply_nets
from kipi.gui.tooltip_art import art_tip
from kipi.gui.unit_spin import UnitSpinBox

COLUMNS = ["Net", "Pins", "Current", "Load"]


class SplitLoadDialog(QDialog):
    """Pick a part, its total current and the nets to share it over; :meth:`chosen` is
    then {net: amps}."""

    def __init__(self, snap: Optional[BoardSnapshot], setup: PISetup,
                 parts: Sequence[Tuple[str, str]], ticked_nets: Sequence[str],
                 ref: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Split a load across nets")
        self.snap, self.setup, self.ticked = snap, setup, set(ticked_nets)
        self._pins: Dict[str, List[str]] = {}
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.part = QComboBox(self)
        for r, value in parts:
            self.part.addItem(f"{r}  {value}".strip(), r)
        from kipi.dc.gui.pi_panel import make_searchable
        make_searchable(self.part)
        self.total = UnitSpinBox()
        self.total.setShownDecimals(4)
        self.total.setRange(-1000, 1000)
        self.total.setSingleStep(0.01)
        self.total.setSuffix(" A")
        self.total.setKeyboardTracking(False)
        self.per = QComboBox(self)
        self.per.addItem("Equal per pin (by each net's pin count)", PER_PIN)
        self.per.addItem("Equal per net", PER_NET)
        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMinimumHeight(160)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #999;")
        form.addRow("Part", self.part)
        form.addRow("Total current", self.total)
        form.addRow("Split", self.per)
        lay.addLayout(form)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.note)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.part.setToolTip("The part drawing the current: its supply nets are listed below")
        self.total.setToolTip("The part's total current, over every net ticked below")
        self.per.setToolTip(art_tip(
            "Equal per pin: every supply pin draws the same current, so a net gets the "
            "total's share of its pins. Equal per net: each ticked net draws the same.",
            "split_nets", "per"))
        self.table.setToolTip(art_tip(
            "The part's supply nets (not its grounds). Tick the nets the total is shared "
            "over: each gets a load of its own, or, where the part already has a load on "
            "the net, that load's current is set.", "split_nets", "nets"))
        if ref is not None and self.part.findData(ref) >= 0:
            self.part.setCurrentIndex(self.part.findData(ref))
        self.part.currentIndexChanged.connect(self._fill)
        self.total.valueChanged.connect(self._update)
        self.per.currentIndexChanged.connect(self._update)
        self.table.itemChanged.connect(self._update)
        self._fill()

    def ref(self) -> str:
        return self.part.currentData() or ""

    def _fill(self, *_):
        ref = self.ref()
        self._pins = supply_nets(self.snap, self.setup, ref)
        loads = [existing_load(self.setup, ref, n) for n in self._pins]
        known = sum(ld.total_current() for ld in loads if ld is not None)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for net, pins in self._pins.items():
            r = self.table.rowCount()
            self.table.insertRow(r)
            item = QTableWidgetItem(net)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            load = existing_load(self.setup, ref, net)
            on = net in self.ticked or load is not None
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            if net not in self.ticked:
                item.setToolTip("Not ticked in the Board panel: tick it there to solve it")
            self.table.setItem(r, 0, item)
            for col, text in ((1, str(len(pins))), (3, load.name if load else "new")):
                self.table.setItem(r, col, QTableWidgetItem(text))
                self.table.item(r, col).setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 2, QTableWidgetItem(""))
            self.table.item(r, 2).setFlags(Qt.ItemIsEnabled)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.total.blockSignals(True)
        self.total.setValue(known or self.total.value() or 1.0)
        self.total.blockSignals(False)
        self._update()

    def _checked(self) -> List[str]:
        return [self.table.item(r, 0).text() for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.Checked]

    def chosen(self) -> Dict[str, float]:
        """{net: amps} for the ticked nets."""
        nets = self._checked()
        return split_current({n: self._pins[n] for n in nets}, self.total.value(),
                             self.per.currentData())

    def _update(self, *_):
        shares = self.chosen()
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            net = self.table.item(r, 0).text()
            self.table.item(r, 2).setText(f"{shares[net]:.4g} A" if net in shares else "")
        self.table.blockSignals(False)
        off = [n for n in shares if n not in self.ticked]
        notes = []
        if not self._pins:
            notes.append("This part has no supply nets.")
        if off:
            notes.append(f"Not ticked in the Board panel, so not solved until it is: "
                         f"{', '.join(off)}.")
        self.note.setText(" ".join(notes))
        self.note.setVisible(bool(notes))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(shares))
