"""The Stackup pane: the board's copper and dielectric thicknesses, editable (#61).

Locked (the default), it shows the thicknesses read from KiCad. Unlocked, each thickness
can be changed to compare stackups; every analysis then uses the edited stackup, and
"Copy to KiCad" carries it back to the board.
"""

from __future__ import annotations

from typing import List, Optional

from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtGui import QColor, QIcon, QPixmap
from qtpy.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from kipi.core.kicad_io.stackup_write import manual_rows
from kipi.core.model.board import CopperLayer, Stackup
from kipi.core.model.stackup import restack, stack_order, thicknesses
from kipi.gui.unit_spin import UnitSpinBox

MIN_UM = 1.0
MAX_UM = 10000.0


class StackupPanel(QWidget):
    """Edits a board's stackup thicknesses. ``stackup_changed`` carries the stackup the
    analyses should use: the imported one while locked, else the edited one."""

    stackup_changed = Signal(object)
    copy_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._imported: Optional[Stackup] = None
        self._current: Optional[Stackup] = None
        self._spins: List[UnitSpinBox] = []
        self._timer = QTimer(self)          # one update after a burst of edits
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._emit_edit)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.chk_lock = QCheckBox("Lock to imported values", self)
        self.chk_lock.setChecked(True)
        self.chk_lock.setToolTip("Use the thicknesses read from KiCad. Untick to try other "
                                 "copper and dielectric thicknesses; every analysis uses "
                                 "them, and the setup file keeps them.")
        self.chk_lock.toggled.connect(self._lock_toggled)
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Layer", "Thickness", "εr"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 10)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setToolTip("The stackup, top to bottom. Thicknesses take units and "
                              "prefixes: 35, 35um or 0.2mm.")
        self.lbl_total = QLabel(self)
        row = QHBoxLayout()
        self.btn_reset = QPushButton("Reset", self)
        self.btn_reset.setToolTip("Go back to the thicknesses read from KiCad")
        self.btn_reset.clicked.connect(self.reset)
        self.btn_copy = QPushButton("Copy to KiCad", self)
        self.btn_copy.setToolTip("Set these thicknesses in the board's stackup in KiCad. "
                                 "Where KiCad cannot take them through its API, KiPI lists "
                                 "them to enter in Board Setup.")
        self.btn_copy.clicked.connect(lambda: self.copy_requested.emit(self._current))
        row.addWidget(self.btn_reset, 1)
        row.addWidget(self.btn_copy, 1)
        lay.addWidget(self.chk_lock)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.lbl_total)
        lay.addLayout(row)
        self._update_enabled()

    # ------------------------------------------------------------------ state
    def set_stackup(self, imported: Stackup, current: Optional[Stackup] = None,
                    colors=None):
        """Show a board's stackup: ``imported`` as read from KiCad, ``current`` the one in
        use (edited, from the setup file), and ``colors`` {copper id: (r, g, b)}."""
        self._timer.stop()
        self._imported = imported
        self._current = current or imported
        self.chk_lock.blockSignals(True)
        self.chk_lock.setChecked(not self._current.edited)
        self.chk_lock.blockSignals(False)
        self._fill(colors or {})
        self._update_enabled()

    def stackup(self) -> Optional[Stackup]:
        return self._current

    def reset(self):
        """Back to the imported thicknesses (the lock is left as it is)."""
        if self._imported is None:
            return
        self._set_values(thicknesses(self._imported))
        self._timer.stop()
        self._apply(self._imported)

    def _fill(self, colors):
        st = self._current
        self._spins = []
        self.table.setRowCount(0)
        if st is None:
            self.lbl_total.clear()
            return
        names = {c.id: c.name for c in st.copper}
        order = stack_order(st)
        self.table.setRowCount(len(order))
        for i, e in enumerate(order):
            if isinstance(e, CopperLayer):
                item = QTableWidgetItem(e.name)
                if e.id in colors:
                    pm = QPixmap(12, 12)
                    pm.fill(QColor.fromRgbF(*colors[e.id]))
                    item.setIcon(QIcon(pm))
                er = ""
            else:
                label = e.material or "Dielectric"
                item = QTableWidgetItem(f"   {label}")
                item.setToolTip(f"Between {names.get(e.above_layer, 'the top')} and "
                                f"{names.get(e.below_layer, 'the bottom')}")
                er = f"{e.epsilon_r:g}" if e.epsilon_r > 0 else "—"
            self.table.setItem(i, 0, item)
            spin = UnitSpinBox(self.table)
            spin.setRange(MIN_UM, MAX_UM)
            spin.setShownDecimals(1)
            spin.setSuffix(" µm")
            spin.setSingleStep(1.0 if isinstance(e, CopperLayer) else 10.0)
            spin.setValue(e.thickness_mm * 1000)
            spin.setFrame(False)
            spin.valueChanged.connect(self._edited)
            self.table.setCellWidget(i, 1, spin)
            self._spins.append(spin)
            er_item = QTableWidgetItem(er)
            er_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 2, er_item)
        self._show_total()

    def _set_values(self, mm: List[float]):
        for spin, t in zip(self._spins, mm):
            spin.blockSignals(True)
            spin.setValue(t * 1000)
            spin.blockSignals(False)

    def _show_total(self):
        st, imp = self._current, self._imported
        if st is None:
            return
        text = f"Board {st.board_thickness_mm:.3f} mm"
        if imp is not None and st.edited:
            text += f" (KiCad {imp.board_thickness_mm:.3f} mm)"
        if not st.from_board:
            text += " — default stackup, KiCad gave none"
        self.lbl_total.setText(text)

    def _update_enabled(self):
        unlocked = not self.chk_lock.isChecked() and self._current is not None
        for spin in self._spins:
            spin.setReadOnly(not unlocked)
            spin.setButtonSymbols(UnitSpinBox.UpDownArrows if unlocked
                                  else UnitSpinBox.NoButtons)
        self.btn_reset.setEnabled(unlocked)
        self.btn_copy.setEnabled(unlocked and self._current is not None
                                 and self._current.from_board)

    # ------------------------------------------------------------------ edits
    def _lock_toggled(self, locked: bool):
        self._update_enabled()
        if locked:
            self.reset()

    def _edited(self, *_):
        self._timer.start()

    def _emit_edit(self):
        if self._imported is None or self.chk_lock.isChecked():
            return
        mm = [s.value() / 1000 for s in self._spins]
        if all(abs(a - b) < 1e-9 for a, b in zip(mm, thicknesses(self._imported))):
            self._apply(self._imported)
        else:
            self._apply(restack(self._imported, mm))

    def _apply(self, st: Stackup):
        if self._current is st or (self._current is not None and
                                   thicknesses(self._current) == thicknesses(st) and
                                   self._current.edited == st.edited):
            self._current = st
            self._show_total()
            return
        self._current = st
        self._show_total()
        self.stackup_changed.emit(st)


