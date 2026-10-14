"""The AC setup panel, shown in place of the DC power-tree panel in AC mode.

A placeholder until AC analysis exists: it says what the mode will do and that the DC
setup (VRMs, loads, thermal settings) is kept and comes back in DC mode.
"""

from __future__ import annotations

from typing import Optional

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QLabel, QVBoxLayout, QWidget

TITLE = "AC setup"


class ACPanel(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        text = QLabel(self)
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        text.setText(
            "<h3>AC analysis is coming in a later version</h3>"
            "<p>AC mode will compute the impedance of the power distribution network, "
            "seen from probe points on the copper, swept over frequency.</p>"
            "<p>VRMs, loads and thermal settings belong to the DC analysis. Your DC setup "
            "is kept as it is; switch back to <b>DC</b> in the toolbar to use it.</p>")
        lay = QVBoxLayout(self)
        lay.addWidget(text)
        lay.addStretch(1)
