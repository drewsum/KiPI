"""The About dialog: what KiPI is, who wrote it, and the versions it is running on."""

from __future__ import annotations

import html
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import List, Optional, Tuple

from qtpy.QtCore import Qt
from qtpy.QtGui import QGuiApplication, QPixmap
from qtpy.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from kipi import __version__

NAME = "KiPI"
DESCRIPTION = ("Power integrity analysis for KiCad: DC IR drop, current density, "
               "via currents and electro-thermal co-simulation in 3D, and AC PDN impedance "
               "over frequency.")
AUTHOR = "Drew Maatman"
EMAIL = "drewmaatman@gmail.com"
REPO = "https://github.com/drewsum/KiPI"
ISSUES = REPO + "/issues"
LICENSE = "GPL-3.0-or-later"

# Distributions whose versions are worth quoting in a bug report.
_DEPS = ["kicad-python", "PySide6-Essentials", "pyvista", "pyvistaqt", "numpy", "scipy",
         "shapely", "triangle", "pyamg", "pypardiso", "mkl", "plotly", "matplotlib"]


def icon_path() -> Optional[Path]:
    """The plugin icon, which lives beside the package in the plugin folder."""
    p = Path(__file__).resolve().parents[2] / "resources" / "kipi_256.png"
    return p if p.is_file() else None


def versions() -> List[Tuple[str, str]]:
    rows = [("Python", f"{platform.python_version()} ({sys.platform})")]
    for dist in _DEPS:
        try:
            rows.append((dist, metadata.version(dist)))
        except metadata.PackageNotFoundError:
            pass
    try:
        from kipi.core.solver import direct
        rows.append(("Direct solver", direct.backend_note()))
    except Exception:          # the dialog must open whatever state the solver stack is in
        pass
    return rows


def info_text() -> str:
    """Plain-text summary for pasting into an issue."""
    lines = [f"{NAME} {__version__}", REPO]
    lines += [f"{k}: {v}" for k, v in versions()]
    return "\n".join(lines)


class AboutDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"About {NAME}")

        icon = QLabel(self)
        path = icon_path()
        if path is not None:
            icon.setPixmap(QPixmap(str(path)).scaled(
                96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        icon.setAlignment(Qt.AlignTop)

        dep_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>&nbsp;&nbsp;{html.escape(v)}</td></tr>"
                           for k, v in versions())
        text = QLabel(self)
        text.setTextFormat(Qt.RichText)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        text.setWordWrap(True)
        text.setText(
            f"<h2 style='margin-bottom:0'>{NAME}</h2>"
            f"<p style='margin-top:2px'>Version {__version__}</p>"
            f"<p>{html.escape(DESCRIPTION)}</p>"
            f"<p>Author: {AUTHOR} (<a href='mailto:{EMAIL}'>{EMAIL}</a>)<br>"
            f"Source: <a href='{REPO}'>{REPO}</a><br>"
            f"Report a bug: <a href='{ISSUES}'>{ISSUES}</a><br>"
            f"License: {LICENSE}</p>"
            f"<p><b>Running on</b></p><table>{dep_rows}</table>")

        top = QHBoxLayout()
        top.addWidget(icon)
        top.addSpacing(12)
        top.addWidget(text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        copy = QPushButton("Copy version info", self)
        copy.setToolTip("Copy the versions above, for pasting into a bug report")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(info_text()))
        buttons.addButton(copy, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(buttons)
        self.setMinimumWidth(520)
