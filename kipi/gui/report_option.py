"""The DC, AC and Coupled Noise panels' report button: write a report of the shown
results on demand (#185), as a web page or a PDF (#119)."""

from __future__ import annotations

from typing import Callable

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QComboBox, QHBoxLayout, QPushButton, QWidget

FORMATS = (("HTML", "html", "an interactive web page"),
           ("PDF", "pdf", "a PDF, to print or attach: the tables and static plots"))


class ReportOption(QWidget):
    """[Report] [HTML|PDF]: the button asks ``on_generate(fmt)`` for a report of the shown
    results, and only results there are to report enable it; ``on_format(fmt)`` hears of
    every format picked here."""

    def __init__(self, what: str, on_generate: Callable[[str], None],
                 on_format: Callable[[str], None], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.btn = QPushButton("Report", self)
        self._tip = (f"Write a report of the shown results ({what}) next to the board, and "
                     "open it")
        self.cmb = QComboBox(self)
        for label, fmt, tip in FORMATS:
            self.cmb.addItem(label, fmt)
            self.cmb.setItemData(self.cmb.count() - 1, f"The report as {tip}", Qt.ToolTipRole)
        self.cmb.setToolTip("HTML: interactive plots, sortable tables and a notes box, in "
                            "your browser. PDF: the same tables with static plots, to print "
                            "or attach.")
        lay.addWidget(self.btn)
        lay.addWidget(self.cmb)
        self._on_format = on_format
        self.btn.clicked.connect(lambda: on_generate(self.fmt()))
        self.cmb.currentIndexChanged.connect(lambda _i: self._on_format(self.fmt()))
        self.set_available(False)

    def set_value(self, fmt: str) -> None:
        """Show a setup's format without reporting it back."""
        self.cmb.blockSignals(True)
        self.cmb.setCurrentIndex(max(self.cmb.findData(fmt), 0))
        self.cmb.blockSignals(False)

    def set_available(self, available: bool) -> None:
        """Whether there are results to report: after a solve, or with saved results open."""
        self.btn.setEnabled(available)
        self.btn.setToolTip(self._tip if available else
                            self._tip + ". Solve, or open saved results, first.")

    def fmt(self) -> str:
        return self.cmb.currentData()
