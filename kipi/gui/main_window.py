"""KiPI main window: board browser, 3D copper view and live log."""

from __future__ import annotations

import html
import logging
import math
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QAction, QColor, QIcon, QPixmap, QTextCursor
from qtpy.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSizePolicy, QSlider,
    QSplitter, QStackedWidget, QTabWidget, QTextBrowser, QToolBar, QVBoxLayout, QWidget,
)

from kipi import __version__
from kipi.ac.gui.panel import ACPanel
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.nets import classify_nets, sorted_nets
from kipi.dc.gui.pi_panel import PIPanel
from kipi.dc.pi.tree import fmt_drop
from kipi.gui.stackup_window import StackupWindow
from kipi.gui.viewer3d import Viewer3D
from kipi.gui.worker import QtProgress, Task

log = logging.getLogger("kipi")

_KIND_TAG = {"power": "PWR", "ground": "GND", "signal": ""}
_KIND_COLOR = {"power": "#e07050", "ground": "#70a0e0", "signal": "#a0a0a0"}
MAX_AUTO_NETS = 4
# Analysis modes, chosen in the toolbar: (key, label, setup dock title).
MODES = (("dc", "DC", "PI setup"), ("ac", "AC", "AC setup"))
VIA_PICK_PX = 10      # a click this close to a via barrel on screen selects it


