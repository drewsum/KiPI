"""The Tolerance window (#167): one row per load with a tolerance, grouped under the VRM
driving it, adding the DC drop, the VRM's DC accuracy, the ripple and the transient against
the load's DC, AC and total tolerances.

The cells show what the setup keeps. A value the results loaded now give differently is
highlighted: clicking it (or Accept all new) takes it into the setup, which Save then
writes. A part no result gives yet is greyed, saying what to run.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qtpy.QtCore import QEvent, QRectF, QSize, Qt, Signal
from qtpy.QtGui import QBrush, QColor, QFont, QImage, QPainter
from qtpy.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSizePolicy,
    QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from kipi.dc.pi import vunits
from kipi.gui.report_option import ReportOption
from kipi.gui import tooltip_art
from kipi.tolerance.evaluate import CHECK_NAMES, Stack, has_any_tolerance, pair, stack
from kipi.tolerance.gather import Fresh, Gatherer, Loaded, driver
from kipi.tolerance.model import KIND_NAMES, KINDS, Contribution

COLUMNS = ["Load", "Nominal", "DC drop", "VRM accuracy", "DC sum", "Ripple", "Transient",
           "AC sum", "Total", "Verdict"]
PART_COLUMN = {2: "dc", 5: "ripple", 6: "transient"}
SUM_COLUMN = {4: "dc", 7: "ac", 8: "total"}
NEW_BG = QColor(255, 176, 0, 90)            # a value not accepted yet: amber, either theme
GROUP_BG = QColor(128, 128, 128, 50)
MISSING = QColor("#999")
PASS, FAIL = QColor("#60d060"), QColor("#ff6060")
EMPTY = ("Loads appear here once they have a tolerance (DC loads setup: Tolerance, DC "
         "tolerance or AC tolerance).")
DIAGRAM = "tolerance_budget"            # the drawing above the table
HINT = ("Each side adds apart: DC drop + VRM accuracy against the DC tolerance, ripple + "
        "transient against the AC tolerance, all of them against the tolerance. A "
        "highlighted value is new from the results loaded now: click it, or Accept all new, "
        "to keep it in the setup; Save writes it.")


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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)


class ToleranceView(QWidget):
    changed = Signal()                          # the setup's tolerance table was edited
    report_requested = Signal(str, str)         # load name, "html" or "pdf"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup = None
        self.loaded = Loaded()
        self.gatherer = Gatherer()
        self._rows: List[Optional[tuple]] = []   # per table row: (load, Stack, Fresh); None: VRM
        self._dirty = True

        self.cmb_unit = QComboBox(self)
        for unit, label in vunits.UNITS:
            self.cmb_unit.addItem(label, unit)
        self.cmb_unit.setToolTip("Show each part in mV or % from the nominal voltage, or as "
                                 "the voltage it takes the load to")
        self.cmb_unit.currentIndexChanged.connect(self._fill)
        self.chk_bw = QCheckBox("20 MHz limit", self)
        self.chk_bw.setToolTip("Take the ripple and the transient through a 20 MHz bandwidth "
                               "limit, as a scope's limit shows them. Saved with the setup.")
        self.chk_bw.toggled.connect(self._bw_toggled)
        self.btn_accept = QPushButton("Accept all new", self)
        self.btn_accept.setToolTip("Keep every highlighted value in the setup")
        self.btn_accept.clicked.connect(self.accept_all)
        self.report_option = ReportOption("the selected load's stack-up", self._report,
                                          lambda _fmt: None, self)
        top = QHBoxLayout()
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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(top)
        lay.addWidget(self.banner)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.lbl)
        self._headers()

    # ------------------------------------------------------------------ inputs
    def set_setup(self, setup) -> None:
        self.setup = setup
        self.chk_bw.blockSignals(True)
        self.chk_bw.setChecked(bool(setup and setup.tolerance.bw_limit))
        self.chk_bw.blockSignals(False)
        self.refresh()

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
        """(VRM or None, [load, ...]) in the setup's order, loads with a tolerance only."""
        from kipi.dc.pi.model import net_groups
        setup = self.setup
        if setup is None:
            return []
        groups = net_groups(setup)
        by_vrm: Dict[str, list] = {}
        for ld in setup.loads:
            if has_any_tolerance(ld):
                v = driver(setup, ld, groups)
                by_vrm.setdefault(v.name if v else "", []).append(ld)
        out = [(v, by_vrm[v.name]) for v in setup.vrms if v.name in by_vrm]
        if "" in by_vrm:
            out.append((None, by_vrm[""]))
        return out

    def _fill(self, *_):
        self._dirty = False
        self._headers()
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
        bw = setup.tolerance.bw_limit
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
                st = stack(ld, setup.tolerance.entries.get(ld.name), vrm, bw,
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
        self.lbl.setText(EMPTY if not n else
                         f"{n} load(s). " + ("Ripple and transient through a 20 MHz limit."
                                             if bw else "Ripple and transient at full "
                                                        "bandwidth."))
        if keep:
            self.select_load(keep)
        self._update_buttons()

    def _fill_row(self, r: int, ld, st: Stack, fresh: Fresh, unit: str, bw: bool):
        t = self.table
        nominal = st.nominal
        entry = self.setup.tolerance.entries.get(ld.name)
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
                            "Click to keep it in the setup.")
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
            entry = self.setup.tolerance.entries.get(ld.name)
            for kind in KINDS:
                new = fresh.values.get(kind)
                if new is not None and not new.same_values(entry.get(kind) if entry else None):
                    out.append((ld.name, kind, new))
        return out

    def accept(self, load: str, kind: str) -> bool:
        for name, k, new in self.pending():
            if (name, k) == (load, kind):
                self.setup.tolerance.accept(name, k, new)
                self._fill()
                self.changed.emit()
                return True
        return False

    def accept_all(self) -> None:
        todo = self.pending()
        for name, kind, new in todo:
            self.setup.tolerance.accept(name, kind, new)
        if todo:
            self._fill()
            self.changed.emit()

    def _clicked(self, row: int, col: int):
        if col in PART_COLUMN and 0 <= row < len(self._rows) and self._rows[row]:
            self.accept(self._rows[row][0].name, PART_COLUMN[col])

    def _bw_toggled(self, on: bool):
        if self.setup is None:
            return
        self.setup.tolerance.bw_limit = on
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
