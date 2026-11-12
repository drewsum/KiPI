"""The user guide: an HTML file that ships with the plugin, opened in the system browser."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from qtpy.QtWidgets import QMessageBox, QWidget

log = logging.getLogger("kipi")

_DOCS = Path(__file__).resolve().parents[2] / "docs"
# Shown when the local copy is missing (a partial install) or cannot be opened.
# GitHub Pages serves docs/ from main, so this is the rendered page, not its source.
ONLINE = "https://drewsum.github.io/KiPI/help.html"
# What PI analysis is and how KiPI's solvers work (#92).
SOLVER_GUIDE_ONLINE = "https://drewsum.github.io/KiPI/solver-guide.html"
# One board set up and solved in every mode, step by step (#242).
TUTORIAL_ONLINE = "https://drewsum.github.io/KiPI/tutorial.html"


def help_path() -> Optional[Path]:
    """The user guide, which lives beside the package in the plugin folder."""
    p = _DOCS / "help.html"
    return p if p.is_file() else None


def solver_guide_url() -> str:
    """The Solver Guide: the copy installed with KiPI, else the online one."""
    return _doc_url("solver-guide.html", SOLVER_GUIDE_ONLINE)


def tutorial_url() -> str:
    """The Tutorial: the copy installed with KiPI, else the online one."""
    return _doc_url("tutorial.html", TUTORIAL_ONLINE)


def _doc_url(name: str, online: str) -> str:
    p = _DOCS / name
    if p.is_file():
        from qtpy.QtCore import QUrl
        return QUrl.fromLocalFile(str(p)).toString()
    return online


def open_help(parent: Optional[QWidget] = None) -> bool:
    """Open the user guide in the default browser. Falls back to Python's webbrowser
    module, then to telling the user where the file is. True if a browser was asked to
    open it."""
    path = help_path()
    if path is None:
        log.warning("User guide not found under %s", _DOCS)
        QMessageBox.information(parent, "KiPI", f"The user guide is not installed with this "
                                                f"copy of KiPI. It is online at:\n\n{ONLINE}")
        return False
    from kipi.gui.browser import open_in_browser
    if open_in_browser(path):
        return True
    QMessageBox.information(parent, "KiPI", f"Could not open a web browser. The user guide "
                                            f"is at:\n\n{path}")
    return False
