"""The About dialog: what KiPI is, who wrote it, and the versions it is running on."""

from __future__ import annotations

import html
import json
import platform
import sys
import threading
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, List, Optional, Tuple

from qtpy.QtCore import QObject, QSettings, QSize, Qt, Signal
from qtpy.QtGui import QGuiApplication, QMovie, QPixmap
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


_RESOURCES = Path(__file__).resolve().parents[2] / "resources"
ICON_SIZE = 96


def icon_path() -> Optional[Path]:
    """The plugin icon, which lives beside the package in the plugin folder."""
    p = _RESOURCES / "kipi_256.png"
    return p if p.is_file() else None


def animated_icon_path() -> Optional[Path]:
    """The icon with its planes flashing through the result maps (also the README logo)."""
    p = _RESOURCES / "kipi.gif"
    return p if p.is_file() else None


def versions(solver: bool = True) -> List[Tuple[str, str]]:
    """Python and the dependency versions; ``solver`` adds the direct solver backend,
    which imports the solver stack (slow the first time)."""
    rows = [("Python", f"{platform.python_version()} ({sys.platform})")]
    for dist in _DEPS:
        try:
            rows.append((dist, metadata.version(dist)))
        except metadata.PackageNotFoundError:
            pass
    if not solver:
        return rows
    try:
        from kipi.core.solver import direct
        rows.append(("Direct solver", direct.backend_note()))
    except Exception:          # the dialog must open whatever state the solver stack is in
        pass
    return rows


#: Clone and download counts, published by the repo's stats workflow. A public file: the
#: Traffic API behind the clone counts needs a token, and no token can ship in the plugin.
STATS_URL = "https://raw.githubusercontent.com/drewsum/KiPI/stats/stats.json"
STATS_TIMEOUT_S = 5.0
_STATS_KEY = "about/stats"


@dataclass(frozen=True)
class RepoStats:
    clones: int          # every clone since `since`
    since: str           # ISO date the clone history starts
    uniques_14d: int     # unique cloners over GitHub's 14-day window
    downloads: int       # release asset downloads, all releases

    def text(self) -> str:
        return (f"{self.clones:,} clones since {self.since} ({self.uniques_14d:,} unique in the "
                f"last 14 days) · {self.downloads:,} release downloads")


def parse_stats(data: Any) -> Optional[RepoStats]:
    """stats.json as the stats workflow writes it, or None if it is anything else."""
    try:
        c = data["clones"]
        return RepoStats(int(c["total"]), str(c["since"]), int(c["uniques_14d"]),
                         int(data["release_downloads"]["total"]))
    except (KeyError, TypeError, ValueError):
        return None


def fetch_stats(url: str = STATS_URL, timeout: float = STATS_TIMEOUT_S) -> Optional[dict]:
    """The published stats as raw JSON, or None if they cannot be had: offline, a firewall,
    a Python without CA certificates (common on macOS), or GitHub being down."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"KiPI/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(1 << 20))
    except Exception:          # any failure just leaves the stats out
        return None
    return data if parse_stats(data) is not None else None


def _settings() -> QSettings:
    return QSettings("KiPI", "KiPI")


def cached_stats() -> Optional[RepoStats]:
    """The stats last fetched, from any session, to show while (or instead of) fetching."""
    try:
        return parse_stats(json.loads(str(_settings().value(_STATS_KEY, ""))))
    except ValueError:
        return None


def _cache_stats(data: dict):
    _settings().setValue(_STATS_KEY, json.dumps(data))


class _StatsFetch(QObject):
    """Fetches the stats on a daemon thread; ``ready`` arrives on the UI thread."""

    ready = Signal(object)    # raw stats dict, or None

    def start(self):
        threading.Thread(target=lambda: self.ready.emit(fetch_stats()),
                         name="kipi-stats", daemon=True).start()


_fetched_this_session = False


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
        self.movie = self._animate(icon)
        path = icon_path()
        if self.movie is None and path is not None:
            icon.setPixmap(QPixmap(str(path)).scaled(
                ICON_SIZE, ICON_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation))
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

        # Repo statistics: the cached ones now, fresh ones when the fetch returns. Without
        # either the line stays hidden; nothing here can stop the dialog opening.
        self.stats_label = QLabel(self)
        self.stats_label.setWordWrap(True)
        self.stats_label.setToolTip("From the project's GitHub repository, updated daily")
        self._show_stats(cached_stats())
        self._fetch: Optional[_StatsFetch] = None
        global _fetched_this_session
        if not _fetched_this_session:
            _fetched_this_session = True
            self._fetch = _StatsFetch()
            self._fetch.ready.connect(self._stats_ready)
            self._fetch.start()

        col = QVBoxLayout()
        col.addWidget(text)
        col.addWidget(self.stats_label)
        col.addStretch(1)

        top = QHBoxLayout()
        top.addWidget(icon)
        top.addSpacing(12)
        top.addLayout(col, 1)

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

    def _show_stats(self, stats: Optional[RepoStats]):
        if stats is not None:
            self.stats_label.setText(stats.text())
        self.stats_label.setVisible(bool(self.stats_label.text()))

    def _stats_ready(self, data: Optional[dict]):
        if data is None:
            return
        _cache_stats(data)
        try:
            self._show_stats(parse_stats(data))
        except RuntimeError:   # the dialog was deleted while the fetch ran
            pass

    def _animate(self, label: QLabel) -> Optional[QMovie]:
        """Play the animated icon, or None to fall back to the still one: the GIF can be
        missing, and some Qt builds ship without the GIF image plugin."""
        path = animated_icon_path()
        if path is None:
            return None
        movie = QMovie(str(path), parent=self)
        if not movie.isValid():
            return None
        movie.setScaledSize(QSize(ICON_SIZE, ICON_SIZE))
        label.setMovie(movie)
        movie.start()
        return movie
