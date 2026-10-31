"""The About dialog: what KiPI is, who wrote it, and the versions it is running on."""

from __future__ import annotations

import html
import json
import os
import platform
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, List, Optional, Tuple

from qtpy.QtCore import QObject, QSettings, Qt, Signal
from qtpy.QtGui import QGuiApplication
from qtpy.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from kipi import __version__
from kipi.gui.help import solver_guide_url
from kipi.host import Host, current as current_host

NAME = "KiPI"
DESCRIPTION = ("Power integrity analysis for KiCad: DC IR drop, current density, "
               "via currents and electro-thermal co-simulation in 3D, and AC PDN impedance "
               "over frequency. "
               "The author is not interested in donations, but if you want to support the "
               "project, please reach out and tell me how you are using it! I'd love to hear from you. "
               " ")
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
    """The icon with its planes flashing through the result maps: the README logo, and
    what the splash and About play if they cannot read the frame strip (see logo.py)."""
    p = _RESOURCES / "kipi.gif"
    return p if p.is_file() else None


def versions(solver: bool = True) -> List[Tuple[str, str]]:
    """Python and the dependency versions; ``solver`` adds the direct solver backend,
    which imports the solver stack (slow the first time)."""
    rows = [("Mode", current_host().label),
            ("Python", f"{platform.python_version()} ({sys.platform})")]
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
    downloads: int       # release zip downloads, all releases

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


#: The newest tagged release, from GitHub's API (no token needed for a public repo).
RELEASES_URL = "https://api.github.com/repos/drewsum/KiPI/releases/latest"


def version_tuple(text: str) -> Optional[Tuple[int, ...]]:
    """``"v0.7.2"`` -> (0, 7, 2); None if it is not a plain version."""
    parts = text.strip().lstrip("vV").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def fetch_latest_release(url: str = RELEASES_URL,
                         timeout: float = STATS_TIMEOUT_S) -> Optional[Tuple[str, str]]:
    """``(tag, release page)`` of the newest release, or None if GitHub cannot be asked:
    offline, a firewall, its rate limit, or a Python without CA certificates."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"KiPI/{__version__}",
                                                   "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(1 << 20))
        return str(data["tag_name"]), str(data.get("html_url") or REPO + "/releases")
    except Exception:          # any failure is said in the dialog instead
        return None


def update_text(latest: Optional[Tuple[str, str]], current: str = __version__) -> str:
    """What the About dialog says once it has checked for updates (rich text)."""
    if latest is None:
        return ("Could not reach GitHub to check for updates; see "
                f"<a href='{REPO}/releases'>the releases</a>.")
    tag, url = latest
    new, have = version_tuple(tag), version_tuple(current)
    if new is not None and have is not None and new > have:
        return (f"KiPI {html.escape(tag)} is available: "
                f"<a href='{html.escape(url)}'>download it</a>.")
    return f"KiPI {html.escape(current)} is up to date (the latest release is {html.escape(tag)})."


class _UpdateCheck(QObject):
    """Asks GitHub for the newest release on a daemon thread; ``ready`` arrives on the UI
    thread."""

    ready = Signal(object)    # (tag, url), or None

    def start(self):
        threading.Thread(target=lambda: self.ready.emit(fetch_latest_release()),
                         name="kipi-update-check", daemon=True).start()


def launcher_command() -> Optional[List[str]]:
    """The command that opens the launcher's setup once this KiPI has closed, or None if
    the launcher did not start this KiPI (as a plugin, or with ``python -m kipi``)."""
    launcher = os.environ.get("KIPI_LAUNCHER", "")
    python = os.environ.get("KIPI_LAUNCHER_PYTHON", "")
    if current_host() is not Host.STANDALONE or not (launcher and python):
        return None
    if not (Path(launcher).is_file() and Path(python).is_file()):
        return None
    pythonw = Path(python).with_name("pythonw.exe")
    if sys.platform == "win32" and pythonw.is_file():
        python = str(pythonw)                    # no console window
    script = Path(launcher).with_suffix(".pyw")  # the setup window, no terminal
    if not script.is_file():
        script = Path(launcher)
    # A venv's python.exe on Windows is a small program that starts the real Python as a
    # child; the environment is free only once both have exited.
    pids = [os.getpid()] + ([os.getppid()] if sys.platform == "win32" else [])
    return [python, str(script), "--setup"] + [a for p in pids for a in ("--after-pid", str(p))]


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
        from kipi.gui import logo     # it imports this module
        self.movie = logo.animate(icon, ICON_SIZE, self)
        if self.movie is None:
            logo.still(icon, ICON_SIZE)
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
            f"<p style='margin-top:4px; margin-bottom:4px;'>{html.escape(DESCRIPTION)}<br><br>"
            f"<a href='{html.escape(solver_guide_url())}'>Solver Guide</a>: what power "
            f"integrity analysis is and how the solvers work</p>"
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
        # What Check for updates found; hidden until it is asked.
        self.update_label = QLabel(self)
        self.update_label.setTextFormat(Qt.RichText)
        self.update_label.setOpenExternalLinks(True)
        self.update_label.setWordWrap(True)
        self.update_label.setVisible(False)
        col.addWidget(self.update_label)
        self._update_check: Optional[_UpdateCheck] = None
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
        self.btn_update = QPushButton("Check for updates", self)
        self.btn_update.setToolTip("Ask GitHub whether a newer KiPI has been released")
        self.btn_update.clicked.connect(self.check_for_updates)
        buttons.addButton(self.btn_update, QDialogButtonBox.ActionRole)
        if launcher_command() is not None:
            manage = QPushButton("Manage dependencies…", self)
            manage.setToolTip("Close KiPI and open its setup, to add or remove PARDISO or "
                              "reinstall KiPI's packages; KiPI starts again afterwards")
            manage.clicked.connect(self._manage_dependencies)
            buttons.addButton(manage, QDialogButtonBox.ActionRole)
        elif current_host() is Host.PLUGIN:
            manage = QPushButton("Manage dependencies…", self)
            manage.setToolTip("Add or remove PARDISO, the optional faster solver, in KiPI's "
                              "plugin environment")
            manage.clicked.connect(self._manage_plugin_dependencies)
            buttons.addButton(manage, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(buttons)
        self.setMinimumWidth(520)

    def check_for_updates(self):
        """Ask GitHub for the newest release, on a thread; the answer shows below."""
        self.btn_update.setEnabled(False)
        self.update_label.setText("Checking for updates…")
        self.update_label.setVisible(True)
        self._update_check = _UpdateCheck()
        self._update_check.ready.connect(self._update_ready)
        self._update_check.start()

    def _update_ready(self, latest):
        try:
            self.update_label.setText(update_text(latest))
            self.btn_update.setEnabled(True)
        except RuntimeError:   # the dialog was deleted while the check ran
            pass

    def _manage_dependencies(self):
        if QMessageBox.question(
                self, "Manage dependencies",
                "KiPI closes and its setup opens, where you can add or remove PARDISO. KiPI "
                "starts again when the setup is done.\n\nSave your setup first if you have "
                "unsaved changes. Close KiPI now?") != QMessageBox.Yes:
            return
        cmd = launcher_command()
        try:
            flags = 0x00000008 if sys.platform == "win32" else 0    # DETACHED_PROCESS
            subprocess.Popen(cmd, close_fds=True, creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as e:
            QMessageBox.warning(self, "KiPI", f"Could not start KiPI's setup: {e}")
            return
        self.accept()
        QApplication.instance().closeAllWindows()

    def _manage_plugin_dependencies(self):
        from kipi.gui import pardiso_setup
        pardiso_setup.manage(self)

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