class MainWindow(QMainWindow):
    def __init__(self, kicad=None, board=None, snapshot: Optional[BoardSnapshot] = None):
        super().__init__()
        self.setWindowTitle(f"KiPI {__version__} — Power Integrity")
        self.resize(1500, 950)
        self._kicad = kicad
        self._board = board
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
        self._model_source = None           # (kicad-cli, board file), False if unusable
        self._kicad_cli = None              # found once per session
        self._cli_looked = False
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

        # The central area: the 3D view and, in AC mode, the impedance plot and the board
        # map of the solve (#67).
        from kipi.ac.gui.board_map import BoardMap
        from kipi.ac.gui.plot import ImpedancePlot
        self.views = QTabWidget(self)
        self.views.setDocumentMode(True)
        self.viewer = Viewer3D(self.views)
        self.plot = ImpedancePlot(self.views)
        self.board_map = BoardMap(self.views)
        self.views.addTab(self.viewer, "3D view")
        self.views.addTab(self.plot, "Impedance")
        self.views.addTab(self.board_map, "Board map")
        self.setCentralWidget(self.views)
        self.viewer.on_click(self._on_click)
        self.plot.cursor_moved.connect(self._show_cursor)
        self._set_plot_tab_shown(False)

        self._build_toolbar()
        self._build_left_panel()
        self._build_log_dock()
        self._build_solve_dock()
        self.stackup_window = StackupWindow(self)
        self.stackup_window.stackup_changed.connect(self._stackup_changed)
        self.stackup_window.settings_changed.connect(self.solve_panel.setup_changed.emit)
        self.statusBar().showMessage("Ready")

        if snapshot is not None:
            QTimer.singleShot(0, lambda: self._set_snapshot(snapshot))
        elif board is not None:
            QTimer.singleShot(0, self.load_from_kicad)

    # ================================================================== UI
    def _build_toolbar(self):
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, tip=""):
            a = QAction(text, self)
            a.setToolTip(tip or text)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        self.act_reload = act("Load from KiCad", self.load_from_kicad,
                              "Re-read the board from the running KiCad PCB editor")
        self.chk_refill = QCheckBox("Refill zones", self)
        self.chk_refill.setToolTip("Refill all zones in KiCad before reading (recommended)")
        tb.addWidget(self.chk_refill)
        tb.addSeparator()
        act("Open snapshot…", self.open_snapshot,
            "Load a saved board snapshot (.json / .json.gz), without KiCad running")
        self.act_save = act("Save snapshot…", self.save_snapshot,
                            "Save the extracted board for offline use")
        tb.addSeparator()
        act("Top", lambda: self._show_3d(self.viewer.view_top), "Look down on the top of the board")
        act("Bottom", lambda: self._show_3d(self.viewer.view_bottom),
            "Look up at the bottom of the board")
        act("Iso", lambda: self._show_3d(self.viewer.view_iso), "Isometric view of the board")
        act("Screenshot…", self.save_screenshot,
            "Save the 3D view, or the impedance plot if it is showing, as a PNG")
        tb.addSeparator()
        self.act_open_results = act("Open results…", self.open_results)
        self._set_open_results_tip()
        self.act_stackup = act("Stackup", self.show_stackup,
                               "The board's layers: thicknesses, εr and copper resistivity, "
                               "for every analysis. Unlock to try another stackup.")
        self.act_power_tree = act("Power tree", self.show_power_tree,
                                  "Block diagram of the setup: VRMs (by type), rails, series "
                                  "links, loads and capacitors, in the direction power flows")
        tb.addSeparator()
        self.act_help = act("Help", self.show_help, "Open the KiPI user guide in your browser (F1)")
        self.act_help.setShortcut("F1")
        self.act_about = act("About", self.show_about, "Version, author and project links")

        # Analysis mode, at the far right.
        spacer = QWidget(tb)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel("Mode: ", tb))
        self.mode_buttons = QButtonGroup(self)
        tips = {"dc": "DC power integrity: IR drop, current density, electro-thermal",
                "ac": "AC power integrity: PDN impedance over frequency at probe points"}
        for key, label, _ in MODES:
            rb = QRadioButton(label, tb)
            rb.setObjectName(f"mode_{key}")
            rb.setToolTip(tips[key])
            rb.setChecked(key == self.mode)
            rb.toggled.connect(lambda on, k=key: on and self.set_mode(k))
            self.mode_buttons.addButton(rb)
            tb.addWidget(rb)

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
        # VRMs and series links are shared: show the other panel's edits.
        if mode == "dc":
            self.solve_panel.reload_shared()
        else:
            self.ac_panel.reload()
        self.setup_stack.setCurrentWidget(self.solve_panel if mode == "dc" else self.ac_panel)
        self.solve_dock.setWindowTitle(next(t for k, _, t in MODES if k == mode))
        self._set_open_results_tip()
        self._set_plot_tab_shown(mode == "ac")
        self._sync_pi_markers()
        if mode == "dc":
            if self._tree:
                self._show_group(self.solve_panel.cmb_group.currentText())
        else:
            self.viewer.clear_result()        # DC results do not apply; show the copper
            if self.plot.result is not None:
                self.views.setCurrentWidget(self.plot)
        self.log(f"Analysis mode: {mode.upper()}")

    def _set_open_results_tip(self):
        self.act_open_results.setToolTip(
            "Reopen a saved DC power-tree solve (.kipi-results); load the board first"
            if self.mode == "dc" else
            "Reopen saved AC impedance results (.kipi-ac-results); load the board first")

    def _set_plot_tab_shown(self, shown: bool):
        """Show the impedance and board map tabs only in AC mode. QTabWidget.setTabVisible
        needs Qt 5.15; older Qt disables the tabs instead."""
        for w in (self.plot, self.board_map):
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

    def _check_mode_button(self, mode: str):
        rb = self.findChild(QRadioButton, f"mode_{mode}")
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
        feat_rows = (QHBoxLayout(), QHBoxLayout())
        for r in feat_rows:
            feats.addLayout(r)
        feat_tips = {"vias": "Show via and plated-hole barrels",
                     "pads": "Show pads", "board": "Show the board outline and laminate",
                     "pi": "Mark the DC setup's VRMs (green), loads (cyan) and series "
                           "links (magenta) on the nets shown: a dot on each of their "
                           "pads, larger on the rail and smaller on ground, and the "
                           "part's name. In DC mode, over the copper only, not over "
                           "results.",
                     "models": "Show the 3D models of the parts in the setup (VRMs, loads, "
                               "series links and capacitors) on the nets shown. KiCad's "
                               "kicad-cli reads them from the saved board file. The copper "
                               "and result views each remember this; over results they "
                               "start off."}
        for k, (name, label) in enumerate((("vias", "Vias"), ("pads", "Pads"),
                                           ("board", "Board"), ("pi", "PI Models"),
                                           ("models", "3D Models"))):
            cb = QCheckBox(label, g_view)
            cb.setChecked(True)
            cb.setToolTip(feat_tips[name])
            cb.toggled.connect(lambda v, n=name: self.viewer.set_feature_visible(n, v))
            feat_rows[k // 3].addWidget(cb)
            if name == "pi":
                self.chk_pi = cb
        self.chk_models = cb
        self.chk_models.toggled.connect(lambda _: self._model_timer.start())
        # The copper and result views each keep their own Models setting.
        self.viewer.result_shown.connect(
            lambda _: self.chk_models.setChecked(self.viewer.models_shown))
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
        self.btn_select_kicad = QPushButton("Select in KiCad", g_info)
        self.btn_select_kicad.setEnabled(False)
        self.btn_select_kicad.setToolTip("Select the picked pad or via in the KiCad PCB editor")
        self.btn_select_kicad.clicked.connect(self._select_in_kicad)
        li.addWidget(self.info, 1)
        li.addWidget(self.btn_select_kicad)

        split = QSplitter(Qt.Vertical, panel)
        for w in (g_nets, g_layers, g_view, g_info):
            split.addWidget(w)
        split.setSizes([360, 170, 110, 220])
        lay.addWidget(split)

        dock = QDockWidget("Board", self)
        dock.setObjectName("board_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(panel)
        dock.setMinimumWidth(300)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def _build_solve_dock(self):
        self.solve_panel = PIPanel(self)
        self.solve_panel.solve_requested.connect(self.run_solve)
        self.solve_panel.field_changed.connect(self._field_changed)
        self.solve_panel.group_changed.connect(self._show_group)
        # Picking the group already selected sends no group_changed; after "Show copper"
        # that pick is how the results come back.
        self.solve_panel.cmb_group.activated.connect(self._group_activated)
        self.solve_panel.show_geometry.connect(self._show_geometry)
        self.solve_panel.save_requested.connect(self._save_setup)
        self.solve_panel.arrows_toggled.connect(self.viewer.set_arrows_visible)
        self.solve_panel.via_selected.connect(self._show_via)
        self.solve_panel.export_requested.connect(self._export)
        self._tree = None
        self._scenes = {}
        self._setup_path = None
        self.ac_panel = ACPanel(self)
        self.ac_panel.save_requested.connect(self._save_setup)
        self.ac_panel.solve_requested.connect(self.run_ac_solve)
        self.ac_panel.result_selected.connect(self._show_ac_result)
        self.ac_panel.results_saved.connect(lambda path: self.log(f"Saved AC results to {path}"))
        for panel in (self.solve_panel, self.ac_panel):
            panel.setup_changed.connect(self._model_timer.start)
            panel.setup_changed.connect(self._refresh_power_tree)
        # One setup dock; it shows the panel of the current analysis mode.
        self.setup_stack = QStackedWidget(self)
        self.setup_stack.addWidget(self.solve_panel)
        self.setup_stack.addWidget(self.ac_panel)
        dock = QDockWidget(MODES[0][2], self)
        dock.setObjectName("solve_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(self.setup_stack)
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
        lay.addLayout(row)
        lay.addWidget(self.log_view)
        dock = QDockWidget("Log", self)
        self.log_dock = dock
        dock.setObjectName("log_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(w)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [200], Qt.Vertical)

    # ============================================================ logging
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
        task.done.connect(on_done)
        task.failed.connect(lambda tb: self._on_task_failed(name, tb))
        task.finished.connect(lambda t=task: self._on_task_finished(t))
        self._task = task
        self.btn_cancel.setEnabled(True)
        self.act_reload.setEnabled(False)
        from kipi.gui import app as kipi_app
        kipi_app.watchdog_start()
        log.info("Task started: %s", name)
        task.start()
        return task

    def _on_task_finished(self, task=None):
        # A task's done() may already have started the next task (loading a board
        # starts the net build). Its "finished" arrives after that, and must not reset
        # the UI or stop the watchdog for the task that is now running.
        if task is not None and task is not self._task:
            log.info("Earlier task finished (another task is already running)")
            return
        from kipi.gui import app as kipi_app
        kipi_app.watchdog_stop()
        log.info("Task finished")
        self.solve_panel.set_busy(False)
        self.ac_panel.set_busy(False)
        pending, self._pending_solve = getattr(self, "_pending_solve", None), None
        pending_ac, self._pending_ac = getattr(self, "_pending_ac", None), None
        self.btn_cancel.setEnabled(False)
        self.act_reload.setEnabled(True)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
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

    def _on_task_failed(self, name: str, tb: str):
        log.error(tb)
        self.log(tb, logging.ERROR)
        QMessageBox.critical(self, "KiPI", f"{name} failed:\n\n{tb.strip().splitlines()[-1]}")

    def _cancel_task(self):
        if self._task is not None:
            self._task.progress.cancel()

    # ===================================================== board loading
    def load_from_kicad(self):
        refill = self.chk_refill.isChecked()

        def work(progress):
            from kipi.core.kicad_io.extract import connect, extract_board, require_single_kicad
            require_single_kicad()     # a second KiCad may have been opened since the last load
            if self._board is None:
                self._kicad, self._board = connect(progress=progress)
            snap = extract_board(self._board, refill_zones=refill, progress=progress)
            try:
                snap.kicad_version = str(self._kicad.get_version())
            except Exception:  # noqa: BLE001
                pass
            return snap

        self._run(work, self._set_snapshot, "Loading board")

    def open_snapshot(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open board snapshot", "",
                                              "KiPI snapshot (*.json *.json.gz)")
        if not path:
            return
        from kipi.core.kicad_io.snapshot import load

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
        from kipi.core.kicad_io.snapshot import save
        # A snapshot is the board as KiCad has it; edited thicknesses live in the setup.
        save(dataclasses.replace(self.snapshot, stackup=self._imported_stackup
                                 or self.snapshot.stackup), path)
        self.log(f"Snapshot saved to {path}")

    def save_screenshot(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", "kipi.png", "PNG (*.png)")
        if path:
            if self.views.currentWidget() in (self.plot, self.board_map):
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
        self._picked = None
        self.btn_select_kicad.setEnabled(False)
        self.setWindowTitle(f"KiPI {__version__} — {snap.board_name}")
        self._load_layer_colors(snap)
        self._result = None
        self._tree = None
        self._scenes = {}
        self._pending_stackup = None
        setup = self._load_setup(snap)
        self._imported_stackup = snap.stackup
        self._apply_setup_stackup(setup)
        self._populate_layers()
        self._populate_nets(keep if keep is not None else setup.nets, setup)
        self._show_board_summary()
        from kipi.core.viz.meshes import board_body
        self.viewer.set_board(*board_body(snap))
        self.sync_nets()

    def _load_layer_colors(self, snap: BoardSnapshot):
        """Color the copper layers like the PCB editor's color theme, read again on every
        load so a theme changed in KiCad shows after Load from KiCad."""
        from kipi.core.kicad_io.colors import copper_theme
        from kipi.core.viz.meshes import set_layer_colors
        colors, source = copper_theme(snap.kicad_version)
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
        self._sync_timer.start()

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
        """Make the 3D view match the checked nets.

        Unchecked nets are removed at once. Newly checked nets are built in the
        background and added as they finish; the nets already shown are left alone.
        """
        if self.snapshot is None:
            return
        self.solve_panel.set_nets(list(self._checked_set()))
        self.ac_panel.set_nets(list(self._checked_set()))
        self.solve_panel.setup.nets = self._checked_nets()   # saved with the setup
        self._model_timer.start()
        if self._busy():
            self._resync_pending = True
            return
        wanted = self._checked_nets()
        shown = set(self.viewer.nets())
        for net in shown - set(wanted):
            self.viewer.remove_net(net)
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
            # Runs on the UI thread as each net finishes; skip nets unchecked meanwhile.
            if ns.net not in self._checked_set():
                return
            if not ns.copper:
                self.log(f"{ns.net}: no copper on any layer", logging.WARNING)
            self.viewer.add_net(ns, render=False)
            added.append(ns.net)
            if not self._render_timer.isActive():
                self._render_timer.start()

        def done(built):
            self.viewer.render()
            self.log(f"Added {len(added)} net(s); {len(self.viewer.nets())} net(s) shown")

        task = self._run(work, done, "Building view", on_partial=on_net)
        if task is not None:
            # A canceled build leaves its nets unchecked so the list matches the view.
            task.canceled.connect(lambda: self._set_checked(
                lambda it: it.data(Qt.UserRole) in self.viewer.nets()))

    # ============================================================ PI Models markers
    def _sync_pi_markers(self):
        """Mark the DC setup's VRMs, loads and series links on the ticked nets (#84). The
        markers belong to DC mode: AC mode's view has none."""
        if self._closed:
            return
        from kipi.dc.viz.markers import pi_markers
        markers = (pi_markers(self.snapshot, self.solve_panel.setup, self._checked_set())
                   if self.mode == "dc" and self.snapshot is not None else [])
        self.viewer.set_pi_markers(markers)

    # ============================================================ part models
    def _wanted_models(self) -> List[str]:
        """Parts in the setup with a pad on a ticked net, like the copper drawn."""
        snap = self.snapshot
        if snap is None or not self.viewer.models_wanted:
            return []
        on_nets = {p.footprint for net in self._checked_set()
                   for p in snap.items_on_net(net)[2]}
        return sorted(self.solve_panel.setup.part_refs() & on_nets)

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
        side = {f.reference: f.side for f in snap.footprints}
        shown = {r: (self._model_cache[r], side.get(r, "top")) for r in wanted
                 if self._model_cache.get(r) is not None}
        kicad = self._imported_stackup or snap.stackup
        self.viewer.set_models(shown, snap.stackup.board_thickness_mm,
                               kicad.board_thickness_mm)

    def _models_source(self):
        """``(kicad-cli, board file)``, or False (said once per board) if either is
        missing."""
        if self._model_source is None:
            from kipi.core.kicad_io import models3d
            if not self._cli_looked:
                self._cli_looked = True
                self._kicad_cli = models3d.find_kicad_cli(self._kicad)
            board = models3d.board_file(self.snapshot)
            if self._kicad_cli is None:
                self.log("3D models are read with KiCad's kicad-cli, which KiPI could not "
                         f"find; set {models3d.CLI_ENV} to its path to show them",
                         logging.WARNING)
            elif board is None:
                self.log(f"3D models: the board file {self.snapshot.board_name} was not "
                         "found next to the project, so no models are shown",
                         logging.WARNING)
            self._model_source = ((self._kicad_cli, board)
                                  if self._kicad_cli and board else False)
        return self._model_source

    def _load_models(self, refs: List[str]):
        source = self._models_source()
        if not source:
            self._model_cache.update({r: None for r in refs})
            return
        cli, board = source
        gen = self._model_gen

        def work(progress):
            from kipi.core.kicad_io.models3d import load_models
            from kipi.core.progress import Canceled
            progress.info(f"Loading 3D models of {len(refs)} part(s) with kicad-cli")
            try:
                found = load_models(cli, board, refs, progress)
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
        task.start()

    def _models_loaded(self, gen: int, found: dict):
        if gen != self._model_gen:          # a board loaded since; these are its parts
            return
        self._model_cache.update(found)
        self._sync_models()

    def _model_task_finished(self):
        self._model_task = None
        if self._models_pending:
            self._models_pending = False
            self._model_timer.start()

    # ============================================================ solve
    # ------------------------------------------------------------ setup file
    def _load_setup(self, snap):
        from kipi.dc.pi.model import PISetup, setup_path
        project_dir = snap.project_dir
        if not project_dir and snap.source and snap.source != "kicad":
            project_dir = str(Path(snap.source).parent)
        path = setup_path(snap.board_name, project_dir)
        setup = PISetup()
        label = f"New setup (will be saved as {path.name})"
        if path.exists():
            try:
                setup = PISetup.load(path)
                label = f"Setup: {path}"
                self.log(f"Loaded PI setup {path}")
            except Exception as e:  # noqa: BLE001 - a bad file must not block the board
                self.log(f"Could not read {path}: {e}", logging.WARNING)
                label = f"Could not read {path.name}; starting empty"
        self._setup_path = path
        self.solve_panel.set_board(snap, setup, label)
        self.ac_panel.set_board(snap, setup, label)
        self._refresh_power_tree()
        self.plot.clear()
        self.board_map.clear()
        return setup

    def _save_setup(self):
        if self._setup_path is None:
            return
        if self.snapshot is not None:   # a checkbox change may not have synced yet
            self.solve_panel.setup.nets = self._checked_nets()
        try:
            self.solve_panel.setup.save(self._setup_path)
            self.solve_panel.lbl_file.setText(f"Setup: {self._setup_path}")
            self.ac_panel.set_label(f"Setup: {self._setup_path}")
            self.log(f"Saved PI setup to {self._setup_path}")
        except OSError as e:
            self.log(f"Could not save the setup: {e}", logging.WARNING)

    # ------------------------------------------------------------ solve
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
        self._save_setup()
        import copy
        frozen = copy.deepcopy(setup)     # edits during the solve don't affect it
        report_path = None
        if frozen.html_report and self._setup_path is not None:
            from kipi.dc.report.html import report_name
            report_path = self._setup_path.parent / report_name(self._results_stem())

        def work(progress):
            from kipi.core.mesh.mesher import MeshError
            from kipi.dc.pi.tree import solve_tree
            from kipi.dc.solver import SolveError
            from kipi.dc.viz.results import build_result_scene
            try:
                if frozen.thermal.enabled:
                    from kipi.dc.thermal.cosim import solve_electrothermal
                    tree = solve_electrothermal(snap, frozen, frozen.thermal, progress)
                else:
                    tree = solve_tree(snap, frozen, progress)
            except (SolveError, MeshError) as e:
                progress.warn(str(e))
                return None, str(e)       # a setup problem, reported in the panel
            scenes = {}
            with progress.timed("Building result views"):
                temp = tree.thermal.sampler() if tree.thermal is not None else None
                for name, g in tree.groups.items():
                    progress.check_cancel()
                    progress.info(f"  {name}: {g.result.mesh.n_tris} triangles")
                    scenes[name] = build_result_scene(snap, g.result, temperature=temp)
            if report_path is not None:
                from kipi.dc.report.html import write_report
                with progress.timed("Writing HTML report"):
                    try:
                        tree.report_path = write_report(report_path, tree, scenes, snap, frozen,
                                                        progress)
                    except OSError as e:
                        progress.warn(f"Could not write the report: {e}")
            return tree, scenes

        def done(out):
            tree, scenes = out
            if tree is None:
                self.solve_panel.show_error(scenes)
                return
            self._tree, self._scenes = tree, scenes
            self.solve_panel.set_report(getattr(tree, "report_path", None))
            self._open_new_report(getattr(tree, "report_path", None))
            first = tree.loads[0].group if tree.loads else next(iter(tree.groups))
            self.solve_panel.show_results(tree, first)
            self._show_group(first)
            for l in tree.loads:
                self.log(f"  {l.name}: {fmt_drop(l.drop)} ({abs(l.drop_pct):.3f} %), worst pin "
                         f"{fmt_drop(l.drop_worst)} at {l.current:g} A {l.verdict}".rstrip())

        if self._run(work, done, "Solving power tree") is not None:
            self.solve_panel.set_busy(True)

    def _open_new_report(self, path):
        """Open a report a solve just wrote in the default browser."""
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
        self._save_setup()
        import copy
        frozen = copy.deepcopy(setup)
        report_path = None
        if frozen.ac.html_report and self._setup_path is not None:
            from kipi.ac.html_report import report_name
            report_path = self._setup_path.parent / report_name(self._results_stem(), vrm_name)

        def work(progress):
            from kipi.ac.solver import ACError, solve_ac
            from kipi.core.mesh.mesher import MeshError
            from kipi.dc.solver import SolveError
            try:
                result = solve_ac(snap, frozen, vrm_name, gnd, progress)
            except (ACError, MeshError, SolveError) as e:
                progress.warn(str(e))
                return None, str(e)
            if report_path is not None:
                from kipi.ac.html_report import write_ac_report
                with progress.timed("Writing HTML report"):
                    try:
                        result.report_path = write_ac_report(report_path, result, snap, frozen,
                                                             progress)
                    except Exception as e:        # the solve's results stand without it
                        progress.warn(f"Could not write the report: {type(e).__name__}: {e}")
            return result, None

        def done(out):
            result, error = out
            if result is None:
                self.ac_panel.show_error(error)
                return
            self.ac_panel.show_result(result)
            self._open_new_report(getattr(result, "report_path", None))

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

    def _show_cursor(self, text: str):
        """The impedance plot's cursor values, in the Info pane in place of the last pick."""
        if not text:
            return
        self._picked = None
        self.btn_select_kicad.setEnabled(False)
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
        from kipi.core.model.stackup import to_setup
        from kipi.core.viz.meshes import board_body
        snap.stackup = st
        self.solve_panel.setup.stackup = to_setup(st) if st.edited else None
        self.log(f"Stackup: {'edited in KiPI' if st.edited else 'from KiCad'}, "
                 f"{st.board_thickness_mm:.3f} mm board"
                 + ("; solve again for results with it" if self._tree is not None else ""))
        if self._tree is not None:
            self._tree, self._scenes = None, {}
            self.solve_panel.clear_results()
        self._populate_layers()
        self._show_board_summary()
        self.viewer.set_board(*board_body(snap), reset_view=False)
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
                path, _ = QFileDialog.getSaveFileName(
                    self, "Save results",
                    str(Path(start) / f"{stamped(self._results_stem())}.kipi-results"),
                    "KiPI results (*.kipi-results)")
                if path:
                    report.save_results(path, self._tree, self._scenes, self.snapshot,
                                        self.solve_panel.setup)
                    self.log(f"Saved results to {path}")
                return
            folder = QFileDialog.getExistingDirectory(self, "Export to folder", start)
            if not folder:
                return
            if kind == "csv":
                paths = report.export_csv(self._tree, self.snapshot, folder, self._results_stem(),
                                          self.solve_panel.setup.via_current_limit_a,
                                          self.solve_panel.setup.thermal)
            else:
                paths = report.export_vtk(self._tree, self._scenes, self.snapshot, folder,
                                          self._results_stem())
            self.log(f"Exported {len(paths)} file(s) to {folder}")
        except OSError as e:
            QMessageBox.warning(self, "KiPI", f"Export failed: {e}")

    def open_results(self):
        if self.snapshot is None:
            # The tables and probing need the board (pads, layers); results files hold
            # only the solution.
            QMessageBox.information(self, "KiPI", "Load the board (from KiCad or a snapshot) "
                                                  "before opening its results.")
            return
        if self.mode == "ac":
            self._open_ac_results()
            return
        from kipi.dc.pi import report
        start = str(self._setup_path.parent) if self._setup_path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open results", start,
                                              "KiPI results (*.kipi-results)")
        if not path:
            return
        try:
            data = report.load_results(path)
        except Exception as e:  # noqa: BLE001 - any unreadable file
            QMessageBox.warning(self, "KiPI", f"Could not open {path}:\n{e}")
            return
        if self.snapshot is not None and data["board"] != self.snapshot.board_name:
            self.log(f"Results are for {data['board']}, not {self.snapshot.board_name}",
                     logging.WARNING)
        self._tree, self._scenes = data["tree"], data["scenes"]
        first = self._tree.loads[0].group if self._tree.loads else next(iter(self._tree.groups))
        self.solve_panel.show_results(self._tree, first)
        self._show_group(first)
        self.log(f"Opened results saved {data['saved']} from {path}")

    def _open_ac_results(self):
        from kipi.ac import report
        start = str(self._setup_path.parent) if self._setup_path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open AC results", start,
                                              f"KiPI AC results (*{report.RESULTS_SUFFIX})")
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
        left_out = self.ac_panel.load_results(data["results"])
        if left_out:
            self.log(f"No VRM in the setup for rail(s) {', '.join(left_out)}; their results "
                     f"were left out", logging.WARNING)
        if not data["results"] or len(left_out) == len(data["results"]):
            QMessageBox.information(self, "KiPI", "None of the rails in this file has a VRM "
                                                  "in the setup, so there is nothing to show.")
            return
        self.log(f"Opened AC results saved {data['saved']} from {path}")

    def _show_group(self, name: str):
        if not self._tree or name not in self._scenes:
            return
        self._result = self._tree.groups[name].result
        self.viewer.show_result(self._scenes[name], self.solve_panel.current_field())

    def _group_activated(self, index: int):
        if self.viewer.result is None:
            self._show_group(self.solve_panel.cmb_group.itemText(index))

    def _field_changed(self, field: str):
        """A new result field; from the copper view this brings the results back."""
        if self.viewer.result is None:
            self._show_group(self.solve_panel.cmb_group.currentText())
        else:
            self.viewer.set_result_field(field)

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
        self.info.setHtml(
            f"<b>{html.escape(s.board_name)}</b><br>"
            f"<small>{html.escape(s.source)} {html.escape(s.kicad_version)}</small><br><br>"
            f"Board thickness: {st.board_thickness_mm:.3f} mm"
            f"{'' if st.from_board else ' <i>(default stackup)</i>'}"
            f"{' <i>(edited in KiPI)</i>' if st.edited else ''}<br>"
            f"Nets: {len(s.nets)} ({n_pwr} power, {n_gnd} ground)<br>"
            f"Tracks: {len(s.tracks)} &nbsp; Vias: {len(s.vias)} &nbsp; Pads: {len(s.pads)}<br>"
            f"Zones: {len(s.zones)} &nbsp; Footprints: {len(s.footprints)}<br><br>"
            f"<table cellspacing=4><tr><th align=left>Layer</th><th>Cu</th><th>z mm</th></tr>"
            f"{rows}</table><br><i>Left-click copper in the 3D view to identify it.</i>")

    def _probe_deviation(self, dv: float, v_ref: float) -> str:
        """A probed point against its reference: drop or boost from a rail's set point, or
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
        self.btn_select_kicad.setEnabled(self._picked is not None and self._board is not None)
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

    def _select_in_kicad(self):
        if self._picked is None or self._board is None:
            return
        from kipy.proto.common.types import KIID

        class _Ref:  # add_to_selection only needs an object with an ``id``
            def __init__(self, value):
                self.id = KIID(value=value)

        try:
            self._board.clear_selection()
            self._board.add_to_selection([_Ref(self._picked[1])])
            self.log(f"Selected {self._picked[0]} in KiCad")
        except Exception as e:  # noqa: BLE001
            self.log(f"Could not select in KiCad: {e}", logging.WARNING)

    # ============================================================ close
    def closeEvent(self, event):
        self._model_timer.stop()            # start no kicad-cli for a closed window
        self._models_pending = False
        self._closed = True
        for task in (self._task, self._model_task):
            if task is not None and task.isRunning():
                task.progress.cancel()
                task.wait(3000)
        self.viewer.close()
        super().closeEvent(event)
