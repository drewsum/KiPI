"""The Tolerance window (#167): one row per load with a tolerance, grouped under the VRM
driving it, adding the DC drop, the VRM's DC accuracy, the ripple and the transient against
the load's DC, AC and total tolerances.

The cells show what the tolerance file keeps (by default ``<board>.kipi-tolerance.yml``
beside the setup, opened with the board). A value the results loaded now give differently is
highlighted: clicking it (or Accept all new) takes it into the table, which Save then
writes. A part no result gives yet is greyed, saying what to run. Only the loads ticked in
the Loads pane are shown; a new table shows none.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Dict, List, Optional

from qtpy.QtCore import QEvent, QRectF, QSize, Qt, Signal
from qtpy.QtGui import QBrush, QColor, QFont, QImage, QPainter
from qtpy.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from kipi.dc.pi import vunits
from kipi.gui.report_option import ReportOption
from kipi.gui import tooltip_art
from kipi.tolerance.evaluate import CHECK_NAMES, Stack, has_any_tolerance, pair, stack
from kipi.tolerance.gather import Fresh, Gatherer, Loaded, driver
from kipi.tolerance import model
from kipi.tolerance.model import KIND_NAMES, KINDS, Contribution, ToleranceTable

COLUMNS = ["Load", "Nominal", "DC drop", "VRM accuracy", "DC sum", "Ripple", "Transient",
           "AC sum", "Total", "Verdict"]
PART_COLUMN = {2: "dc", 5: "ripple", 6: "transient"}
SUM_COLUMN = {4: "dc", 7: "ac", 8: "total"}
NEW_BG = QColor(255, 176, 0, 90)            # a value not accepted yet: amber, either theme
GROUP_BG = QColor(128, 128, 128, 50)
MISSING = QColor("#999")
PASS, FAIL = QColor("#60d060"), QColor("#ff6060")
EMPTY = ("Loads can be shown here once they have a tolerance (DC loads setup: Tolerance, DC "
         "tolerance or AC tolerance).")
NONE_PICKED = "No loads ticked yet: tick the ones to show under Loads, on the left."
DIAGRAM = "tolerance_budget"            # the drawing above the table
HINT = ("Each side adds apart: DC drop + VRM accuracy against the DC tolerance, ripple + "
        "transient against the AC tolerance, all of them against the tolerance. A "
        "highlighted value is new from the results loaded now: click it, or Accept all new, "
        "to keep it in the table; Save writes the tolerance file.")


class Banner(QWidget):
    """A drawing from kipi/gui/graphics shown in the window, in its palette (as the
    tooltips' are): its own size where there is room, scaled down to the width where not.
    Without SVG support it has nothing to show (:meth:`available` is False)."""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setProperty("art", name)          # the drawing is in use (tooltip art test)
        self._img: Optional[QImage] = None
        self._w = self._h = 0
        # Any width the window gives it (the drawing shrinks), its height from that width.
        policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._load()

    def _load(self):
        shown = tooltip_art.image(self.name, (), self.palette())
        self._img = QImage(str(shown[0])) if shown else None
        if self._img is not None and self._img.isNull():
            self._img = None
        self._w, self._h = (shown[1], shown[2]) if self._img is not None else (0, 0)
        self.updateGeometry()
        self.update()

    def available(self) -> bool:
        return self._img is not None

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange:
            self._load()                       # a light or dark theme picked meanwhile

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return round(self._h * min(1.0, w / self._w)) if self._w else 0

    def sizeHint(self) -> QSize:
        return QSize(self._w, self._h)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(self.width()))

    def paintEvent(self, event):
        if self._img is None:
            return
        s = min(1.0, self.width() / self._w)
        w, h = self._w * s, self._h * s
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(QRectF((self.width() - w) / 2, 0, w, h), self._img)
        p.end()


class ToleranceWindow(QDialog):
    """Non-modal window holding the view, as the Power tree's: it can be maximized,
    minimized and closed, not docked."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KiPI — Tolerance stack-up")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.resize(1250, 640)
        self.view = ToleranceView(self)
        self.view.changed.connect(self._title)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self._title()

    def _title(self):
        v = self.view
        name = v.path.name if v.path is not None else "not saved"
        self.setWindowTitle(f"KiPI — Tolerance stack-up — {name}"
                            + (" *" if v.unsaved() else ""))


