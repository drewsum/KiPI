"""The DC and AC panels' report option: write one after a solve, as a web page or a PDF
(#119)."""

from __future__ import annotations

from typing import Callable

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QWidget

FORMATS = (("HTML", "html", "an interactive web page"),
           ("PDF", "pdf", "a PDF, to print or attach: the tables and static plots"))


class ReportOption(QWidget):
    """[x] Report [HTML|PDF]: tells ``on_change(enabled, fmt)`` of every change made here."""

    def __init__(self, what: str, on_change: Callable[[bool, str], None], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.chk = QCheckBox("Report", self)
        self.chk.setToolTip(f"Also write a report ({what}) next to the board")
        self.cmb = QComboBox(self)
        for label, fmt, tip in FORMATS:
            self.cmb.addItem(label, fmt)
            self.cmb.setItemData(self.cmb.count() - 1, f"The report as {tip}", Qt.ToolTipRole)
        self.cmb.setToolTip("HTML: interactive plots, sortable tables and a notes box, in "
                            "your browser. PDF: the same tables with static plots, to print "
                            "or attach.")
        self.cmb.setEnabled(False)
        lay.addWidget(self.chk)
        lay.addWidget(self.cmb)
        self._on_change = on_change
        self.chk.toggled.connect(self._changed)
        self.cmb.currentIndexChanged.connect(self._changed)

    def set_value(self, enabled: bool, fmt: str) -> None:
        """Show a setup's choice without reporting it back."""
        for w in (self.chk, self.cmb):
            w.blockSignals(True)
        self.chk.setChecked(enabled)
        self.cmb.setCurrentIndex(max(self.cmb.findData(fmt), 0))
        self.cmb.setEnabled(enabled)
        for w in (self.chk, self.cmb):
            w.blockSignals(False)

    def fmt(self) -> str:
        return self.cmb.currentData()

    def _changed(self, *_):
        self.cmb.setEnabled(self.chk.isChecked())
        self._on_change(self.chk.isChecked(), self.fmt())
