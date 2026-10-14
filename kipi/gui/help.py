"""The user guide: an HTML file that ships with the plugin, opened in the system browser."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from qtpy.QtCore import QUrl
from qtpy.QtGui import QDesktopServices
from qtpy.QtWidgets import QMessageBox, QWidget

from kipi.gui.about import REPO

log = logging.getLogger("kipi")

_DOCS = Path(__file__).resolve().parents[2] / "docs"
# Shown when the local copy is missing (a partial install) or cannot be opened.
ONLINE = REPO + "/blob/main/docs/help.html"


def help_path() -> Optional[Path]:
    """The user guide, which lives beside the package in the plugin folder."""
    p = _DOCS / "help.html"
    return p if p.is_file() else None


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
    if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        return True
    try:
        import webbrowser
        if webbrowser.open(path.as_uri()):
            return True
    except Exception as e:  # noqa: BLE001 - any browser failure ends in the message below
        log.warning("Could not open the user guide: %s", e)
    QMessageBox.information(parent, "KiPI", f"Could not open a web browser. The user guide "
                                            f"is at:\n\n{path}")
    return False
