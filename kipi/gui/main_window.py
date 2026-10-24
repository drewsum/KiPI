# Portions Copyright (c) 2026 Tomachie LLC.
"""KiPI main window: board browser, 3D copper view and live log."""

from __future__ import annotations

import copy
import html
import logging
import math
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from qtpy.QtCore import QFileSystemWatcher, QSettings, Qt, QTimer
from qtpy.QtGui import QAction, QColor, QIcon, QPixmap, QTextCursor
from qtpy.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSizePolicy, QSlider,
    QScrollArea, QSplitter, QStackedWidget, QTabWidget, QTextBrowser, QToolBar, QToolButton,
    QVBoxLayout, QWidget,
)

from kipi import __version__
from kipi.ac.gui.panel import ACPanel
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.nets import classify_nets, sorted_nets
from kipi.dc.gui.pi_panel import PIPanel
from kipi.dc.pi.tree import fmt_drop
from kipi.gui.heartbeat import Heartbeat, busy_cursor
from kipi.gui.stackup_window import StackupWindow
from kipi.gui.tooltip_art import art_tip
from kipi.gui.viewer3d import Viewer3D
from kipi.gui.worker import QtProgress, Task
from kipi.host import Host, current as current_host
from kipi.sources import registry
from kipi.sources.base import BoardSource, InstalledTool

log = logging.getLogger("kipi")

_KIND_TAG = {"power": "PWR", "ground": "GND", "signal": ""}
_KIND_COLOR = {"power": "#e07050", "ground": "#70a0e0", "signal": "#a0a0a0"}
MAX_AUTO_NETS = 4
# A save can touch the board file several times (KiCad writes a new file and renames
# it); the window waits this long for the file to settle before offering to reload.
FILE_SETTLE_MS = 800
# Analysis modes, chosen in the toolbar: (key, label, setup dock title).
MODES = (("dc", "DC", "PI setup"), ("ac", "AC", "AC setup"),
         ("noise", "Noise", "Coupled Noise setup"))
# Keyboard shortcuts (#105). The view keys work while the 3D view has the focus, so that
# Home and letters still reach the lists and text boxes; the rest work anywhere in the
# window. Each is named in its control's tooltip and in the user guide.
VIEW_KEYS = {"iso": "Home", "top": "Z", "bottom": "Shift+Z"}
KEYS = {"open": "Ctrl+O", "reload": "F5", "save": "Ctrl+S", "screenshot": "F12",
        "results": "Ctrl+Shift+O", "stackup": "Ctrl+L", "power_tree": "Ctrl+T",
        "help": "F1", "solve": "Ctrl+Return", "cancel": "Esc", "dc": "Ctrl+1", "ac": "Ctrl+2",
        "noise": "Ctrl+3"}


def with_keys(tip: str, keys: str) -> str:
    """``tip`` with its shortcut after it, as the tooltips show it."""
    return f"{tip} ({keys})" if keys else tip
VIA_PICK_PX = 10      # a click this close to a via barrel on screen selects it


#: The size the window opens at when the screen has room for it.
PREFERRED_SIZE = (1500, 950)