class ManualStackupDialog(QDialog):
    """The thicknesses to enter in KiCad by hand, when KiPI cannot set them itself."""

    def __init__(self, stackup: Stackup, reason: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Copy stackup to KiCad")
        self.rows = manual_rows(stackup)
        lay = QVBoxLayout(self)
        text = QLabel(f"KiPI could not set the stackup in KiCad: {reason}.<br><br>"
                      "In the PCB editor, open <b>File &gt; Board Setup &gt; Board Stackup "
                      "&gt; Physical Stackup</b> and enter these thicknesses. A dielectric "
                      "made of several sublayers keeps its sublayers' proportions.", self)
        text.setWordWrap(True)
        table = QTableWidget(len(self.rows), 2, self)
        table.setHorizontalHeaderLabels(["Layer", "Thickness"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for i, (name, t) in enumerate(self.rows):
            table.setItem(i, 0, QTableWidgetItem(name))
            table.setItem(i, 1, QTableWidgetItem(t))
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        btn_copy = buttons.addButton("Copy as text", QDialogButtonBox.ActionRole)
        btn_copy.setToolTip("Copy the table to the clipboard")
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text()))
        buttons.rejected.connect(self.reject)
        lay.addWidget(text)
        lay.addWidget(table, 1)
        lay.addWidget(buttons)
        self.resize(460, 420)

    def text(self) -> str:
        return "\n".join(f"{name}\t{t}" for name, t in self.rows)