class ToleranceView(QWidget):
    changed = Signal()                          # the table or its file changed
    report_requested = Signal(str, str)         # load name, "html" or "pdf"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup = None
        self.data = ToleranceTable()            # what the tolerance file holds
        self._saved = ToleranceTable()          # as last opened or saved
        self.path: Optional[Path] = None        # the tolerance file; None: not saved yet
        self.start_dir = ""                     # where Save as and Open start
        self.log = lambda text, level=logging.INFO: None   # the main window's log
        self.loaded = Loaded()
        self.gatherer = Gatherer()
        self._rows: List[Optional[tuple]] = []   # per table row: (load, Stack, Fresh); None: VRM
        self._dirty = True

        self.btn_open = QPushButton("Open…", self)
        self.btn_open.setToolTip("Open a tolerance file (.kipi-tolerance.yml)")
        self.btn_open.clicked.connect(self.open_file)
        self.btn_save = QPushButton("Save", self)
        self.btn_save.setToolTip("Save the table to its tolerance file")
        self.btn_save.clicked.connect(self.save)
        self.btn_save_as = QPushButton("Save as…", self)
        self.btn_save_as.setToolTip("Save the table to another tolerance file")
        self.btn_save_as.clicked.connect(self.save_as)

        self.cmb_unit = QComboBox(self)
        for unit, label in vunits.UNITS:
            self.cmb_unit.addItem(label, unit)
        self.cmb_unit.setToolTip("Show each part in mV or % from the nominal voltage, or as "
                                 "the voltage it takes the load to")
        self.cmb_unit.currentIndexChanged.connect(self._fill)
        self.chk_bw = QCheckBox("20 MHz limit", self)
        self.chk_bw.setToolTip("Take the ripple and the transient through a 20 MHz bandwidth "
                               "limit, as a scope's limit shows them. Saved in the tolerance "
                               "file.")
        self.chk_bw.toggled.connect(self._bw_toggled)
        self.btn_accept = QPushButton("Accept all new", self)
        self.btn_accept.setToolTip("Keep every highlighted value in the table")
        self.btn_accept.clicked.connect(self.accept_all)
        self.report_option = ReportOption("the selected load's stack-up", self._report,
                                          lambda _fmt: None, self)
        top = QHBoxLayout()
        for b in (self.btn_open, self.btn_save, self.btn_save_as):
            top.addWidget(b)
        top.addSpacing(12)
        top.addWidget(QLabel("Show", self))
        top.addWidget(self.cmb_unit)
        top.addWidget(self.chk_bw)
        self.banner = Banner(DIAGRAM, self)
        self.chk_diagram = QCheckBox("Diagram", self)
        self.chk_diagram.setToolTip("Show how the tolerances add up, above the table")
        self.chk_diagram.setChecked(True)
        self.chk_diagram.toggled.connect(self.banner.setVisible)
        self.chk_diagram.setVisible(self.banner.available())
        self.banner.setVisible(self.banner.available())
        top.addWidget(self.chk_diagram)
        top.addStretch(1)
        top.addWidget(self.btn_accept)
        top.addWidget(self.report_option)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setToolTip(HINT)
        self.table.cellClicked.connect(self._clicked)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.lbl = QLabel(self)
        self.lbl.setWordWrap(True)
        self.lbl.setStyleSheet("color: #999;")
        right = QWidget(self)
        lr = QVBoxLayout(right)
        lr.setContentsMargins(0, 0, 0, 0)
        lr.addWidget(self.banner)
        lr.addWidget(self.table, 1)
        lr.addWidget(self.lbl)
        self.split = QSplitter(Qt.Horizontal, self)
        self.split.addWidget(self._build_loads_pane())
        self.split.addWidget(right)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([230, 1000])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(top)
        lay.addWidget(self.split, 1)
        self._headers()

    def _build_loads_pane(self) -> QGroupBox:
        """The loads the table shows, ticked as nets are in the main window's Nets pane."""
        g = QGroupBox("Loads", self)
        self.load_filter = QLineEdit(g)
        self.load_filter.setPlaceholderText("Filter loads…")
        self.load_filter.setToolTip("Show only loads whose name, part or net contains this "
                                    "text")
        self.load_filter.textChanged.connect(self._apply_load_filter)
        self.load_list = QListWidget(g)
        self.load_list.setToolTip("Tick the loads the table shows, of those with a tolerance. "
                                  "Saved in the tolerance file.")
        self.load_list.itemChanged.connect(self._load_item_changed)
        self.btn_all = QPushButton("Select all", g)
        self.btn_all.setToolTip("Tick every load listed (those the filter shows)")
        self.btn_all.clicked.connect(lambda: self._tick_listed(True))
        self.btn_none = QPushButton("Select none", g)
        self.btn_none.setToolTip("Untick every load listed (those the filter shows)")
        self.btn_none.clicked.connect(lambda: self._tick_listed(False))
        row = QHBoxLayout()
        row.addWidget(self.btn_all, 1)
        row.addWidget(self.btn_none, 1)
        lay = QVBoxLayout(g)
        lay.addWidget(self.load_filter)
        lay.addWidget(self.load_list, 1)
        lay.addLayout(row)
        return g

    # ------------------------------------------------------------------ inputs
    def set_setup(self, setup) -> None:
        self.setup = setup
        self.refresh()

    def set_table(self, table: ToleranceTable, path: Optional[Path]) -> None:
        """Show ``table``, as saved in ``path`` (None: not saved anywhere yet)."""
        self.data, self._saved = table, copy.deepcopy(table)
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.start_dir = str(self.path.parent)
        self.chk_bw.blockSignals(True)
        self.chk_bw.setChecked(table.bw_limit)
        self.chk_bw.blockSignals(False)
        self.refresh()
        self.changed.emit()

    # ------------------------------------------------------------------ file
    def open_default(self, board: str, project_dir: str) -> None:
        """The board's tolerance file beside its setup, if it has one; else an empty table
        to be saved there."""
        self.start_dir = project_dir or ""
        found = model.find(board, project_dir)
        table, path = ToleranceTable(), model.default_path(board, project_dir)
        if found is not None:
            try:
                table, path = ToleranceTable.load(found), found
                self.log(f"Loaded tolerance file {found}")
            except Exception as e:  # noqa: BLE001 - a bad file must not block the board
                self.log(f"Could not read {found}: {e}", logging.WARNING)
        self.set_table(table, path)

    def unsaved(self) -> bool:
        return self.data != self._saved

    def open_file(self) -> bool:
        if not self.keep_or_drop():
            return False
        path, _ = QFileDialog.getOpenFileName(self, "Open tolerance file", self.start_dir,
                                              f"{model.FILE_FILTER};;All files (*)")
        if not path:
            return False
        try:
            table = ToleranceTable.load(path)
        except Exception as e:  # noqa: BLE001 - any unreadable file
            QMessageBox.warning(self, "KiPI", f"Could not read {Path(path).name}:\n\n{e}")
            return False
        self.set_table(table, path)
        self.log(f"Opened tolerance file {path}")
        return True

    def save(self) -> bool:
        if self.path is None:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
        start = self.path or Path(self.start_dir or ".") / f"kipi{model.SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(self, "Save tolerance file", str(start),
                                              model.FILE_FILTER)
        return bool(path) and self._write(Path(path))

    def _write(self, path: Path) -> bool:
        try:
            self.path = self.data.save(path)
        except OSError as e:
            QMessageBox.warning(self, "KiPI", f"Could not save {path.name}:\n\n{e}")
            return False
        self.start_dir = str(self.path.parent)
        self._saved = copy.deepcopy(self.data)
        self.log(f"Saved the tolerance table to {self.path}")
        self.changed.emit()
        return True

    def keep_or_drop(self) -> bool:
        """Before the table is left (another file, board or closing), ask about changes not
        saved; False means stay."""
        if not self.unsaved():
            return True
        answer = QMessageBox.question(
            self, "KiPI", "The tolerance table has changes that are not saved. Save them?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    # ------------------------------------------------------------------ load filter
    def candidates(self) -> list:
        """The setup's loads with a tolerance, which the filter offers."""
        return [ld for ld in (self.setup.loads if self.setup else []) if has_any_tolerance(ld)]

    def _fill_loads(self) -> None:
        """The Loads pane: every load with a tolerance, ticked if the table shows it."""
        shown = set(self.data.loads)
        lst = self.load_list
        loads = self.candidates()
        lst.blockSignals(True)
        if [ld.name for ld in loads] == [lst.item(i).data(Qt.UserRole)
                                         for i in range(lst.count())]:
            # The same loads: only the ticks (no rebuild under a click being handled).
            for i in range(lst.count()):
                want = Qt.Checked if lst.item(i).data(Qt.UserRole) in shown else Qt.Unchecked
                if lst.item(i).checkState() != want:
                    lst.item(i).setCheckState(want)
            lst.blockSignals(False)
            return
        lst.clear()
        for ld in loads:
            it = QListWidgetItem(f"{ld.name}  ({ld.supply.ref} on {ld.supply.net})", lst)
            it.setData(Qt.UserRole, ld.name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if ld.name in shown else Qt.Unchecked)
        lst.blockSignals(False)
        self._apply_load_filter()

    def _apply_load_filter(self, *_):
        text = self.load_filter.text().strip().lower()
        for i in range(self.load_list.count()):
            it = self.load_list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def _ticked(self) -> List[str]:
        lst = self.load_list
        return [lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                if lst.item(i).checkState() == Qt.Checked]

    def _load_item_changed(self, _item):
        self.set_loads(self._ticked())

    def _tick_listed(self, on: bool) -> None:
        lst = self.load_list
        lst.blockSignals(True)
        for i in range(lst.count()):
            if not lst.item(i).isHidden():
                lst.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        lst.blockSignals(False)
        self.set_loads(self._ticked())

    def set_loads(self, names: List[str]) -> None:
        """Show ``names`` of the loads offered; picks of loads the setup lacks now stay."""
        offered = {ld.name for ld in self.candidates()}
        loads = [n for n in self.data.loads if n not in offered] + list(names)
        if set(loads) != set(self.data.loads):
            self.data.loads = loads
            self._fill()
            self.changed.emit()

    def set_dc(self, tree, source: str = "", when: str = "") -> None:
        self.loaded.dc = (tree, source, when) if tree is not None else None
        self.refresh()

    def add_ac(self, result, source: str = "", when: str = "") -> None:
        self.loaded.ac[result.rail] = (result, source, when)
        self.refresh()

    def set_ac(self, results: Dict[str, object], source: str = "", when: str = "") -> None:
        self.loaded.ac = {k: (r, source, when) for k, r in results.items()}
        self.refresh()

    def add_noise(self, result, source: str = "", when: str = "") -> None:
        self.loaded.noise[result.aggressor] = (result, source, when)
        self.refresh()

    def set_noise(self, results: Dict[str, object], source: str = "", when: str = "") -> None:
        self.loaded.noise = {k: (r, source, when) for k, r in results.items()}
        self.refresh()

    def clear_results(self) -> None:
        self.loaded = Loaded()
        self.refresh()

    def keep(self, ac: Dict[str, object], noise: Dict[str, object]) -> None:
        """Forget the AC and Noise results the panels no longer hold (their VRM or
        aggressor was removed)."""
        self.loaded.ac = {k: v for k, v in self.loaded.ac.items() if ac.get(k) is v[0]}
        self.loaded.noise = {k: v for k, v in self.loaded.noise.items()
                             if noise.get(k) is v[0]}
        self.refresh()

    # ------------------------------------------------------------------ work
    def refresh(self, force: bool = False) -> None:
        """Work the rows out again: now if shown (or ``force``), else when next shown."""
        self._dirty = True
        if force or self.isVisible():
            self._fill()

    def showEvent(self, event):
        super().showEvent(event)
        if self._dirty:
            self._fill()

    def unit(self) -> str:
        return self.cmb_unit.currentData() or vunits.DEFAULT

    def _headers(self):
        u = {vunits.MV: "mV", vunits.PCT: "%", vunits.VOLTS: "V"}[self.unit()]
        self.table.setHorizontalHeaderLabels(
            [c if i in (0, 1, 9) else f"{c} ({u})" for i, c in enumerate(COLUMNS)])

    def rows(self) -> List[tuple]:
        """(VRM or None, [load, ...]) in the setup's order: the loads picked that have a
        tolerance."""
        from kipi.dc.pi.model import net_groups
        setup = self.setup
        if setup is None:
            return []
        groups = net_groups(setup)
        shown = set(self.data.loads)
        by_vrm: Dict[str, list] = {}
        for ld in setup.loads:
            if ld.name in shown and has_any_tolerance(ld):
                v = driver(setup, ld, groups)
                by_vrm.setdefault(v.name if v else "", []).append(ld)
        out = [(v, by_vrm[v.name]) for v in setup.vrms if v.name in by_vrm]
        if "" in by_vrm:
            out.append((None, by_vrm[""]))
        return out

    def _fill(self, *_):
        self._dirty = False
        self._headers()
        self._fill_loads()
        t = self.table
        keep = self.selected_load()
        t.clearSpans()
        t.setRowCount(0)
        self._rows = []
        setup = self.setup
        if setup is None:
            self.lbl.setText(EMPTY)
            self._update_buttons()
            return
        from kipi.dc.pi.model import net_groups
        groups = net_groups(setup)
        bw = self.data.bw_limit
        unit = self.unit()
        live = []
        for vrm, loads in self.rows():
            r = t.rowCount()
            t.insertRow(r)
            self._rows.append(None)
            label = (f"{vrm.name}: {vrm.output.net}, {vrm.voltage:g} V" if vrm is not None
                     else "No VRM drives these loads' rails")
            head = QTableWidgetItem(label)
            font = QFont(head.font())
            font.setBold(True)
            head.setFont(font)
            head.setBackground(QBrush(GROUP_BG))
            head.setFlags(Qt.ItemIsEnabled)
            t.setItem(r, 0, head)
            t.setSpan(r, 0, 1, len(COLUMNS))
            for ld in loads:
                fresh = self.gatherer.fresh(setup, ld, self.loaded, groups)
                st = stack(ld, self.data.entries.get(ld.name), vrm, bw,
                           fresh.not_applicable)
                r = t.rowCount()
                t.insertRow(r)
                self._rows.append((ld, st, fresh))
                self._fill_row(r, ld, st, fresh, unit, bw)
        for v in self.loaded.ac.values():
            live.append(v[0])
        for v in self.loaded.noise.values():
            live.append(v[0])
        self.gatherer.forget(live)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        n = sum(1 for x in self._rows if x)
        self.lbl.setText((NONE_PICKED if self.candidates() else EMPTY) if not n else
                         f"{n} load(s). " + ("Ripple and transient through a 20 MHz limit."
                                             if bw else "Ripple and transient at full "
                                                        "bandwidth."))
        if keep:
            self.select_load(keep)
        self._update_buttons()

    def _fill_row(self, r: int, ld, st: Stack, fresh: Fresh, unit: str, bw: bool):
        t = self.table
        nominal = st.nominal
        entry = self.data.entries.get(ld.name)
        name = QTableWidgetItem(ld.name)
        name.setToolTip(f"{ld.supply.ref} on {ld.supply.net}\nTolerance: "
                        f"{ld.tolerance_label() or 'not set'}\nDC tolerance: "
                        f"{ld.dc_tolerance_label() or 'not set'}\nAC tolerance: "
                        f"{ld.ac_tolerance_label() or 'not set'}")
        t.setItem(r, 0, name)
        t.setItem(r, 1, QTableWidgetItem(f"{nominal:g} V" if nominal is not None else ""))
        for col, kind in PART_COLUMN.items():
            t.setItem(r, col, self._part_item(kind, entry.get(kind) if entry else None,
                                              fresh, nominal, unit, bw))
        acc = QTableWidgetItem(pair(st.parts.get("accuracy"), nominal, unit))
        if not st.accuracy_set:
            acc.setForeground(QBrush(MISSING))
            acc.setToolTip("The VRM has no DC accuracy set (VRM setup); taken as 0")
        t.setItem(r, 3, acc)
        for col, kind in SUM_COLUMN.items():
            c = st.checks[kind]
            text = pair((c.below_v, c.above_v), nominal, unit)
            if c.has_limit:
                text += "  of " + pair(c.allowed, nominal, unit)     # "–" for an open end
            if not c.complete:
                text += " …"
            item = QTableWidgetItem(text)
            if c.passed is not None:
                item.setForeground(QBrush(PASS if c.passed else FAIL))
            tip = f"{CHECK_NAMES[kind]}: "
            if c.has_limit:
                tip += "margin " + ", ".join(
                    f"{where} {'not limited' if m is None else f'{m * 1e3:.3f} mV'}"
                    for where, m in zip(("below", "above"), c.margins()))
            else:
                tip += "no tolerance set"
            if not c.complete:
                tip += "\nNot every part is known yet: it can fail but not pass"
            item.setToolTip(tip)
            t.setItem(r, col, item)
        v = st.verdict
        verdict = QTableWidgetItem("PASS" if v else "FAIL" if v is False else "incomplete")
        verdict.setForeground(QBrush(PASS if v else FAIL if v is False else MISSING))
        t.setItem(r, 9, verdict)

    def _part_item(self, kind: str, kept: Optional[Contribution], fresh: Fresh,
                   nominal, unit: str, bw: bool) -> QTableWidgetItem:
        new = fresh.values.get(kind)
        if new is not None and not new.same_values(kept):
            item = QTableWidgetItem(pair(new.sides(bw), nominal, unit))
            item.setBackground(QBrush(NEW_BG))
            was = (f"kept: {pair(kept.sides(bw), nominal, vunits.MV)} mV from {kept.source} "
                   f"{kept.when}" if kept is not None else "nothing kept yet")
            item.setToolTip(f"New from {new.source} {new.when}: {new.detail}\n{was}\n"
                            "Click to keep it in the table.")
            return item
        if kept is not None:
            item = QTableWidgetItem(pair(kept.sides(bw), nominal, unit))
            item.setToolTip(f"From {kept.source} {kept.when}: {kept.detail}"
                            + (f"\n{fresh.hints[kind]}" if kind in fresh.hints else ""))
            return item
        if kind in fresh.not_applicable:
            item = QTableWidgetItem("—")
            item.setToolTip(f"Not applicable: {fresh.not_applicable[kind]}")
            item.setForeground(QBrush(MISSING))
            return item
        item = QTableWidgetItem(fresh.short.get(kind, ""))
        item.setForeground(QBrush(MISSING))
        font = QFont(item.font())
        font.setItalic(True)
        item.setFont(font)
        item.setToolTip(f"{KIND_NAMES[kind]} not known yet: {fresh.hints.get(kind, '')}")
        return item

    # ------------------------------------------------------------------ accepting
    def pending(self) -> List[tuple]:
        """(load name, kind, Contribution) for every highlighted value."""
        out = []
        for row in self._rows:
            if row is None:
                continue
            ld, _, fresh = row
            entry = self.data.entries.get(ld.name)
            for kind in KINDS:
                new = fresh.values.get(kind)
                if new is not None and not new.same_values(entry.get(kind) if entry else None):
                    out.append((ld.name, kind, new))
        return out

    def accept(self, load: str, kind: str) -> bool:
        for name, k, new in self.pending():
            if (name, k) == (load, kind):
                self.data.accept(name, k, new)
                self._fill()
                self.changed.emit()
                return True
        return False

    def accept_all(self) -> None:
        todo = self.pending()
        for name, kind, new in todo:
            self.data.accept(name, kind, new)
        if todo:
            self._fill()
            self.changed.emit()

    def _clicked(self, row: int, col: int):
        if col in PART_COLUMN and 0 <= row < len(self._rows) and self._rows[row]:
            self.accept(self._rows[row][0].name, PART_COLUMN[col])

    def _bw_toggled(self, on: bool):
        self.data.bw_limit = on
        self._fill()
        self.changed.emit()

    # ------------------------------------------------------------------ selection, report
    def selected_load(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        r = rows[0].row()
        return self._rows[r][0].name if r < len(self._rows) and self._rows[r] else None

    def select_load(self, name: str) -> None:
        for r, row in enumerate(self._rows):
            if row and row[0].name == name:
                self.table.selectRow(r)
                return

    def rows_shown(self) -> list:
        """The loads with a row now."""
        return [row[0] for row in self._rows if row]

    def stack_of(self, name: str):
        """(load, Stack, Fresh) of a load shown, or None."""
        return next((row for row in self._rows if row and row[0].name == name), None)

    def _selection_changed(self):
        self._update_buttons()

    def _update_buttons(self):
        self.btn_accept.setEnabled(self.setup is not None and bool(self.pending()))
        self.report_option.set_available(self.selected_load() is not None)

    def _report(self, fmt: str):
        name = self.selected_load()
        if name:
            self.report_requested.emit(name, fmt)