def _scrolling(widget: QWidget) -> QScrollArea:
    """``widget`` in a scroll area: a dock that is shorter than its contents scrolls
    instead of making the whole window taller than the screen."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidget(widget)
    return area


def fit_to_screen(window: QMainWindow) -> None:
    """Keep the window, frame and title bar included, inside its screen's work area (the
    area the taskbar leaves): no larger than it, and moved onto it. A 2560 x 1600 laptop at
    175 % has 1463 x 874 logical pixels above the taskbar, less than the preferred size."""
    screen = window.screen() if hasattr(window, "screen") else None
    if screen is None:
        from qtpy.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    frame = window.frameGeometry()
    extra_w = frame.width() - window.width()
    extra_h = frame.height() - window.height()
    w = min(window.width(), area.width() - extra_w)
    h = min(window.height(), area.height() - extra_h)
    window.resize(max(w, 1), max(h, 1))
    frame = window.frameGeometry()
    x = min(max(frame.x(), area.left()), area.right() - frame.width() + 1)
    y = min(max(frame.y(), area.top()), area.bottom() - frame.height() + 1)
    window.move(max(x, area.left()), max(y, area.top()))


def write_report_file(write, path, progress):
    """``write(path)`` a solve's report; where a PDF cannot be made, the HTML report next to
    where it would have been (#119). The path written is returned."""
    from kipi.core.pdf import PdfError
    with progress.timed(f"Writing {path.suffix.lstrip('.').upper()} report"):
        try:
            return write(path)
        except PdfError as e:
            progress.warn(f"Could not write the PDF report ({e}); writing it as HTML instead")
    with progress.timed("Writing HTML report"):
        return write(path.with_suffix(".html"))


class MainWindow(QMainWindow):
    def __init__(self, source: Optional[BoardSource] = None, tool: Optional[InstalledTool] = None,
                 snapshot: Optional[BoardSnapshot] = None, host: Optional[Host] = None):
        """``source`` is where Load reads the board from (None: no board yet, or only
        snapshots); ``tool`` the design tool on this computer that gives layer colors and
        part models (None: KiPI's own colors, no models); ``host`` how KiPI was started
        (default :func:`kipi.host.current`), which picks the toolbar."""
        super().__init__()
        self.host = host or current_host()
        self._set_title("Power Integrity")
        self.resize(*PREFERRED_SIZE)
        self._fitted = False
        self._source = source
        self._tool = tool or InstalledTool()
        self.snapshot: Optional[BoardSnapshot] = None
        self.power_tree_window = None
        self._copper_cache: Dict[str, Dict[int, object]] = {}
        self._task: Optional[Task] = None
        self._resync_pending = False   # net checkboxes changed while a task was running
        self._sync_timer = QTimer(self)  # coalesce rapid checkbox clicks into one build
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(150)
        self._sync_timer.timeout.connect(self.sync_nets)
        self._render_timer = QTimer(self)  # coalesce renders while nets stream in
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._timed_render)
        from kipi.gui import app as kipi_app
        self._ui_beat_timer = QTimer(self)   # lets the task watchdog spot a stalled UI
        self._ui_beat_timer.setInterval(250)
        self._ui_beat_timer.timeout.connect(kipi_app.ui_alive)
        self._ui_beat_timer.start()
        self._checked_cache: Optional[frozenset] = None  # None = recompute from the list
        # Part 3D models: ref -> mesh, or None when the part has no model KiCad can find.
        self._model_cache: Dict[str, object] = {}
        self._model_task: Optional[Task] = None
        self._model_gen = 0                 # bumped per board; stale loads are dropped
        self._models_pending = False        # wanted parts changed while a load ran
        self._guess_wanted = False          # Guess models clicked: run once models are in
        self._model_source = None           # the board's ModelProvider, False if none
        self._closed = False
        self._layer_colors_from = None      # where the layer colors were last read from
        self._model_timer = QTimer(self)    # coalesce setup edits into one update
        self._model_timer.setSingleShot(True)
        self._model_timer.setInterval(300)
        self._model_timer.timeout.connect(self._sync_models)
        self._model_timer.timeout.connect(self._sync_pi_markers)
        self._last_fraction_t = 0.0
        self._picked = None   # ("pad"|"via", id)
        self._imported_stackup = None     # as read from KiCad, before any edits
        self._pending_stackup = None      # edited while a task ran; applied after
        self.mode = "dc"
        # The board's files, watched so a board saved in its editor can be read again.
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._board_file_changed)
        self._changed_timer = QTimer(self)
        self._changed_timer.setSingleShot(True)
        self._changed_timer.setInterval(FILE_SETTLE_MS)
        self._changed_timer.timeout.connect(self._offer_reload)
        self._asking_reload = False
        self._own_write_until = 0.0         # changes before this are KiPI's own saves

        # The central area: the 3D view and, in AC mode, the impedance plot and the board
        # map of the solve (#67); in Coupled Noise mode, the aggressor's waveforms (#69).
        from kipi.ac.gui.board_map import BoardMap
        from kipi.ac.gui.plot import ImpedancePlot
        from kipi.noise.gui.plot import NoisePlot
        self.views = QTabWidget(self)
        self.views.setDocumentMode(True)
        self.viewer = Viewer3D(self.views)
        self.plot = ImpedancePlot(self.views)
        self.board_map = BoardMap(self.views)
        self.noise_plot = NoisePlot(self.views)
        self.views.addTab(self.viewer, "3D view")
        self.views.addTab(self.plot, "Impedance")
        self.views.addTab(self.board_map, "Board map")
        self.views.addTab(self.noise_plot, "Waveforms")
        self.setCentralWidget(self.views)
        self.viewer.on_click(self._on_click)
        self.plot.cursor_moved.connect(self._show_cursor)
        self._set_plot_tabs("dc")

        self._build_toolbar()
        self._build_left_panel()
        self._build_log_dock()
        self._build_solve_dock()
        self._build_shortcuts()
        self.stackup_window = StackupWindow(self)
        self.stackup_window.stackup_changed.connect(self._stackup_changed)
        self.stackup_window.settings_changed.connect(self.solve_panel.setup_changed.emit)
        self._source_changed()
        self.statusBar().showMessage("Ready")

        if snapshot is not None:
            QTimer.singleShot(0, lambda: self._set_snapshot(snapshot))
        elif source is not None:
            QTimer.singleShot(0, self.reload_board)

    # ================================================================== UI
    def _build_toolbar(self):
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, tip="", keys="", view=False):
            a = QAction(text, self)
            a.setToolTip(with_keys(tip or text, keys))
            a.triggered.connect(slot)
            tb.addAction(a)
            if keys:
                a.setShortcut(keys)
            if view:                    # only while the 3D view (or the toolbar) has focus
                a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
                self.viewer.addAction(a)
            return a

        # Reload and Refill zones follow the board's source (see _source_changed). As a
        # plugin the board comes from KiCad, and snapshots open beside it; standalone,
        # Open reads a board file and Reload reads it again.
        if self.host is Host.STANDALONE:
            self.act_open = act("Open…", self.open_board, "Open a board file: " +
                                ", ".join(f.name for f in registry.FORMATS) +
                                "; the arrow lists the boards opened lately",
                                KEYS["open"])
            self.recent_menu = QMenu(self)
            self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
            self.act_open.setMenu(self.recent_menu)
            button = tb.widgetForAction(self.act_open)
            if isinstance(button, QToolButton):
                button.setPopupMode(QToolButton.MenuButtonPopup)
        self.act_reload = act("Reload", self.reload_board, "Re-read the board",
                              KEYS["reload"])
        self.chk_refill = QCheckBox("Refill zones", self)
        # A toolbar widget is shown and hidden through the action holding it.
        self._act_refill = tb.addWidget(self.chk_refill)
        tb.addSeparator()
        if self.host is Host.PLUGIN:
            act("Open snapshot…", self.open_snapshot,
                "Load a saved board snapshot (.json / .json.gz), without KiCad running",
                KEYS["open"])
        self.act_save = act("Save snapshot…", self.save_snapshot,
                            "Save the extracted board for offline use", KEYS["save"])
        tb.addSeparator()
        act("Top", lambda: self._show_3d(self.viewer.view_top),
            "Look down on the top of the board", VIEW_KEYS["top"], view=True)
        act("Bottom", lambda: self._show_3d(self.viewer.view_bottom),
            "Look up at the bottom of the board", VIEW_KEYS["bottom"], view=True)
        act("Iso", lambda: self._show_3d(self.viewer.view_iso), "Isometric view of the board",
            VIEW_KEYS["iso"], view=True)
        act("Screenshot…", self.save_screenshot,
            "Save the 3D view, or the impedance plot if it is showing, as a PNG",
            KEYS["screenshot"])
        tb.addSeparator()
        self.act_open_results = act("Open results…", self.open_results, keys=KEYS["results"])
        self._set_open_results_tip()
        self.act_stackup = act("Stackup", self.show_stackup,
                               "The board's layers: thicknesses, εr and copper resistivity, "
                               "for every analysis. Unlock to try another stackup.",
                               KEYS["stackup"])
        self.act_power_tree = act("Power tree", self.show_power_tree,
                                  "Block diagram of the setup: VRMs (by type), rails, series "
                                  "links, loads and capacitors, in the direction power flows",
                                  KEYS["power_tree"])
        tb.addSeparator()
        self.act_help = act("Help", self.show_help, "Open the KiPI user guide in your browser",
                            KEYS["help"])
        self.act_about = act("About", self.show_about, "Version, author and project links")

        # Analysis mode, at the far right.
        spacer = QWidget(tb)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel("Mode: ", tb))
        self.mode_buttons = QButtonGroup(self)
        tips = {"dc": "DC power integrity: IR drop, current density, electro-thermal",
                "ac": "AC power integrity: PDN impedance over frequency at probe points",
                "noise": "Coupled Noise: a buck or boost switcher's waveforms and the noise "
                         "it couples onto signal nets"}
        for key, label, _ in MODES:
            rb = QRadioButton(label, tb)
            rb.setObjectName(f"mode_{key}")
            rb.setToolTip(with_keys(tips[key], KEYS[key]))
            rb.setChecked(key == self.mode)
            rb.toggled.connect(lambda on, k=key: on and self.set_mode(k))
            self.mode_buttons.addButton(rb)
            tb.addWidget(rb)

    def _set_controls_enabled(self, on: bool):
        """The DC / AC switch, Import and Save are greyed out while a task runs (reading
        the board, building nets, a solve) and come back when it finishes or is
        cancelled; Import and Save also wait for a board."""
        for rb in self.mode_buttons.buttons():
            rb.setEnabled(on)
        self.setup_bar.setEnabled(on and self.snapshot is not None)

    def _build_shortcuts(self):
        """Keys with no toolbar button: Solve and Cancel."""
        def key(name, slot):
            a = QAction(self)
            a.setShortcut(KEYS[name])
            a.triggered.connect(slot)
            self.addAction(a)
            return a

        for mode, _, _ in MODES:
            key(mode, lambda _=False, m=mode: self._mode_button(m).setChecked(True))
        key("solve", self._solve_shortcut)
        # Esc is only taken while there is something to cancel; otherwise it still closes
        # popups and dialogs as usual.
        self.act_cancel = key("cancel", self._cancel_task)
        self.act_cancel.setEnabled(False)
        self.btn_cancel.setToolTip(with_keys(self.btn_cancel.toolTip(), KEYS["cancel"]))
        for panel in self._panels().values():
            panel.btn_solve.setToolTip(with_keys(panel.btn_solve.toolTip(), KEYS["solve"]))

    def _solve_shortcut(self):
        """Press the current mode's Solve button, when it can be pressed."""
        button = self._mode_panel().btn_solve
        if button.isEnabled():
            button.click()

    def set_mode(self, mode: str):
        """Switch the analysis mode: the setup dock shows that mode's panel. The DC setup
        and results are kept while in AC mode, and come back with DC."""
        if mode == self.mode:
            return
        if self._busy():
            QMessageBox.information(self, "KiPI", "Wait for the current operation to finish "
                                                  "before changing the analysis mode.")
            self._check_mode_button(self.mode)
            return
        self.mode = mode
        self._check_mode_button(mode)
        # VRMs, series links and capacitor models are shared: show the other panels' edits.
        if mode == "dc":
            self.solve_panel.reload_shared()
        else:
            self._mode_panel().reload()
        self.setup_stack.setCurrentWidget(self._mode_panel())
        self.solve_dock.setWindowTitle(next(t for k, _, t in MODES if k == mode))
        self._set_open_results_tip()
        self._set_plot_tabs(mode)
        self._sync_pi_markers()
        self._show_coupling(front=False)      # Coupled Noise's overlay, that mode only
        self._mode_panel()._part_selection(False)     # the part selected in this mode's lists
        if mode == "dc":
            if self._tree:
                self._show_group(self.solve_panel.cmb_group.currentText())
        else:
            self.viewer.clear_result()        # DC results do not apply; show the copper
            plot = self.plot if mode == "ac" else self.noise_plot
            if plot.result is not None:
                self.views.setCurrentWidget(plot)
        self.log(f"Analysis mode: {mode.upper()}")

    def _set_open_results_tip(self):
        self.act_open_results.setToolTip(with_keys(
            {"dc": "Reopen a saved DC or DC + thermal solve (.kipi-dc-results, "
                   ".kipi-dc-thermal-results); load the board first",
             "ac": "Reopen saved AC impedance results (.kipi-ac-results); load the board "
                   "first",
             "noise": "Reopen saved Coupled Noise results (.kipi-noise-results); load the "
                      "board first"}[self.mode],
            KEYS["results"]))

    def _set_plot_tabs(self, mode: str):
        """Show each mode's plot tabs only in that mode: the impedance and board map in AC
        mode, the waveforms in Coupled Noise mode. QTabWidget.setTabVisible needs Qt 5.15;
        older Qt disables the tabs instead."""
        tabs = {self.plot: mode == "ac", self.board_map: mode == "ac",
                self.noise_plot: mode == "noise"}
        for w, shown in tabs.items():
            i = self.views.indexOf(w)
            if hasattr(self.views, "setTabVisible"):
                self.views.setTabVisible(i, shown)
            else:
                self.views.setTabEnabled(i, shown)
            if not shown and self.views.currentIndex() == i:
                self.views.setCurrentWidget(self.viewer)

    def _show_3d(self, then=None):
        """Bring the 3D view to the front (from the impedance tab), then run ``then``."""
        self.views.setCurrentWidget(self.viewer)
        if then is not None:
            then()

    def _mode_button(self, mode: str) -> QRadioButton:
        return self.findChild(QRadioButton, f"mode_{mode}")

    def _check_mode_button(self, mode: str):
        rb = self._mode_button(mode)
        if rb is not None and not rb.isChecked():
            rb.blockSignals(True)
            rb.setChecked(True)          # the group unchecks the other button
            rb.blockSignals(False)

    def show_about(self):
        from kipi.gui.about import AboutDialog
        AboutDialog(self).exec()

    def show_stackup(self):
        self.stackup_window.show()
        self.stackup_window.raise_()
        self.stackup_window.activateWindow()

    def show_power_tree(self):
        from kipi.gui.power_tree import PowerTreeWindow
        if self.power_tree_window is None:
            self.power_tree_window = PowerTreeWindow(self)
        self.power_tree_window.show_tree(self.solve_panel.setup, self.snapshot)

    def _refresh_power_tree(self):
        w = self.power_tree_window
        if w is not None and w.isVisible():
            w.set_source(self.solve_panel.setup, self.snapshot)

    def show_help(self):
        from kipi.gui.help import open_help
        open_help(self)

    def _build_left_panel(self):
        panel = QWidget(self)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 6, 6, 6)

        # --- nets
        g_nets = QGroupBox("Nets", panel)
        ln = QVBoxLayout(g_nets)
        self.net_filter = QLineEdit(g_nets)
        self.net_filter.setPlaceholderText("Filter nets…")
        self.net_filter.setToolTip("Show only nets whose name contains this text")
        self.net_filter.textChanged.connect(self._apply_net_filter)
        self.chk_power_only = QCheckBox("Power && ground only", g_nets)
        self.chk_power_only.setChecked(True)
        self.chk_power_only.setToolTip("Hide signal nets that are not ticked. Power and ground "
                                       "nets are recognized by name, net class and zones.")
        self.chk_power_only.toggled.connect(self._apply_net_filter)
        self.net_list = QListWidget(g_nets)
        self.net_list.itemChanged.connect(self._on_net_item_changed)
        self.net_list.setToolTip("Tick the nets to analyze: they are drawn in 3D and offered "
                                 "in the setup's pin pickers")
        row = QHBoxLayout()
        btn_power = QPushButton("All power", g_nets)
        btn_power.setToolTip("Tick every power and ground net (keeps the nets already ticked)")
        btn_power.clicked.connect(self._check_power_nets)
        btn_none = QPushButton("None", g_nets)
        btn_none.setToolTip("Untick every net")
        btn_none.clicked.connect(lambda: self._set_checked(lambda it: False))
        row.addWidget(btn_power, 1)
        row.addWidget(btn_none, 1)
        ln.addWidget(self.net_filter)
        ln.addWidget(self.chk_power_only)
        ln.addWidget(self.net_list, 1)
        ln.addLayout(row)

        # --- layers
        g_layers = QGroupBox("Copper layers", panel)
        ll = QVBoxLayout(g_layers)
        self.layer_list = QListWidget(g_layers)
        self.layer_list.itemChanged.connect(self._on_layer_item_changed)
        self.layer_list.setToolTip("Show or hide each copper layer. Result colors stretch "
                                   "over the layers that are shown.")
        lrow = QHBoxLayout()
        btn_all_layers = QPushButton("All", g_layers)
        btn_all_layers.setToolTip("Show every copper layer")
        btn_all_layers.clicked.connect(lambda: self._set_layers_checked(True))
        btn_no_layers = QPushButton("None", g_layers)
        btn_no_layers.setToolTip("Hide every copper layer")
        btn_no_layers.clicked.connect(lambda: self._set_layers_checked(False))
        lrow.addWidget(btn_all_layers, 1)
        lrow.addWidget(btn_no_layers, 1)
        ll.addWidget(self.layer_list)
        ll.addLayout(lrow)

        # --- display options
        g_view = QGroupBox("Display", panel)
        fv = QFormLayout(g_view)
        self.cmb_color = QComboBox(g_view)
        self.cmb_color.addItems(["By layer", "By net"])
        self.cmb_color.setToolTip("Color the copper by layer, in the PCB editor's layer colors, "
                                  "or by net (plain geometry view)")
        self.cmb_color.currentIndexChanged.connect(
            lambda i: self.viewer.set_color_mode("net" if i == 1 else "layer"))
        self.spin_z = QDoubleSpinBox(g_view)
        self.spin_z.setRange(1.0, 100.0)
        self.spin_z.setSingleStep(1.0)
        self.spin_z.setValue(5.0)
        self.spin_z.setSuffix(" ×")
        self.spin_z.setToolTip("Stretch the stackup vertically to see the layers apart. "
                               "Only the drawing changes, not the solve.")
        self.spin_z.valueChanged.connect(self.viewer.set_zscale)
        self.viewer.set_zscale(self.spin_z.value())
        opac = QHBoxLayout()
        self.sld_opacity = QSlider(Qt.Horizontal, g_view)
        self.sld_opacity.setRange(5, 100)
        self.sld_opacity.setValue(100)
        self.sld_opacity.setToolTip("How solid the copper layers and pads are drawn. Lower "
                                    "it to see the layers beneath; vias stay solid.")
        self.lbl_opacity = QLabel("100%", g_view)
        self.lbl_opacity.setMinimumWidth(self.lbl_opacity.fontMetrics()
                                         .horizontalAdvance("100%") + 4)
        self.sld_opacity.valueChanged.connect(self._opacity_changed)
        opac.addWidget(self.sld_opacity, 1)
        opac.addWidget(self.lbl_opacity)
        feats = QVBoxLayout()
        feat_rows = (QHBoxLayout(), QHBoxLayout(), QHBoxLayout())
        for r in feat_rows:
            feats.addLayout(r)
        feat_tips = {"vias": "Show via and plated-hole barrels",
                     "pads": "Show pads", "board": "Show the board outline and laminate",
                     "mask": "Show the solder masks, open over the pads. They hide the "
                             "copper, so they start off. The copper and result views "
                             "each remember this; over results it starts off.",
                     "silk": "Show what the silkscreens print, as read from the board: "
                             "its drawings and text, off the pads. Off to start with. The "
                             "copper and result views each remember this; over results "
                             "it starts off.",
                     "pi": "Mark the parts of the current analysis on the nets shown: a "
                           "dot on each of their pads, larger on the rail and smaller on "
                           "ground, and the part's name. In DC mode its VRMs (green), "
                           "loads (cyan) and series links (magenta); in AC mode its VRMs, "
                           "series links, probes (violet) and capacitors with a model "
                           "(white), the probes in place of the loads, which no impedance "
                           "sweep uses. Over the copper only, not over results. Off to "
                           "start with. The VRM, load, series link, probe or capacitor "
                           "model selected in the setup is marked in amber, and clicking "
                           "its name selects it there.",
                     "models": "Show the 3D models of every part on the board. KiCad's "
                               "kicad-cli reads them from the saved board file; with none, "
                               "Guess models picks them from KiCad's library. The copper "
                               "and result views each remember this; over results they "
                               "start off."}
        for k, (name, label) in ((0, ("vias", "Vias")), (0, ("pads", "Pads")),
                                 (0, ("board", "Board")), (1, ("mask", "Mask")),
                                 (1, ("silk", "Silkscreen")), (2, ("pi", "PI Models")),
                                 (2, ("models", "3D Models"))):
            cb = QCheckBox(label, g_view)
            cb.setChecked(name not in ("pi", "mask", "silk"))
            cb.setToolTip(art_tip(feat_tips[name], "coatings", name) if name in ("mask", "silk")
                          else feat_tips[name])
            cb.toggled.connect(lambda v, n=name: self.viewer.set_feature_visible(n, v))
            feat_rows[k].addWidget(cb)
            if name == "pi":
                self.chk_pi = cb
            elif name in ("mask", "silk"):
                setattr(self, f"chk_{name}", cb)
        self.chk_models = cb
        self.chk_models.setVisible(self._tool.offers_part_models)
        self.chk_hide_non_included = QCheckBox("Hide non-simulated copper", g_view)
        self.chk_hide_non_included.setToolTip(
            "Show all copper by default. When ticked, show only nets included in the analysis.")
        self.chk_hide_non_included.toggled.connect(self.viewer.set_hide_non_included)
        feats.addWidget(self.chk_hide_non_included)
        # Only on request: a guessed model may be the wrong part (#130).
        self.btn_guess = QPushButton("Guess models", g_view)
        self.btn_guess.setToolTip(
            "Pick 3D models from KiCad's library for the parts that have none, by their "
            "footprint name: for a board with no KiCad board file (an ODB++ job, a "
            "snapshot away from its board), or parts whose models KiCad cannot find. "
            "It is a guess: the log names each model picked by size code or the closest "
            "name. Opening or reloading a board drops them.")
        self.btn_guess.setVisible(self._tool.offers_part_models)
        self.btn_guess.setEnabled(False)            # with a board loaded
        self.btn_guess.clicked.connect(self.guess_models)
        feat_rows[2].addWidget(self.btn_guess)
        self.chk_snap = QCheckBox("Snap to selected", g_view)
        self.chk_snap.setToolTip("Bring the 3D view to a VRM, load, series link, probe or "
                                 "capacitor model (all its parts) when it is clicked in the "
                                 "setup's lists. Untick it to keep "
                                 "the view where it is; the part is still marked in amber. "
                                 "Available while PI Models is ticked.")
        # QSettings may hand a bool back as "true" / "false" (an INI file, the registry).
        snap = QSettings("KiPI", "KiPI").value("view/snap_to_selected", True)
        self.chk_snap.setChecked(str(snap).lower() not in ("false", "0"))
        self.chk_snap.toggled.connect(
            lambda v: QSettings("KiPI", "KiPI").setValue("view/snap_to_selected", v))
        feats.addWidget(self.chk_snap)          # a row of its own: the dock is narrow
        self.chk_snap.setEnabled(self.chk_pi.isChecked())   # it snaps to the markers
        self.chk_pi.toggled.connect(self.chk_snap.setEnabled)
        self.chk_models.toggled.connect(lambda _: self._model_timer.start())
        # The copper and result views each keep their own Models, Mask and Silkscreen
        # settings (#144).
        self.viewer.result_shown.connect(self._sync_view_features)
        fv.addRow("Color", self.cmb_color)
        fv.addRow("Z scale", self.spin_z)
        fv.addRow("Opacity", opac)
        fv.addRow(feats)

        # --- info
        g_info = QGroupBox("Info", panel)
        li = QVBoxLayout(g_info)
        self.info = QTextBrowser(g_info)
        self.info.setOpenExternalLinks(False)
        self.info.setToolTip("What you last clicked in the 3D view, or the impedance "
                             "plot's cursor values")
        self.btn_select = QPushButton("Select", g_info)
        self.btn_select.setEnabled(False)
        self.btn_select.clicked.connect(self._select_in_source)
        li.addWidget(self.info, 1)
        li.addWidget(self.btn_select)

        split = QSplitter(Qt.Vertical, panel)
        for w in (g_nets, g_layers, g_view, g_info):
            split.addWidget(w)
        split.setSizes([360, 170, 110, 220])
        lay.addWidget(split)

        dock = QDockWidget("Board", self)
        dock.setObjectName("board_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(_scrolling(panel))
        dock.setMinimumWidth(300)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def _build_solve_dock(self):
        self.solve_panel = PIPanel(self)
        self.solve_panel.solve_requested.connect(self.run_solve)
        self.solve_panel.field_changed.connect(self._field_changed)
        self.solve_panel.scale_changed.connect(self._scale_changed)
        self.solve_panel.group_changed.connect(self._show_group)
        # Picking the group already selected sends no group_changed; after "Show copper"
        # that pick is how the results come back.
        self.solve_panel.cmb_group.activated.connect(self._group_activated)
        self.solve_panel.show_geometry.connect(self._show_geometry)
        self.solve_panel.arrows_toggled.connect(self.viewer.set_arrows_visible)
        self.solve_panel.via_selected.connect(self._show_via)
        # Each panel marks its own selection, while its mode is showing.
        self.solve_panel.part_selected.connect(
            lambda *a: self._pi_part_selected(*a) if self.mode == "dc" else None)
        self.solve_panel.export_requested.connect(self._export)
        self.solve_panel.report_requested.connect(self._dc_report)
        self._tree = None
        self._scenes = {}
        self._shown_group = ""
        self._setup_path = None
        self._saved = None              # the setup as its file has it
        self.ac_panel = ACPanel(self)
        self.ac_panel.solve_requested.connect(self.run_ac_solve)
        self.ac_panel.report_requested.connect(self._ac_report)
        self.ac_panel.result_selected.connect(self._show_ac_result)
        self.ac_panel.results_saved.connect(lambda path: self.log(f"Saved AC results to {path}"))
        self.ac_panel.part_selected.connect(
            lambda *a: self._pi_part_selected(*a) if self.mode == "ac" else None)
        from kipi.noise.gui.panel import NoisePanel
        self.noise_panel = NoisePanel(self)
        self.noise_panel.solve_requested.connect(self.run_noise_solve)
        self.noise_panel.report_requested.connect(self._noise_report)
        self.noise_panel.result_selected.connect(self._show_noise_result)
        self.noise_panel.victim_selected.connect(self._show_victim)
        self.noise_panel.coupling_changed.connect(self._show_coupling)
        self.noise_panel.results_saved.connect(
            lambda path: self.log(f"Saved Coupled Noise results to {path}"))
        self.noise_panel.part_selected.connect(
            lambda *a: self._pi_part_selected(*a) if self.mode == "noise" else None)
        for panel in self._panels().values():
            panel.setup_changed.connect(self._model_timer.start)
            panel.setup_changed.connect(self._refresh_power_tree)
            panel.setup_changed.connect(self._show_unsaved)
        # One setup dock: the setup file's bar, then the panel of the current analysis mode.
        # The bar stays outside the scrolling, so Import and Save are always in view.
        from kipi.gui.setup_file_bar import SetupFileBar
        self.setup_bar = SetupFileBar(self)
        self.setup_bar.import_requested.connect(self.import_pdn)
        self.setup_bar.save_requested.connect(self._save_setup)
        self.setup_bar.setEnabled(False)                       # until a board is read
        self.setup_stack = QStackedWidget(self)
        for panel in self._panels().values():
            self.setup_stack.addWidget(panel)
        holder = QWidget(self)
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.addWidget(self.setup_bar)
        col.addWidget(_scrolling(self.setup_stack), 1)
        dock = QDockWidget(MODES[0][2], self)
        dock.setObjectName("solve_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(holder)
        self.solve_dock = dock
        dock.setMinimumWidth(420)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self._result = None

    def _build_log_dock(self):
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.lbl_stage = QLabel("Idle", w)
        self.progress_bar = QProgressBar(w)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(14)
        self.btn_cancel = QPushButton("Cancel", w)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setToolTip("Stop the running operation")
        self.btn_cancel.clicked.connect(self._cancel_task)
        row.addWidget(self.lbl_stage, 2)
        row.addWidget(self.progress_bar, 3)
        row.addWidget(self.btn_cancel)
        self.log_view = QPlainTextEdit(w)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self.log_view.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        # A few lines always show: on a short screen the dock otherwise shrinks to its
        # progress bar and what an import or solve said cannot be seen.
        self.log_view.setMinimumHeight(self.log_view.fontMetrics().lineSpacing() * 4 + 8)
        lay.addLayout(row)
        lay.addWidget(self.log_view)
        dock = QDockWidget("Log", self)
        self.log_dock = dock
        dock.setObjectName("log_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(w)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [200], Qt.Vertical)
        self.heartbeat = Heartbeat(self)          # turns while any task runs (#111)
        self.statusBar().addPermanentWidget(self.heartbeat)

    # ============================================================ logging
    def showEvent(self, event):
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            fit_to_screen(self)

    def log(self, text: str, level: int = logging.INFO):
        color = {logging.WARNING: "#e0b050", logging.ERROR: "#ff6060"}.get(level)
        if text.startswith("== "):
            color = "#80c0ff"
        esc = html.escape(text)
        self.log_view.appendHtml(f'<span style="color:{color}">{esc}</span>' if color else esc)
        self.log_view.moveCursor(QTextCursor.End)

    def _on_stage(self, name: str):
        self.lbl_stage.setText(name)
        self.statusBar().showMessage(name)
        # A new stage moves until it reports how far it is, so the last stage's full bar
        # does not sit still while this one works.
        self.progress_bar.setRange(0, 0)

    def _on_fraction(self, f: float):
        if f < 0:
            self.progress_bar.setRange(0, 0)   # busy indicator
            return
        # Repainting the bar repaints the window, including the 3D view, so cap it at ~10 Hz.
        now = time.perf_counter()
        if f < 1.0 and now - self._last_fraction_t < 0.1:
            return
        self._last_fraction_t = now
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(int(1000 * f))

    def _timed_render(self):
        """Render, then space the next streaming render by a few frame times."""
        t0 = time.perf_counter()
        self.viewer.render()
        frame = time.perf_counter() - t0
        self._render_timer.setInterval(int(min(1000, max(50, 4000 * frame))))

    # ============================================================== tasks
    def _busy(self) -> bool:
        return self._task is not None and self._task.isRunning()

    def _run(self, fn, on_done, name: str, on_partial=None):
        if self._busy():
            QMessageBox.information(self, "KiPI", "Another operation is still running.")
            return None
        prog = QtProgress()
        prog.sig_message.connect(self.log)
        prog.sig_stage.connect(self._on_stage)
        prog.sig_fraction.connect(self._on_fraction)
        if on_partial is not None:
            prog.sig_partial.connect(on_partial)
        task = Task(fn, prog, self)
        task.done.connect(lambda result: self._task_done(on_done, result))
        task.failed.connect(lambda tb: self._on_task_failed(name, tb))
        task.finished.connect(lambda t=task: self._on_task_finished(t))
        self._task = task
        self.heartbeat.start(task, name)
        self.progress_bar.setRange(0, 0)          # moving until the first stage reports
        self.btn_cancel.setEnabled(True)
        self.act_cancel.setEnabled(True)
        self.act_reload.setEnabled(False)
        self._set_controls_enabled(False)      # offered only when they can be taken
        from kipi.gui import app as kipi_app
        kipi_app.watchdog_start()
        log.info("Task started: %s", name)
        task.start()
        return task

    def _task_done(self, on_done, result):
        """A task's result is drawn in the UI thread, which can take a while for a big
        solve: the wait cursor shows meanwhile."""
        with busy_cursor():
            on_done(result)

    def _on_task_finished(self, task=None):
        self.heartbeat.stop(task)
        # A task's done() may already have started the next task (loading a board
        # starts the net build). Its "finished" arrives after that, and must not reset
        # the UI or stop the watchdog for the task that is now running.
        if task is not None and task is not self._task:
            log.info("Earlier task finished (another task is already running)")
            return
        from kipi.gui import app as kipi_app
        kipi_app.watchdog_stop()
        log.info("Task finished")
        for panel in self._panels().values():
            panel.set_busy(False)
        pending, self._pending_solve = getattr(self, "_pending_solve", None), None
        pending_ac, self._pending_ac = getattr(self, "_pending_ac", None), None
        pending_noise, self._pending_noise = getattr(self, "_pending_noise", None), None
        self.btn_cancel.setEnabled(False)
        self.act_cancel.setEnabled(False)
        self.act_reload.setEnabled(True)
        self._set_controls_enabled(True)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)          # empty when idle: a full bar reads as busy
        self.lbl_stage.setText("Idle")
        self.statusBar().showMessage("Ready")
        if self._pending_stackup is not None:
            st, self._pending_stackup = self._pending_stackup, None
            QTimer.singleShot(0, lambda: self._stackup_changed(st))
        elif self._resync_pending:
            self._resync_pending = False
            self._sync_timer.start()
        if pending is not None:
            QTimer.singleShot(0, lambda: self.run_solve(pending))
        elif pending_ac is not None:
            QTimer.singleShot(0, lambda: self.run_ac_solve(*pending_ac))
        elif pending_noise is not None:
            QTimer.singleShot(0, lambda: self.run_noise_solve(*pending_noise))

    def _on_task_failed(self, name: str, tb: str):
        log.error(tb)
        self.log(tb, logging.ERROR)
        QMessageBox.critical(self, "KiPI", f"{name} failed:\n\n{tb.strip().splitlines()[-1]}")

    def _cancel_task(self):
        if self._task is not None:
            self._task.progress.cancel()

    # ===================================================== board loading
    def _set_title(self, text: str):
        self.setWindowTitle(f"KiPI {__version__} — {text}"
                            + (" (standalone)" if self.host is Host.STANDALONE else ""))

    def _source_changed(self):
        """Show what the board's source can do (Reload, Refill zones, Select), named
        after it: "Load from KiCad" as a plugin, "Reload" for a file."""
        src = self._source
        if src is not None and self.host is Host.PLUGIN:
            self.act_reload.setText(f"Load from {src.name}")
        else:
            self.act_reload.setText("Reload")
        self.act_reload.setToolTip(with_keys(f"Re-read the board from {src.description}"
                                             if src else "Re-read the board", KEYS["reload"]))
        self.act_reload.setVisible(src is not None and src.caps.reload)
        self.chk_refill.setToolTip(f"Refill all zones in {src.name if src else 'the editor'} "
                                   "before reading (recommended)")
        self._act_refill.setVisible(src is not None and src.caps.refill_zones)
        self.btn_select.setText(f"Select in {src.name}" if src else "Select")
        self.btn_select.setToolTip("Select the picked pad or via in "
                                   f"{src.description if src else 'the editor'}")
        self.btn_select.setVisible(src is not None and src.caps.select_items)
        self._watch(src.files() if src is not None else [])

    # --- the board's files -----------------------------------------------------------
    def _watch(self, files):
        old = self._watcher.files()
        if old:
            self._watcher.removePaths(old)
        existing = [str(f) for f in files if Path(f).is_file()]
        if existing:
            self._watcher.addPaths(existing)

    def _board_file_changed(self, path: str):
        # Saving by writing a new file and renaming it drops the old one from the watch.
        if path not in self._watcher.files() and Path(path).is_file():
            self._watcher.addPath(path)
        if time.monotonic() < self._own_write_until:
            return
        self._changed_timer.start()

    def _offer_reload(self):
        src = self._source
        if src is None or self._closed or self._asking_reload:
            return
        files = src.files()
        if self._busy() or not all(Path(f).is_file() for f in files):
            self._changed_timer.start()      # mid-task or mid-save: ask a little later
            return
        self._watch(files)
        self._asking_reload = True
        try:
            answer = QMessageBox.question(
                self, "Board changed",
                f"{src.name} has changed on disk. Read it again?\n\n"
                "The setup and the nets you have ticked stay as they are.")
        finally:
            self._asking_reload = False
        if answer == QMessageBox.Yes:
            self.reload_board()

    def _fill_recent_menu(self):
        from kipi.gui import recent
        self.recent_menu.clear()
        paths = recent.boards()
        for p in paths:
            a = self.recent_menu.addAction(Path(p).name)
            a.setToolTip(p)
            a.setStatusTip(p)
            a.triggered.connect(lambda _=False, p=p: self.open_path(p))
        if not paths:
            self.recent_menu.addAction("No recent boards").setEnabled(False)
            return
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("Clear the list", recent.clear)

    def open_board(self):
        """Standalone: open a board file, which Reload then reads again."""
        path, _ = QFileDialog.getOpenFileName(self, "Open board", "", registry.file_filter())
        if path:
            self.open_path(path)

    def open_path(self, path):
        """Read the board file ``path``; it becomes the source once it has been read."""
        if not self._keep_or_drop_changes():
            return
        try:
            source = registry.open_board(path)
        except ValueError as e:
            QMessageBox.warning(self, "KiPI", str(e))
            return

        def done(snap):
            from kipi.gui import recent
            self._source = source
            self._source_changed()
            self._set_snapshot(snap)
            recent.add(path)

        self._run(lambda progress: source.load(progress), done, "Opening board")

    def reload_board(self):
        """Read the board from the source again (Load from KiCad)."""
        source = self._source
        if source is None or not self._keep_or_drop_changes():
            return
        refill = source.caps.refill_zones and self.chk_refill.isChecked()
        self._run(lambda progress: source.load(progress, refill_zones=refill),
                  self._set_snapshot, "Loading board")

    def open_snapshot(self):
        if not self._keep_or_drop_changes():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open board snapshot", "",
                                              "KiPI snapshot (*.json *.json.gz)")
        if not path:
            return
        from kipi.core.model.snapshot_io import load

        self._run(lambda progress: load(path), self._set_snapshot, "Opening snapshot")

    def save_snapshot(self):
        if self.snapshot is None:
            return
        default = str(Path(self.snapshot.project_dir or ".") /
                      f"{Path(self.snapshot.board_name).stem}.kipi.json.gz")
        path, _ = QFileDialog.getSaveFileName(self, "Save board snapshot", default,
                                              "KiPI snapshot (*.json.gz *.json)")
        if not path:
            return
        import dataclasses
        from kipi.core.model.snapshot_io import save
        self._own_write_until = time.monotonic() + 2.0   # may be the file Reload reads
        # A snapshot is the board as its source has it; edited thicknesses live in the setup.
        with busy_cursor():
            save(dataclasses.replace(self.snapshot, stackup=self._imported_stackup
                                     or self.snapshot.stackup), path)
        self.log(f"Snapshot saved to {path}")

    def save_screenshot(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", "kipi.png", "PNG (*.png)")
        if path:
            if self.views.currentWidget() in (self.plot, self.board_map, self.noise_plot):
                self.views.currentWidget().save_png(path)
            else:
                self.viewer.screenshot(path)
            self.log(f"Screenshot saved to {path}")

    def _set_snapshot(self, snap: BoardSnapshot):
        prev = self.snapshot
        # Re-reading the same board from KiCad keeps the nets ticked now, even unsaved.
        keep = (self._checked_nets() if prev is not None and
                (prev.board_name, prev.project_dir) == (snap.board_name, snap.project_dir)
                else None)
        self.snapshot = snap
        self._copper_cache.clear()
        self._model_cache.clear()
        self._model_gen += 1
        self._model_source = None
        self._guess_wanted = False
        self.btn_guess.setEnabled(True)
        self._picked = None
        self.btn_select.setEnabled(False)
        self._set_title(snap.board_name)
        self._load_layer_colors(snap)
        self._result = None
        self._set_tree(None, {})
        self._pending_stackup = None
        setup = self._load_setup(snap)
        self._imported_stackup = snap.stackup
        self._apply_setup_stackup(setup)
        self._populate_layers()
        self._populate_nets(keep if keep is not None else setup.nets, setup)
        # The setup as loaded, with the nets ticked by default on a new board: what Save
        # would write now, so only later edits count as not saved.
        setup.nets = self._checked_nets()
        self._saved = copy.deepcopy(setup)
        self._show_unsaved()
        self.setup_bar.setEnabled(not self._busy())
        self._show_board_summary()
        from kipi.core.viz.meshes import board_body, board_coatings
        self.viewer.set_board(*board_body(snap))
        self.viewer.set_coatings(board_coatings(snap))
        self.sync_nets()

    def _load_layer_colors(self, snap: BoardSnapshot):
        """Color the copper layers like the PCB editor's color theme, read again on every
        load so a theme changed in KiCad shows after Load from KiCad."""
        from kipi.core.viz.meshes import set_layer_colors
        colors, source = self._tool.layer_colors(snap)
        set_layer_colors(colors)
        if source != self._layer_colors_from:
            self._layer_colors_from = source
            self.log(f"Layer colors: {source}")

    # ============================================================ lists
    def _populate_layers(self):
        from kipi.core.viz.meshes import layer_color
        st = self.snapshot.stackup
        hidden = {self.layer_list.item(i).data(Qt.UserRole) for i in range(self.layer_list.count())
                  if self.layer_list.item(i).checkState() != Qt.Checked}
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for cu in st.copper:
            it = QListWidgetItem(f"{cu.name}   {cu.thickness_mm * 1000:.0f} µm   "
                                 f"z={cu.z_mid_mm:.3f} mm")
            r, g, b = layer_color(cu, len(st.copper))
            pm = QPixmap(12, 12)
            pm.fill(QColor.fromRgbF(r, g, b))
            it.setIcon(QIcon(pm))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked if cu.id in hidden else Qt.Checked)
            it.setData(Qt.UserRole, cu.id)
            self.layer_list.addItem(it)
        self.layer_list.blockSignals(False)

    def _populate_nets(self, chosen: Optional[List[str]], setup):
        """Fill the net list, ticking ``chosen``; None means the user has never chosen
        (a new setup), so tick the main power and ground planes instead."""
        snap = self.snapshot
        kinds = classify_nets(snap)
        pad_count: Dict[str, int] = {}
        for p in snap.pads:
            pad_count[p.net] = pad_count.get(p.net, 0) + 1
        if chosen is not None:
            auto = set(chosen)
            gone = sorted(auto - set(kinds))
            if gone:
                self.log(f"Nets in the setup that are not on the board: {', '.join(gone)}",
                         logging.WARNING)
        else:
            # Start with the main plane nets: a few power nets plus the main ground.
            zone_nets = {z.net for z in snap.zones}
            by_pads = sorted(zone_nets, key=lambda n: -pad_count.get(n, 0))
            auto = set([n for n in by_pads if kinds.get(n) == "power"][:MAX_AUTO_NETS - 1] +
                       [n for n in by_pads if kinds.get(n) == "ground"][:1])
            # A setup saved before nets were stored: its VRMs, loads and links are on
            # nets the user had ticked.
            auto |= setup.pin_nets()

        self.net_list.blockSignals(True)
        self.net_list.clear()
        for name in sorted_nets(snap):
            kind = kinds[name]
            tag = _KIND_TAG[kind]
            extra = f"  [{tag}]" if tag else ""
            it = QListWidgetItem(f"{name}{extra}   ({pad_count.get(name, 0)} pads)")
            it.setData(Qt.UserRole, name)
            it.setData(Qt.UserRole + 1, kind)
            it.setForeground(QColor(_KIND_COLOR[kind]))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if name in auto else Qt.Unchecked)
            self.net_list.addItem(it)
        self.net_list.blockSignals(False)
        self._checked_cache = None
        self._apply_net_filter()

    def _apply_net_filter(self):
        text = self.net_filter.text().strip().lower()
        power_only = self.chk_power_only.isChecked()
        for i in range(self.net_list.count()):
            it = self.net_list.item(i)
            name = it.data(Qt.UserRole).lower()
            kind = it.data(Qt.UserRole + 1)
            hide = (text and text not in name) or (power_only and kind == "signal"
                                                  and it.checkState() != Qt.Checked)
            it.setHidden(bool(hide))

    def _checked_nets(self) -> List[str]:
        return [self.net_list.item(i).data(Qt.UserRole) for i in range(self.net_list.count())
                if self.net_list.item(i).checkState() == Qt.Checked]

    def _checked_set(self) -> frozenset:
        """Cached set of checked nets; cleared whenever a checkbox changes."""
        if self._checked_cache is None:
            self._checked_cache = frozenset(self._checked_nets())
        return self._checked_cache

    def _set_checked(self, pred):
        """Check exactly the net items for which ``pred(item)`` is true, then sync once."""
        self.net_list.blockSignals(True)
        for i in range(self.net_list.count()):
            it = self.net_list.item(i)
            it.setCheckState(Qt.Checked if pred(it) else Qt.Unchecked)
        self.net_list.blockSignals(False)
        self._checked_cache = None
        self._apply_net_filter()
        self._sync_timer.start()

    def _check_power_nets(self):
        self._set_checked(lambda it: it.checkState() == Qt.Checked
                          or it.data(Qt.UserRole + 1) != "signal")

    def _on_net_item_changed(self, item):
        self._checked_cache = None
        self.viewer.set_included_nets(self._checked_set())
        self._sync_timer.start()
        self._show_unsaved()

    def _on_layer_item_changed(self, item):
        self.viewer.set_layer_visible(item.data(Qt.UserRole), item.checkState() == Qt.Checked)

    def _opacity_changed(self, value: int):
        self.lbl_opacity.setText(f"{value}%")
        self.viewer.set_layer_opacity(value / 100.0)

    def _set_layers_checked(self, on: bool):
        """Tick (show) or untick (hide) every copper layer."""
        state = Qt.Checked if on else Qt.Unchecked
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it.checkState() != state:
                it.setCheckState(state)      # itemChanged updates the viewer

    # ============================================================ scene
    def sync_nets(self):
        """Keep all board copper in the view and synchronize checked nets to analysis.

        All net meshes are retained so the geometry view can show context copper;
        checked nets continue to define each analysis scope.
        """
        if self.snapshot is None:
            return
        included = self._checked_set()
        self.viewer.set_included_nets(included)
        self.solve_panel.set_nets(list(included))
        self.ac_panel.set_nets(list(included))
        self.noise_panel.set_nets(list(included))
        self.solve_panel.setup.nets = self._checked_nets()   # saved with the setup
        self._model_timer.start()
        if self._busy():
            self._resync_pending = True
            return
        wanted = [n.name for n in self.snapshot.nets]
        shown = set(self.viewer.nets())
        todo = [n for n in wanted if n not in shown]
        if not todo:
            return

        snap = self.snapshot
        cache = self._copper_cache

        def work(progress):
            from kipi.core.geometry.copper import net_copper
            from kipi.core.viz.meshes import build_net_scene
            built = 0
            with progress.timed(f"Building {len(todo)} net(s)"):
                for i, net in enumerate(todo):
                    progress.check_cancel()
                    if net not in cache:
                        cache[net] = net_copper(snap, net, progress)
                    progress.partial(build_net_scene(snap, net, cache[net], progress))
                    built += 1
                    progress.fraction((i + 1) / len(todo))
            return built

        added: List[str] = []

        def on_net(ns):
            if not ns.copper:
                self.log(f"{ns.net}: no copper on any layer", logging.WARNING)
            self.viewer.add_net(ns, render=False)
            added.append(ns.net)
            if not self._render_timer.isActive():
                self._render_timer.start()

        def done(built):
            self.viewer.render()
            self.log(f"Added {len(added)} net(s); {len(self.viewer.nets())} net(s) shown")

        self._run(work, done, "Building view", on_partial=on_net)

    # ============================================================ PI Models markers
    def _sync_pi_markers(self):
        """Mark the parts the current mode solves, on the ticked nets (#84): DC mode's VRMs,
        loads and series links, or, in AC mode, the VRMs, series links, probes and
        modelled capacitors of an impedance sweep, plus each victim's driver and
        receiver in Coupled Noise mode."""
        if self._closed:
            return
        if self.snapshot is None:
            self.viewer.set_pi_markers([])
            return
        # Both panels edit the one setup, which holds each mode's parts.
        setup, nets = self.solve_panel.setup, self._checked_set()
        if self.mode == "dc":
            from kipi.dc.viz.markers import pi_markers
            markers = pi_markers(self.snapshot, setup, nets)
        elif self.mode == "noise":
            from kipi.noise.markers import noise_markers
            markers = noise_markers(self.snapshot, setup, nets)
        else:
            from kipi.ac.viz.markers import ac_markers
            markers = ac_markers(self.snapshot, setup, nets)
        self.viewer.set_pi_markers(markers)
        if self.mode == "noise":
            self._show_coupling(front=False)  # a new board's view starts without it

    def _show_coupling(self, front: bool = True):
        """Draw the Coupled Noise victim's coupling picked in its results in the 3D view
        (#152), and bring the view to the front; only in that mode."""
        if self._closed:
            return
        ov = self.noise_panel.coupling_overlay() if self.mode == "noise" else None
        if ov is None and self.viewer.coupling is None:
            return
        self.viewer.set_coupling(ov)
        if ov is not None and front:
            self.views.setCurrentWidget(self.viewer)

    def _panels(self):
        """Each mode's setup panel, by mode key."""
        return {"dc": self.solve_panel, "ac": self.ac_panel, "noise": self.noise_panel}

    def _mode_panel(self):
        """The setup panel of the current mode."""
        return self._panels()[self.mode]

    def _pi_part_selected(self, kind: str, name: str, by_user: bool):
        """Mark the part selected in the current mode's setup lists in the 3D view: a VRM,
        load or series link in DC mode, a VRM, series link, probe or capacitor model (all
        its parts) in AC mode. With PI Models
        shown and Snap to selected ticked, one the user picked brings the view to it (#96)."""
        self.viewer.set_pi_selected((kind, name) if kind else None)
        if not (by_user and kind and self.chk_pi.isChecked() and self.chk_snap.isChecked()):
            return
        marks = [m for m in self.viewer.pi_markers if m.is_item(kind, name)]
        if not marks:
            self.statusBar().showMessage(f"{name} has no pads on the nets shown", 5000)
            return
        self.viewer.focus_on(np.vstack([p for m in marks for p in (m.power, m.ground)]))

    def _sync_view_features(self, _=None):
        """Tick the features each view keeps its own setting of as the view now shown has
        them."""
        self.chk_models.setChecked(self.viewer.models_shown)
        self.chk_mask.setChecked(self.viewer.feature_shown("mask"))
        self.chk_silk.setChecked(self.viewer.feature_shown("silk"))

    # ============================================================ part models
    def _wanted_models(self) -> List[str]:
        """Every part on the board, when either view shows models (#96)."""
        snap = self.snapshot
        if snap is None or not self.viewer.models_wanted:
            return []
        # Pads name their parts too: snapshots from before footprints were saved list none.
        refs = {f.reference for f in snap.footprints} | {p.footprint for p in snap.pads}
        refs.discard("")
        return sorted(refs)

    def _sync_models(self):
        """Show the models of the wanted parts, loading those not seen yet."""
        snap = self.snapshot
        if snap is None or self._closed:
            return
        wanted = self._wanted_models()
        todo = [r for r in wanted if r not in self._model_cache]
        if todo and self._model_task is None:
            self._load_models(todo)
        elif todo:
            self._models_pending = True
        if (self._guess_wanted and wanted and self._model_task is None
                and all(r in self._model_cache for r in wanted)):
            self._run_guess(wanted)
        side = {f.reference: f.side for f in snap.footprints}
        shown = {r: (self._model_cache[r], side.get(r, "top")) for r in wanted
                 if self._model_cache.get(r) is not None}
        kicad = self._imported_stackup or snap.stackup
        self.viewer.set_models(shown, snap.stackup.board_thickness_mm,
                               kicad.board_thickness_mm)

    def _models_source(self):
        """The board's :class:`~kipi.sources.base.ModelProvider`, or False (and why, said
        once per board) if it has none."""
        if self._model_source is None:
            provider, why = self._tool.part_models(self.snapshot)
            if provider is None and why:
                self.log(why, logging.WARNING)
            self._model_source = provider or False
        return self._model_source

    def guess_models(self):
        """Guess models from KiCad's library for the parts that have none (#130): once
        the board's own models are in, ticking 3D Models if it is not."""
        if self.snapshot is None:
            return
        self._guess_wanted = True
        if self.chk_models.isChecked():
            self._model_timer.start()
        else:
            self.chk_models.setChecked(True)        # starts the timer

    def _run_guess(self, wanted: List[str]):
        self._guess_wanted = False
        refs = [r for r in wanted if self._model_cache.get(r) is None]
        if not refs:
            self.log("3D models: every part has one already")
            return
        provider, why = self._tool.guessed_models(self.snapshot)
        if provider is None:
            self.log(why, logging.WARNING)
            return
        self._load_models(refs, provider)

    def _load_models(self, refs: List[str], provider=None):
        provider = provider or self._models_source()
        if not provider:
            self._model_cache.update({r: None for r in refs})
            return
        gen = self._model_gen

        def work(progress):
            from kipi.core.progress import Canceled
            progress.info(f"Loading 3D models of {len(refs)} part(s) with {provider.label}")
            try:
                found = provider.load(refs, progress)
            except Canceled:
                raise
            except Exception as e:  # noqa: BLE001 - models are a nicety; never fail on them
                progress.warn(f"Could not load 3D models: {e}")
                return {r: None for r in refs}
            n = sum(m is not None for m in found.values())
            progress.info(f"3D models: {n} of {len(refs)} part(s) have one")
            return found

        prog = QtProgress()
        prog.sig_message.connect(self.log)
        task = Task(work, prog, self)
        task.done.connect(lambda found: self._models_loaded(gen, found))
        task.finished.connect(self._model_task_finished)
        self._model_task = task
        self.heartbeat.start(task, "Loading 3D models")
        task.start()

    def _models_loaded(self, gen: int, found: dict):
        if gen != self._model_gen:          # a board loaded since; these are its parts
            return
        self._model_cache.update(found)
        self._sync_models()

    def _model_task_finished(self):
        self.heartbeat.stop(self._model_task)
        self._model_task = None
        if self._models_pending or self._guess_wanted:
            self._models_pending = False
            self._model_timer.start()

    # ============================================================ solve
    # ------------------------------------------------------------ setup file
    def _load_setup(self, snap):
        from kipi.dc.pi.model import PISetup, find_setup, is_yaml, setup_path
        project_dir = snap.project_dir
        if not project_dir and snap.file:
            project_dir = str(Path(snap.file).parent)
        path = setup_path(snap.board_name, project_dir)
        found = find_setup(snap.board_name, project_dir)
        setup = PISetup()
        label = f"New setup (will be saved as {path.name})"
        if found is not None:
            try:
                setup = PISetup.load(found)
                label = f"Setup: {found}"
                self.log(f"Loaded PI setup {found}")
                if not is_yaml(found) and is_yaml(path):
                    label += f" (saved as {path.name} from now on)"
                    self.log(f"{found.name} is a JSON setup. JSON setups are deprecated and "
                             "a future KiPI will stop reading them; saving the setup writes "
                             f"{path.name}, which is read from then on. The JSON file is "
                             "left as it is.", logging.WARNING)
            except Exception as e:  # noqa: BLE001 - a bad file must not block the board
                self.log(f"Could not read {found}: {e}", logging.WARNING)
                label = f"Could not read {found.name}; starting empty"
        self._setup_path = path
        self._saved = copy.deepcopy(setup)
        for panel in self._panels().values():
            panel.set_board(snap, setup)
        self.setup_bar.set_label(label)
        self._refresh_power_tree()
        self.plot.clear()
        self.board_map.clear()
        self.noise_plot.clear()
        return setup

    def import_pdn(self):
        """Replace the power tree with the one in a Tomachie .pdn.json (#120)."""
        if self.snapshot is None:
            QMessageBox.information(self, "KiPI", "Load or open the board first.")
            return
        current = self.solve_panel.setup
        if (current.vrms or current.loads or current.links) and QMessageBox.question(
                self, "KiPI", f"This board already has a power tree: {len(current.vrms)} "
                              f"VRM(s), {len(current.loads)} load(s) and {len(current.links)} "
                              "DC connection(s). Replace it with the one in the file?\n\n"
                              "Mesh, via plating, thermal and AC settings stay as they are."
        ) != QMessageBox.Yes:
            return
        start = str(self._setup_path.parent) if self._setup_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Tomachie power tree", start,
            "Tomachie power tree (*.pdn.json);;JSON (*.json)")
        if not path:
            return
        from kipi.dc.pi.importers.tomachie_pdn import PdnImportError, import_tomachie_pdn
        name = Path(path).name
        try:
            setup, notes = import_tomachie_pdn(path, self.snapshot, current)
        except (PdnImportError, ValueError, KeyError, TypeError, OSError) as e:
            QMessageBox.critical(self, "KiPI", f"Could not import {name}:\n\n{e}")
            return
        from kipi.dc.pi.importers.import_summary import counts, group_notes, setup_changes
        notes = group_notes(notes)
        if notes:
            self.log(f"Notes on {name}:")
        for note in notes:
            self.log(f"  {note}", logging.WARNING)
        used = setup.pin_nets()
        self._set_checked(lambda it: it.checkState() == Qt.Checked
                          or it.data(Qt.UserRole) in used)
        label = f"Imported from {name}"
        for panel in self._panels().values():
            panel.set_board(self.snapshot, setup)
        self._refresh_power_tree()
        self.setup_bar.set_label(f"{label} - not saved")
        # Last in the Log, under the notes, so it is the line in view.
        self.log(f"== Read {name}: {counts(setup)} - {setup_changes(current, setup)}"
                 + (f"; {len(notes)} note(s) above" if notes else "")
                 + ". Not saved: Save keeps it, the setup file is as it was")
        self.log_dock.show()
        self.log_dock.raise_()
        self._show_unsaved()

    def _save_setup(self):
        if self._setup_path is None:
            return
        if self.snapshot is not None:   # a checkbox change may not have synced yet
            self.solve_panel.setup.nets = self._checked_nets()
        try:
            self._setup_path = self.solve_panel.setup.save(self._setup_path)
            self._saved = copy.deepcopy(self.solve_panel.setup)
            self._show_unsaved()
            self.setup_bar.set_label(f"Setup: {self._setup_path}")
            self.log(f"Saved PI setup to {self._setup_path}")
        except OSError as e:
            self.log(f"Could not save the setup: {e}", logging.WARNING)

    # ------------------------------------------------------------ solve
    def _set_tree(self, tree, scenes):
        """The DC result every other part of the window reads: the capacitors' DC bias in
        an AC solve (#74) and the DC drop in the AC target calculator."""
        self._tree, self._scenes = tree, scenes
        self.ac_panel.set_dc_result(tree)

    def run_solve(self, setup):
        snap = self.snapshot
        if snap is None:
            return
        if self._busy():
            # A view update (e.g. after ticking nets) is still running: queue the solve
            # instead of refusing it; it starts as soon as the current task finishes.
            self._pending_solve = setup
            self.solve_panel.set_busy(True, queued=True)
            self.log("Solve queued until the current task finishes")
            return
        frozen = copy.deepcopy(setup)     # edits during the solve don't affect it

        def work(progress):
            from kipi.core.mesh.mesher import MeshError
            from kipi.dc.pi.tree import solve_tree
            from kipi.dc.solver import SolveError
            from kipi.dc.viz.results import build_scenes
            try:
                if frozen.thermal.enabled:
                    from kipi.dc.thermal.cosim import solve_electrothermal
                    tree = solve_electrothermal(snap, frozen, frozen.thermal, progress)
                else:
                    tree = solve_tree(snap, frozen, progress)
            except (SolveError, MeshError) as e:
                progress.warn(str(e))
                return None, str(e)       # a setup problem, reported in the panel
            with progress.timed("Building result views"):
                scenes = build_scenes(tree, snap, progress)
            tree.solved_setup = frozen         # what a report of it shows (#185)
            return tree, scenes

        def done(out):
            tree, scenes = out
            if tree is None:
                self.solve_panel.show_error(scenes)
                return
            self._set_tree(tree, scenes)
            self.solve_panel.set_report(None)
            first = tree.loads[0].group if tree.loads else next(iter(tree.groups))
            self.solve_panel.show_results(tree, first)
            self._show_group(first)
            for l in tree.loads:
                self.log(f"  {l.name}: {fmt_drop(l.drop)} ({abs(l.drop_pct):.3f} %), worst pin "
                         f"{fmt_drop(l.drop_worst)} at {l.current:g} A {l.verdict}".rstrip())

        if self._run(work, done, "Solving power tree") is not None:
            self.solve_panel.set_busy(True)

    # ------------------------------------------------------------ reports (#185)
    def _report_setup(self, result, panel):
        """The setup a report of ``result`` shows: the one it was solved with, or, for
        results opened from a file, the setup as it is now."""
        return getattr(result, "solved_setup", None) or copy.deepcopy(panel.setup)

    def _report_path(self, name: str) -> Path:
        """Where a report goes: next to the setup, else the board."""
        if self._setup_path is not None:
            return self._setup_path.parent / name
        snap = self.snapshot
        folder = snap.project_dir or (str(Path(snap.file).parent) if snap.file else "")
        return Path(folder or ".") / name

    def _write_report(self, what: str, write, path: Path, on_written):
        """In the background, ``write(path, progress)`` a report of the shown results
        (where a PDF cannot be made, as HTML), then ``on_written(path)`` and open it."""
        def work(progress):
            try:
                return write_report_file(lambda p: write(p, progress), path, progress), None
            except Exception as e:  # noqa: BLE001 - the results stand without a report
                return None, f"{type(e).__name__}: {e}"

        def done(out):
            written, error = out
            if written is None:
                self.log(f"Could not write the report: {error}", logging.WARNING)
                QMessageBox.warning(self, "KiPI", f"Could not write the report:\n{error}")
                return
            on_written(written)
            self._open_new_report(written)

        self._run(work, done, f"Writing the {what} report")

    def _dc_report(self, fmt: str):
        tree, scenes, snap = self._tree, self._scenes, self.snapshot
        if tree is None or snap is None:
            return
        from kipi.dc.report.html import report_name, write_report
        setup = self._report_setup(tree, self.solve_panel)

        def written(path):
            tree.report_path = path
            self.solve_panel.set_report(path)

        self._write_report("DC", lambda p, progress: write_report(p, tree, scenes, snap, setup,
                                                                  progress),
                           self._report_path(report_name(self._results_stem(), fmt=fmt)),
                           written)

    def _ac_report(self, result, fmt: str):
        snap = self.snapshot
        if result is None or snap is None:
            return
        from kipi.ac.html_report import report_name, write_ac_report
        setup = self._report_setup(result, self.ac_panel)
        self._write_report("AC", lambda p, progress: write_ac_report(p, result, snap, setup,
                                                                     progress),
                           self._report_path(report_name(self._results_stem(), result.rail,
                                                         fmt=fmt)),
                           lambda path: self.ac_panel.set_report(result, path))

    def _noise_report(self, result, fmt: str):
        snap = self.snapshot
        if result is None or snap is None:
            return
        from kipi.noise.html_report import report_name, write_noise_report
        setup = self._report_setup(result, self.noise_panel)
        self._write_report("Coupled Noise",
                           lambda p, progress: write_noise_report(p, result, snap, setup,
                                                                  progress),
                           self._report_path(report_name(self._results_stem(),
                                                         result.aggressor, fmt=fmt)),
                           lambda path: self.noise_panel.set_report(result, str(path)))

    def _open_new_report(self, path):
        """Open a report just written in the default browser."""
        if path is None:
            return
        from kipi.gui.browser import open_in_browser
        if open_in_browser(path):
            self.log(f"Opened the report in the web browser: {path}")
        else:
            self.log(f"Could not open a web browser; the report is at {path}")

    # ------------------------------------------------------------ AC solve
    def run_ac_solve(self, setup, vrm_name: str, gnd: str):
        snap = self.snapshot
        if snap is None:
            return
        if self._busy():
            self._pending_ac = (setup, vrm_name, gnd)
            self.ac_panel.set_busy(True, queued=True)
            self.log("AC solve queued until the current task finishes")
            return
        frozen = copy.deepcopy(setup)
        dc_result = self._tree       # the last DC solve gives the capacitors' DC bias (#74)

        def work(progress):
            from kipi.ac.solver import ACError, solve_ac
            from kipi.core.mesh.mesher import MeshError
            from kipi.dc.solver import SolveError
            try:
                result = solve_ac(snap, frozen, vrm_name, gnd, progress, dc_result)
            except (ACError, MeshError, SolveError) as e:
                progress.warn(str(e))
                return None, str(e)
            result.solved_setup = frozen     # what a report of it shows (#185)
            return result, None

        def done(out):
            result, error = out
            if result is None:
                self.ac_panel.show_error(error)
                return
            self.ac_panel.show_result(result)

        def partial(board_map):
            """The solve's board map as it fills: shown while the rail solves."""
            first = self.board_map.board_map is not board_map
            self.board_map.set_map(board_map)
            if first and self.mode == "ac":
                self.views.setCurrentWidget(self.board_map)

        if self._run(work, done, "AC impedance solve", on_partial=partial) is not None:
            self.ac_panel.set_busy(True)

    def _show_ac_result(self, result):
        """The AC panel's shown result: a new solve, or another rail's earlier one (None:
        that rail has not been solved)."""
        if result is None:
            self.plot.clear()
            self.board_map.clear()
            return
        self.plot.set_result(result)
        if self.board_map.board_map is not getattr(result, "board_map", None):
            self.board_map.set_map(getattr(result, "board_map", None))  # None: saved before 0.7.2
        self.views.setCurrentWidget(self.plot)

    def run_noise_solve(self, setup, aggressor: str):
        """Solve one aggressor (#69): its waveforms are shown as soon as they are built."""
        snap = self.snapshot
        if snap is None:
            return
        if self._busy():
            self._pending_noise = (setup, aggressor)
            self.noise_panel.set_busy(True, queued=True)
            self.log("Coupled Noise solve queued until the current task finishes")
            return
        frozen = copy.deepcopy(setup)

        def work(progress):
            from kipi.noise.checks import NoiseError
            from kipi.noise.solver import solve_noise
            try:
                result = solve_noise(snap, frozen, aggressor, progress)
            except NoiseError as e:
                progress.warn(str(e))
                return None, str(e)
            result.solved_setup = frozen     # what a report of it shows (#185)
            return result, None

        def done(out):
            result, error = out
            if result is None:
                self.noise_panel.show_error(error)
                return
            self.noise_panel.show_result(result)

        def partial(result):
            """The waveforms, shown while the coupling solves."""
            self.noise_panel.show_result(result)

        if self._run(work, done, "Coupled Noise solve", on_partial=partial) is not None:
            self.noise_panel.set_busy(True)

    def _show_noise_result(self, result):
        """The Coupled Noise panel's shown result (None: that aggressor has not been
        solved)."""
        if result is None:
            self.noise_plot.clear()
            return
        self.noise_plot.periods = self.noise_panel.setup.noise.periods_shown
        self.noise_plot.set_result(result)
        if self.mode == "noise" and self.viewer.coupling is None:
            self.views.setCurrentWidget(self.noise_plot)

    def _show_victim(self, name: str):
        """A victim picked in the Coupled Noise results: plot its noise (or, with its
        coupling shown in 3D, keep the 3D view in front)."""
        self.noise_plot.set_victim(name)
        if self.mode == "noise" and self.viewer.coupling is None:
            self.views.setCurrentWidget(self.noise_plot)

    def _open_noise_results(self):
        from kipi.noise import report
        start = str(self._setup_path.parent) if self._setup_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Coupled Noise results", start,
            f"KiPI Coupled Noise results (*{report.RESULTS_SUFFIX})")
        if not path:
            return
        try:
            data = report.load_results(path)
        except Exception as e:  # noqa: BLE001 - any unreadable file
            QMessageBox.warning(self, "KiPI", f"Could not open {path}:\n{e}")
            return
        if data["board"] != self.snapshot.board_name:
            self.log(f"Results are for {data['board']}, not {self.snapshot.board_name}",
                     logging.WARNING)
        left_out = self.noise_panel.load_results(data["results"])
        if left_out:
            self.log(f"No aggressor in the setup named {', '.join(left_out)}; their results "
                     f"were left out", logging.WARNING)
        if not data["results"] or len(left_out) == len(data["results"]):
            QMessageBox.information(self, "KiPI", "None of the aggressors in this file is in "
                                                  "the setup, so there is nothing to show.")
            return
        self.log(f"Opened Coupled Noise results saved {data['saved']} from {path}")

    def _show_cursor(self, text: str):
        """The impedance plot's cursor values, in the Info pane in place of the last pick."""
        if not text:
            return
        self._picked = None
        self.btn_select.setEnabled(False)
        self.info.setHtml(text)

    # ------------------------------------------------------------ stackup
    def _layer_colors(self):
        from kipi.core.viz.meshes import layer_color
        st = self.snapshot.stackup
        return {cu.id: layer_color(cu, len(st.copper)) for cu in st.copper}

    def _apply_setup_stackup(self, setup):
        """Use the stackup edits kept in the setup file, if they still fit the board."""
        from kipi.core.model.stackup import from_setup
        snap = self.snapshot
        edited, why = from_setup(self._imported_stackup, setup.stackup)
        if edited is not None:
            snap.stackup = edited
            self.log(f"Stackup: using the stackup edited in KiPI "
                     f"({edited.board_thickness_mm:.3f} mm board; "
                     f"KiCad has {self._imported_stackup.board_thickness_mm:.3f} mm)")
        elif why:
            setup.stackup = None
            self.log(f"Stackup: the setup's edited stackup no longer fits the board "
                     f"({why}); using KiCad's", logging.WARNING)
        if setup.legacy_laminate_k > 0:
            # Laminate k left the thermal settings for the Stackup window in 0.8.0 (#90).
            from kipi.core.model.stackup import to_setup, with_dielectric_k
            k = setup.legacy_laminate_k
            setup.legacy_laminate_k = 0.0
            edited = with_dielectric_k(edited or self._imported_stackup, k)
            snap.stackup = edited
            setup.stackup = to_setup(edited)
            self.log(f"Stackup: the thermal settings' Laminate k ({k:g} W/m·K) is now each "
                     "dielectric's k in the Stackup window")
        self.stackup_window.set_board(self._imported_stackup, snap.stackup, setup,
                                      self._layer_colors())

    def _stackup_changed(self, st):
        """Use an edited stackup (or KiCad's again) for the views and every analysis."""
        snap = self.snapshot
        if snap is None or st is snap.stackup:
            return
        if self._busy():
            # A solve may be reading the stackup: change it when the task is done.
            self._pending_stackup = st
            self.log("Stackup change waits for the current task to finish")
            return
        with busy_cursor():
            self._apply_stackup(snap, st)

    def _apply_stackup(self, snap, st):
        from kipi.core.model.stackup import to_setup
        from kipi.core.viz.meshes import board_body, board_coatings
        snap.stackup = st
        self.solve_panel.setup.stackup = to_setup(st) if st.edited else None
        self.log(f"Stackup: {'edited in KiPI' if st.edited else 'from KiCad'}, "
                 f"{st.board_thickness_mm:.3f} mm board"
                 + ("; solve again for results with it" if self._tree is not None else ""))
        if self._tree is not None:
            self._set_tree(None, {})
            self.solve_panel.clear_results()
        self._populate_layers()
        self._show_board_summary()
        self.viewer.set_board(*board_body(snap), reset_view=False)
        self.viewer.set_coatings(board_coatings(snap))
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it.checkState() != Qt.Checked:
                self.viewer.set_layer_visible(it.data(Qt.UserRole), False)
        self.sync_nets()

    def _show_via(self, group: str, owner_id: str):
        """Jump to a via from the results table: show its group, center and select it."""
        self._show_3d()
        if self.solve_panel.cmb_group.currentText() != group:
            self.solve_panel.cmb_group.setCurrentText(group)
        elif self.viewer.result is None:          # the copper view is up ("Show copper")
            self._show_group(group)
        scene = self.viewer.result
        if scene is None or owner_id not in scene.via_ids:
            return
        top = scene.via_segments[scene.via_ids.index(owner_id)][0]
        self.viewer.plotter.set_focus((top[0], -top[1], top[2] * self.viewer.zscale))
        self.viewer.plotter.render()
        self._on_pick(None, owner_id)

    def _results_stem(self) -> str:
        return Path(self.snapshot.board_name).stem if self.snapshot else "kipi"

    def _export(self, kind: str):
        if not self._tree:
            return
        from kipi.core.naming import stamped
        from kipi.dc.pi import report
        start = str(self._setup_path.parent) if self._setup_path else ""
        try:
            if kind == "results":
                suffix = report.results_suffix(self._tree)
                what = "DC + thermal" if suffix == report.THERMAL_RESULTS_SUFFIX else "DC"
                path, _ = QFileDialog.getSaveFileName(
                    self, "Save results",
                    str(Path(start) / f"{stamped(self._results_stem())}{suffix}"),
                    f"KiPI {what} results (*{suffix})")
                if path:
                    self._save_results(path)
                return
            folder = QFileDialog.getExistingDirectory(self, "Export to folder", start)
        except OSError as e:
            QMessageBox.warning(self, "KiPI", f"Export failed: {e}")
            return
        if not folder:
            return
        tree, scenes, snap, stem = self._tree, self._scenes, self.snapshot, self._results_stem()
        setup = self.solve_panel.setup

        def work(progress):
            """In the background: a big board's VTK files take a while (#111)."""
            with progress.timed(f"Exporting {'CSV' if kind == 'csv' else 'VTK'} files"):
                try:
                    if kind == "csv":
                        return report.export_csv(tree, snap, folder, stem,
                                                 setup.via_current_limit_a, setup.thermal)
                    return report.export_vtk(tree, scenes, snap, folder, stem)
                except OSError as e:
                    return e

        def done(paths):
            if isinstance(paths, OSError):
                QMessageBox.warning(self, "KiPI", f"Export failed: {paths}")
            else:
                self.log(f"Exported {len(paths)} file(s) to {folder}")

        self._run(work, done, "Exporting")

    def open_results(self):
        if self.snapshot is None:
            # The tables and probing need the board (pads, layers); results files hold
            # only the solution.
            QMessageBox.information(self, "KiPI", "Load or open the board "
                                                  "before opening its results.")
            return
        if self.mode == "ac":
            self._open_ac_results()
            return
        if self.mode == "noise":
            self._open_noise_results()
            return
        from kipi.dc.pi import report
        start = str(self._setup_path.parent) if self._setup_path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open results", start, report.OPEN_FILTER)
        if not path:
            return
        legacy = self._allow_legacy_results(path)
        if legacy is None:
            return

        def work(progress):
            try:
                return report.load_results(path, progress, legacy=legacy), None
            except Canceled:
                raise
            except Exception as e:  # noqa: BLE001 - any unreadable file
                return None, str(e)

        def done(out):
            data, error = out
            if data is None:
                QMessageBox.warning(self, "KiPI", f"Could not open {path}:\n{error}")
                return
            if self.snapshot is not None and data["board"] != self.snapshot.board_name:
                self.log(f"Results are for {data['board']}, not {self.snapshot.board_name}",
                         logging.WARNING)
            self._set_tree(data["tree"], data["scenes"])
            first = (self._tree.loads[0].group if self._tree.loads
                     else next(iter(self._tree.groups)))
            self.solve_panel.show_results(self._tree, first)
            self._show_group(first)
            self.log(f"Opened results saved {data['saved']} from {path}")
            self._warn_legacy(data)

        from kipi.core.progress import Canceled
        self._run(work, done, "Opening results")

    def _save_results(self, path: str):
        from kipi.dc.pi import report
        tree, snap, setup = self._tree, self.snapshot, self.solve_panel.setup

        def work(progress):
            with progress.timed("Saving results"):
                try:
                    report.save_results(path, tree, snap, setup)
                except (OSError, TypeError, ValueError) as e:
                    return str(e)
            return None

        def done(error):
            if error:
                QMessageBox.warning(self, "KiPI", f"Saving the results failed: {error}")
            else:
                self.log(f"Saved results to {path}")

        self._run(work, done, "Saving results")

    def _allow_legacy_results(self, path: str) -> Optional[bool]:
        """Whether ``path`` holds results pickled by KiPI 0.7 or earlier, which can run code
        when opened (#99); None if it does and the user chose not to open it."""
        from kipi.core import results_io
        try:
            if not results_io.is_legacy(path):
                return False
        except OSError:
            return False             # opening it fails again, and says why
        answer = QMessageBox.warning(
            self, "KiPI",
            f"{Path(path).name} was saved by KiPI 0.7 or earlier, as a Python pickle, which "
            "can run code on this computer when it is opened.\n\nOpen it only if you saved "
            "it yourself or trust whoever did. Saving the results again writes KiPI's new "
            "format, which cannot run code.",
            QMessageBox.Open | QMessageBox.Cancel, QMessageBox.Cancel)
        return True if answer == QMessageBox.Open else None

    def _warn_legacy(self, data: dict):
        if data.get("legacy"):
            self.log("These results are in the pickle format of KiPI 0.7 and earlier, which a "
                     "later KiPI will no longer open; save them again to keep them",
                     logging.WARNING)

    def _open_ac_results(self):
        from kipi.ac import report
        start = str(self._setup_path.parent) if self._setup_path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open AC results", start,
                                              f"KiPI AC results (*{report.RESULTS_SUFFIX})")
        if not path:
            return
        legacy = self._allow_legacy_results(path)
        if legacy is None:
            return
        try:
            data = report.load_results(path, legacy=legacy)
        except Exception as e:  # noqa: BLE001 - any unreadable file
            QMessageBox.warning(self, "KiPI", f"Could not open {path}:\n{e}")
            return
        if data["board"] != self.snapshot.board_name:
            self.log(f"Results are for {data['board']}, not {self.snapshot.board_name}",
                     logging.WARNING)
        left_out = self.ac_panel.load_results(data["results"])
        if left_out:
            self.log(f"No VRM in the setup for rail(s) {', '.join(left_out)}; their results "
                     f"were left out", logging.WARNING)
        if not data["results"] or len(left_out) == len(data["results"]):
            QMessageBox.information(self, "KiPI", "None of the rails in this file has a VRM "
                                                  "in the setup, so there is nothing to show.")
            return
        self.log(f"Opened AC results saved {data['saved']} from {path}")
        self._warn_legacy(data)

    def _show_group(self, name: str):
        if not self._tree or name not in self._scenes:
            return
        self._result = self._tree.groups[name].result
        field = self.solve_panel.current_field()
        self.viewer.show_result(self._scenes[name], field,
                                limits=self.solve_panel.color_limits(name, field))
        self._shown_group = name
        self._sync_scale()

    def _group_activated(self, index: int):
        if self.viewer.result is None:
            self._show_group(self.solve_panel.cmb_group.itemText(index))

    def _field_changed(self, field: str):
        """A new result field; from the copper view this brings the results back."""
        if self.viewer.result is None:
            self._show_group(self.solve_panel.cmb_group.currentText())
        else:
            self.viewer.set_result_field(
                field, limits=self.solve_panel.color_limits(self._shown_group, field))
            self._sync_scale()

    def _scale_changed(self, limits):
        """Color limits typed in the Results pane (None: the result's whole range)."""
        if self.viewer.result is not None:
            self.viewer.set_color_limits(limits)
            self._sync_scale()

    def _sync_scale(self):
        """Show the color range drawn in the Results pane's scale fields."""
        rng = self.viewer.color_range()
        if rng:
            self.solve_panel.show_scale(self._shown_group, self.solve_panel.current_field(),
                                        *rng)

    def _show_geometry(self):
        self.viewer.clear_result()

    # ============================================================ info
    def _show_board_summary(self):
        s = self.snapshot
        st = s.stackup
        rows = "".join(
            f"<tr><td>{html.escape(c.name)}</td><td align=right>{c.thickness_mm * 1000:.1f} µm</td>"
            f"<td align=right>{c.z_mid_mm:.3f}</td></tr>" for c in st.copper)
        kinds = classify_nets(s)
        n_pwr = sum(1 for k in kinds.values() if k == "power")
        n_gnd = sum(1 for k in kinds.values() if k == "ground")
        origin = " ".join(filter(None, [s.format_name, s.kicad_version]))
        if s.file:
            origin += f", saved as {s.file}"
        # Values the source did not give, so the results rest on KiPI's placeholders.
        assumed = ("<p><b>Placeholders</b> (not from the board):</p><ul>" +
                   "".join(f"<li>{html.escape(a)}</li>" for a in s.assumed) + "</ul>"
                   if s.assumed else "")
        self.info.setHtml(
            f"<b>{html.escape(s.board_name)}</b><br>"
            f"<small>From {html.escape(origin)}</small><br><br>"
            f"{assumed}"
            f"Board thickness: {st.board_thickness_mm:.3f} mm"
            f"{'' if st.from_board else ' <i>(default stackup)</i>'}"
            f"{' <i>(edited in KiPI)</i>' if st.edited else ''}<br>"
            f"Nets: {len(s.nets)} ({n_pwr} power, {n_gnd} ground)<br>"
            f"Tracks: {len(s.tracks)} &nbsp; Vias: {len(s.vias)} &nbsp; Pads: {len(s.pads)}<br>"
            f"Zones: {len(s.zones)} &nbsp; Footprints: {len(s.footprints)}<br><br>"
            f"<table cellspacing=4><tr><th align=left>Layer</th><th>Cu</th><th>z mm</th></tr>"
            f"{rows}</table><br><i>Left-click copper in the 3D view to identify it.</i>")

    def _probe_deviation(self, dv: float, v_ref: float) -> str:
        """A probed point against its reference: drop or boost from a rail's target voltage, or
        how far a ground sits from its 0 V reference."""
        group = self._tree.groups.get(self.viewer.result.net) if self._tree else None
        if group is not None and group.kind == "ground":
            return f"{abs(dv) * 1e3:.4f} mV {'above' if dv <= 0 else 'below'} 0 V"
        return f"{fmt_drop(dv, '.4f')} (from {v_ref:g} V)"

    def _nearest_layer(self, z: float):
        st = self.snapshot.stackup
        return min(st.copper, key=lambda c: abs(c.z_mid_mm - z))

    def _via_candidates(self):
        """Plated holes that a click can select: (segments (N,2,3) KiCad coords, ids)."""
        scene = self.viewer.result
        if scene is not None:
            return scene.via_segments, scene.via_ids
        st = self.snapshot.stackup
        shown = set(self.viewer.nets())
        segs, ids = [], []
        for v in self.snapshot.vias:
            if v.net not in shown:
                continue
            layers = [st.layer(l) for l in v.layers if st.layer(l) is not None]
            if layers:
                x, y = v.position
                segs.append([(x, y, max(l.z_top_mm for l in layers)),
                             (x, y, min(l.z_bottom_mm for l in layers))])
                ids.append(v.id)
        return np.asarray(segs, dtype=float).reshape(-1, 2, 3), ids

    def _on_click(self, pt, screen_xy):
        if self.snapshot is None:
            return
        # A PI Models name (or dot) selects its part in the current mode's setup (#96).
        marker = self.viewer.pi_marker_at(screen_xy)
        if marker is not None and self._mode_panel().select_part(marker.kind,
                                                                 marker.setup_item):
            what = (f"model {marker.setup_item} ({marker.name})" if marker.kind == "cap"
                    else marker.name)
            self.statusBar().showMessage(f"Selected {what} in the setup", 5000)
            return
        segs, ids = self._via_candidates()
        via_id = None
        if ids:
            d = self.viewer.screen_distance_to_segments(segs, screen_xy)
            i = int(np.argmin(d))
            if d[i] <= VIA_PICK_PX:
                via_id = ids[i]
        if pt is None and via_id is None:
            return
        self._on_pick(pt, via_id)

    def _on_pick(self, pt, via_id=None):
        """Show what was clicked: the copper point ``pt`` (may be None) and/or the plated
        hole ``via_id`` (a via id, or a PTH pad id in result views)."""
        if self.snapshot is None:
            return
        s = self.snapshot
        via_obj = next((v for v in s.vias if v.id == via_id), None) if via_id else None
        pth_obj = next((p for p in s.pads if p.id == via_id), None) if via_id and not via_obj else None
        if pt is None:
            hole = via_obj or pth_obj
            layer_id = (hole.layers if via_obj else hole.layers)[0]
            pt = (hole.position[0], hole.position[1], s.stackup.layer(layer_id).z_top_mm)
        x, y, z = pt
        cu = self._nearest_layer(z)
        shown = set(self.viewer.nets())
        probe_html = ""
        scene = self.viewer.result
        if scene is not None:
            # The scene is named after its group; a group may join several nets.
            group = self._tree.groups.get(scene.net) if self._tree else None
            shown = set(group.nets) if group is not None else {scene.net}
            # Results only exist on layers with this net's copper; pick the nearest of those.
            layers = [s.stackup.layer(l) for l in scene.layers]
            cu = min(layers, key=lambda c: abs(c.z_mid_mm - z))
            val = scene.probe(cu.id, x, y)
            if val is not None:
                dev = self._probe_deviation(val["drop_mV"] * 1e-3, scene.v_ref)
                probe_html = (f"<b>V = {val['V']:.6f} V</b> &nbsp; <b>{dev}</b><br>"
                              f"|J| = {val['J']:.3f} A/mm² &nbsp; P = {val['P']:.4g} mW/mm²")
                self.statusBar().showMessage(
                    f"{cu.name}  x={x:.3f} y={y:.3f}  V={val['V']:.6f}  "
                    f"{dev}  |J|={val['J']:.3f} A/mm²")

        best_via = via_obj      # chosen by on-screen distance in _on_click

        pad_hit = pth_obj
        from shapely.geometry import Point
        from kipi.core.geometry.copper import poly_to_shapely
        p0 = Point(x, y)
        for p in ([] if pad_hit is not None or best_via is not None else s.pads):
            if p.net not in shown or cu.id not in p.shapes:
                continue
            if math.hypot(p.position[0] - x, p.position[1] - y) > 10:
                continue
            for poly in p.shapes[cu.id]:
                g = poly_to_shapely(poly)
                if g is not None and g.buffer(0.01).contains(p0):
                    pad_hit = p
                    break
            if pad_hit:
                break

        self._barrel_status = ""
        lines = [f"<b>Picked</b> x={x:.3f} y={y:.3f} mm on <b>{html.escape(cu.name)}</b>"]
        if probe_html:
            lines.append(probe_html)
        self._picked = None
        if pad_hit is not None:
            p = pad_hit
            self._picked = ("pad", p.id)
            lines.append(f"<b>Pad {html.escape(p.label)}</b> ({p.pad_type})<br>"
                         f"Net: {html.escape(p.net)}<br>"
                         f"Layers: {', '.join(html.escape(s.stackup.layer(l).name) for l in p.layers)}")
            if p.drill_mm[0] > 0:
                lines.append(f"Drill: {p.drill_mm[0]:.3f} × {p.drill_mm[1]:.3f} mm")
            lines.extend(self._barrel_current_lines(p.id))
        elif best_via is not None:
            v = best_via
            self._picked = ("via", v.id)
            names = [s.stackup.layer(l).name for l in v.layers if s.stackup.layer(l)]
            lines.append(f"<b>Via</b> ({v.via_type})<br>Net: {html.escape(v.net)}<br>"
                         f"Drill {v.drill_mm:.3f} mm / pad {v.diameter_mm:.3f} mm<br>"
                         f"Span: {html.escape(names[0])} → {html.escape(names[-1])}")
            lines.extend(self._barrel_current_lines(v.id))
        else:
            # Which net's copper was hit on this layer?
            for net in shown:
                g = self._copper_cache.get(net, {}).get(cu.id)
                if g is not None and g.buffer(0.01).contains(p0):
                    lines.append(f"Copper of net <b>{html.escape(net)}</b>")
                    break
        self.info.setHtml("<br>".join(lines))
        self.btn_select.setEnabled(self._picked is not None and self._source is not None
                                   and self._source.can_select())
        if self._barrel_status:
            self.statusBar().showMessage(self._barrel_status)
        elif not probe_html:
            self.statusBar().showMessage(f"x={x:.3f} y={y:.3f} mm  {cu.name}")

    def _barrel_current_lines(self, owner_id: str):
        if self._result is None or self.viewer.result is None:
            return []
        vr = next((r for r in self._result.fields.vias if r.owner_id == owner_id), None)
        if vr is None:
            return []
        self._barrel_status = (f"{'Via' if vr.kind == 'via' else 'PTH barrel'} current "
                               f"{vr.current * 1e3:.2f} mA")
        return [f"<b>{'Via' if vr.kind == 'via' else 'Barrel'} current: "
                f"{vr.current * 1e3:.2f} mA</b> &nbsp; ({vr.power * 1e3:.3f} mW in the barrel)"]

    def _select_in_source(self):
        source = self._source
        if self._picked is None or source is None or not source.can_select():
            return
        try:
            source.select(*self._picked)
            self.log(f"Selected {self._picked[0]} in {source.name}")
        except Exception as e:  # noqa: BLE001
            self.log(f"Could not select in {source.name}: {e}", logging.WARNING)

    def _unsaved(self) -> bool:
        if self.snapshot is None or self._saved is None:
            return False
        now = copy.copy(self.solve_panel.setup)       # compared, not changed: the net
        now.nets = self._checked_nets()                # list may still be filling in
        saved = self._saved
        # An AC rail or ground left empty follows the VRM's; the AC panel filling it in
        # once the nets are known is not an edit.
        if not saved.ac.rail or not saved.ac.ground:
            saved = copy.deepcopy(saved)
            saved.ac.rail = saved.ac.rail or now.ac.rail
            saved.ac.ground = saved.ac.ground or now.ac.ground
        return now != saved

    def _show_unsaved(self):
        """Outline the Save buttons while the setup has changes not saved."""
        self.setup_bar.set_unsaved(self._unsaved())

    def _keep_or_drop_changes(self) -> bool:
        """The setup file is written only by Save. Before the setup is left (closing, or
        reading a board again), ask about changes not saved; False means stay."""
        if not self._unsaved():
            return True
        answer = QMessageBox.question(
            self, "KiPI", "The PI setup has changes that are not saved. Save them?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Save:
            self._save_setup()
        return answer != QMessageBox.Cancel

    # ============================================================ close
    def closeEvent(self, event):
        if not self._keep_or_drop_changes():
            event.ignore()
            return
        self._model_timer.stop()            # start no kicad-cli for a closed window
        self._changed_timer.stop()
        self._models_pending = False
        self._guess_wanted = False
        self._closed = True
        for task in (self._task, self._model_task):
            if task is not None and task.isRunning():
                task.progress.cancel()
                task.wait(3000)
        self.viewer.close()
        super().closeEvent(event)
