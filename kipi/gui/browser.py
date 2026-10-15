"""Open a local file (the user guide, an HTML report) in the default web browser."""

from __future__ import annotations

import logging
from pathlib import Path

from qtpy.QtCore import QUrl
from qtpy.QtGui import QDesktopServices

log = logging.getLogger(__name__)


def open_in_browser(path) -> bool:
    """Ask the desktop to open ``path``; fall back to Python's webbrowser module where Qt
    cannot (some Linux desktops without a handler). True if a browser was asked to open
    it."""
    path = Path(path)
    if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        return True
    try:
        import webbrowser
        if webbrowser.open(path.resolve().as_uri()):
            return True
    except Exception as e:  # noqa: BLE001 - any browser failure is only logged
        log.warning("Could not open %s: %s", path, e)
    return False
