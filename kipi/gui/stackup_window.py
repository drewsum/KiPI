"""The Stackup window: the board's layers with their thicknesses, materials, εr and
resistivity, for every analysis (#61).

Locked (the default), the thicknesses, materials and εr are the ones read from KiCad.
Unlocked, each can be changed to compare stackups (choosing a dielectric's material sets its
typical εr and thermal conductivity); every analysis then uses the edited stackup. KiCad's
API cannot change a board's stackup yet, so edits stay in KiPI. A copper layer's conductor,
the copper resistivity and the TCR are setup settings and can always be changed: choosing a
conductor sets the layer's resistivity, TCR and thermal conductivity.
"""

from __future__ import annotations

from functools import partial
from typing import Dict, List, Optional

from qtpy.QtCore import QSettings, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QIcon, QPixmap
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from kipi.core.model.board import CopperLayer, Stackup
from kipi.core.model.conductors import UOHM_CM, conductors, find, layer_k_w_mk
from kipi.core.model.stackup import (
    dielectric_er, dielectric_materials, restack, stack_order, thicknesses,
)
from kipi.dc.thermal.materials import catalog, lookup
from kipi.gui.unit_spin import UnitSpinBox

# Thickness units, as mm per unit. 1 oz/ft² is taken as 35 µm, the usual nominal value
# (KiCad's default 35 µm and 17.5 µm foils read as 1 oz and ½ oz). Ounces are a copper
# weight, so in "oz" the dielectrics show in mil.
THICKNESS_UNITS = {"µm": 1e-3, "mm": 1.0, "mil": 0.0254, "oz": 0.035}
UNIT_DECIMALS = {"µm": 1, "mm": 4, "mil": 2, "oz": 3}
MIN_MM, MAX_MM = 0.001, 10.0
COLS = ["Layer", "Material", "Thickness", "εr", "k (W/m·K)", "Resistivity at 20 °C"]
C_NAME, C_MAT, C_THK, C_ER, C_K, C_RHO = range(len(COLS))
BOARD_DEFAULT = "Copper (board Resistivity)"     # no conductor chosen: the settings below
CUSTOM = "Custom resistivity"


def row_unit(unit: str, copper: bool) -> str:
    """The unit a layer's thickness is shown in when the window's unit is ``unit``."""
    return "mil" if unit == "oz" and not copper else unit


def _spin(lo, hi, value, decimals, suffix, step) -> UnitSpinBox:
    s = UnitSpinBox()
    s.setShownDecimals(decimals)
    s.setRange(lo, hi)
    s.setValue(value)
    s.setSuffix(suffix)
    s.setSingleStep(step)
    return s


class StackupWindow(QDialog):
    """Views and edits a board's stackup. ``stackup_changed`` carries the stackup the
    analyses should use: the imported one while locked, else the edited one."""

    stackup_changed = Signal(object)
    settings_changed = Signal()          # resistivity or TCR

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stackup")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._imported: Optional[Stackup] = None
        self._current: Optional[Stackup] = None
        self._setup = None
        self._colors: Dict[int, tuple] = {}
        self._order: list = []
        self._mm: List[float] = []
        self._er: List[float] = []                   # dielectrics, top to bottom
        self._mat: List[str] = []                    # dielectrics' materials, top to bottom
        self._mat_boxes: List[QComboBox] = []
        self._cond_boxes: Dict[str, QComboBox] = {}  # copper layer name -> conductor
        self._thk_spins: List[UnitSpinBox] = []
        self._er_spins: List[UnitSpinBox] = []
        self._rho_spins: List[UnitSpinBox] = []
        self._timer = QTimer(self)                   # one update after a burst of edits
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._emit_edit)

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.chk_lock = QCheckBox("Lock to imported values", self)
        self.chk_lock.setChecked(True)
        self.chk_lock.setToolTip("Use the thicknesses, materials and εr read from KiCad. "
                                 "Untick to try others; every analysis uses them, and the "
                                 "setup file keeps them.")
        self.chk_lock.toggled.connect(self._lock_toggled)
        self.cmb_unit = QComboBox(self)
        self.cmb_unit.addItems(list(THICKNESS_UNITS))
        self.cmb_unit.setToolTip("Unit for the thicknesses (1 oz = 35 µm). In oz, the "
                                 "dielectrics show in mil.")
        unit = QSettings("KiPI", "KiPI").value("stackup/thickness_unit", "µm")
        self.cmb_unit.setCurrentText(unit if unit in THICKNESS_UNITS else "µm")
        self.cmb_unit.currentTextChanged.connect(self._unit_changed)
        top.addWidget(self.chk_lock)
        top.addStretch(1)
        top.addWidget(QLabel("Thickness unit", self))
        top.addWidget(self.cmb_unit)

        self.table = QTableWidget(0, len(COLS), self)
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 10)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        for c in range(len(COLS)):
            hdr.setSectionResizeMode(c, QHeaderView.Stretch if c == C_MAT
                                     else QHeaderView.ResizeToContents)
        self.table.setToolTip("The stackup, top to bottom. Values take units and prefixes, "
                              "like 35um or 0.2mm in a µm or mm field.")
        self.lbl_total = QLabel(self)

        g_cu = QGroupBox("Copper", self)
        f = QFormLayout(g_cu)
        self.spin_rho = _spin(0.5, 100.0, 1.724, 3, " µΩ·cm", 0.01)
        self.spin_rho.setToolTip("Resistivity at 20 °C of every layer without its own value, "
                                 "and of via barrel plating. Annealed copper is 1.724 µΩ·cm.")
        self.spin_tcr = _spin(0.0, 0.02, 0.00393, 5, " /°C", 0.0001)
        self.spin_tcr.setToolTip("Temperature coefficient of resistance, used by the "
                                 "electro-thermal co-simulation. Copper is 0.00393 /°C.")
        f.addRow("Resistivity", self.spin_rho)
        f.addRow("TCR", self.spin_tcr)
        for sp in (self.spin_rho, self.spin_tcr):
            f.labelForField(sp).setToolTip(sp.toolTip())
            sp.valueChanged.connect(self._copper_changed)
        note = QLabel("These apply to every copper layer left at the board's copper, and "
                      "to via plating. A layer's conductor, or a resistivity typed in the "
                      "table, overrides them. Conductors, resistivity and TCR are setup "
                      "settings, not locked.", g_cu)
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        f.addRow(note)

        row = QHBoxLayout()
        self.btn_reset = QPushButton("Reset", self)
        self.btn_reset.setToolTip("Go back to the thicknesses, materials and εr read from "
                                  "KiCad")
        self.btn_reset.clicked.connect(self.reset)
        btn_close = QPushButton("Close", self)
        btn_close.setToolTip("Close the window; the stackup stays as set here")
        btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_reset)
        row.addStretch(1)
        row.addWidget(btn_close)

        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.lbl_total)
        lay.addWidget(g_cu)
        lay.addLayout(row)
        self.resize(900, 560)
        self._update_enabled()

    # ------------------------------------------------------------------ state
    def set_board(self, imported: Optional[Stackup], current: Optional[Stackup] = None,
                  setup=None, colors=None):
        """Show a board: ``imported`` as read from KiCad, ``current`` the one in use (edited,
        from the setup file), the setup holding the resistivity, and ``colors``
        {copper id: (r, g, b)}."""
        self._timer.stop()
        self._imported = imported
        self._current = current or imported
        self._setup = setup
        self._colors = dict(colors or {})
        self.chk_lock.blockSignals(True)
        self.chk_lock.setChecked(self._current is None or not self._current.edited)
        self.chk_lock.blockSignals(False)
        if setup is not None:
            for sp, v in ((self.spin_rho, setup.settings.rho_ohm_mm / UOHM_CM),
                          (self.spin_tcr, setup.settings.tcr_per_c)):
                sp.blockSignals(True)
                sp.setValue(v)
                sp.blockSignals(False)
        self._fill()
        self._update_enabled()

    def stackup(self) -> Optional[Stackup]:
        return self._current

    def unit(self) -> str:
        return self.cmb_unit.currentText()

    def reset(self):
        """Back to the imported thicknesses, materials and εr (the lock is left as it is)."""
        if self._imported is None:
            return
        self._mm = thicknesses(self._imported)
        self._er = dielectric_er(self._imported)
        self._mat = dielectric_materials(self._imported)
        self._show_values()
        self._timer.stop()
        self._apply(self._imported)

    def flush(self):
        """Apply an edit still waiting for the update timer."""
        if self._timer.isActive():
            self._timer.stop()
            self._emit_edit()

    def done(self, r):           # closing the window applies what was typed last
        self.flush()
        super().done(r)

    # ------------------------------------------------------------------ table
    def _fill(self):
        st = self._current
        self._thk_spins, self._er_spins, self._rho_spins = [], [], []
        self._mat_boxes = []
        self._cond_boxes = {}
        self.table.setRowCount(0)
        if st is None:
            self._order, self._mm, self._er, self._mat = [], [], [], []
            self.lbl_total.setText("Load a board to see its stackup.")
            return
        names = {c.id: c.name for c in st.copper}
        self._order = stack_order(st)
        self._mm = thicknesses(st)
        self._er = dielectric_er(st)
        self._mat = dielectric_materials(st)
        kicad_mats = dielectric_materials(self._imported) if self._imported else self._mat
        overrides = self._setup.settings.rho_by_layer if self._setup is not None else {}
        self.table.setRowCount(len(self._order))
        d = 0
        for i, e in enumerate(self._order):
            copper = isinstance(e, CopperLayer)
            if copper:
                item = QTableWidgetItem(e.name)
                if e.id in self._colors:
                    pm = QPixmap(12, 12)
                    pm.fill(QColor.fromRgbF(*self._colors[e.id]))
                    item.setIcon(QIcon(pm))
                self.table.setItem(i, C_NAME, item)
                box = self._conductor_box(e.name)
                self.table.setCellWidget(i, C_MAT, box)
                self._cond_boxes[e.name] = box
            else:
                between = (f"Between {names.get(e.above_layer, 'the top')} and "
                           f"{names.get(e.below_layer, 'the bottom')}")
                item = QTableWidgetItem("   Dielectric")
                item.setToolTip(between)
                self.table.setItem(i, C_NAME, item)
                box = self._material_box(d, kicad_mats[d] if d < len(kicad_mats) else "")
                box.setToolTip(f"{between}. Choosing a material sets its typical εr and "
                               "in-plane thermal conductivity (k, for the electro-thermal "
                               "solve).")
                self.table.setCellWidget(i, C_MAT, box)
                self._mat_boxes.append(box)
            thk = UnitSpinBox(self.table)
            thk.setFrame(False)
            thk.valueChanged.connect(partial(self._thickness_edited, i))
            self.table.setCellWidget(i, C_THK, thk)
            self._thk_spins.append(thk)
            if copper:
                self.table.setItem(i, C_ER, QTableWidgetItem(""))
                k = QTableWidgetItem()
                k.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, C_K, k)
                rho = _spin(0.0, 100.0, overrides.get(e.name, 0.0) / UOHM_CM, 3,
                            " µΩ·cm", 0.01)
                rho.setSpecialValueText("default")
                rho.setFrame(False)
                rho.setToolTip(f"Resistivity of {e.name} at 20 °C ('default' uses the "
                               "Resistivity setting below)")
                rho.valueChanged.connect(partial(self._layer_rho_changed, e.name))
                self.table.setCellWidget(i, C_RHO, rho)
                self._rho_spins.append(rho)
                self._show_conductor(e.name, i)
            else:
                er = _spin(0.0, 20.0, self._er[d], 2, "", 0.1)
                er.setSpecialValueText("default")
                er.setFrame(False)
                er.setToolTip("Dielectric constant ('default' uses the AC setup's Default "
                              "εr)")
                er.valueChanged.connect(partial(self._er_edited, d))
                self.table.setCellWidget(i, C_ER, er)
                self._er_spins.append(er)
                k = QTableWidgetItem()
                k.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, C_K, k)
                self.table.setItem(i, C_RHO, QTableWidgetItem(""))
                d += 1
        self._show_values()

    def _conductor_box(self, layer: str) -> QComboBox:
        """A copper layer's conductor: the setup's board-wide copper, or one KiPI knows."""
        box = QComboBox(self.table)
        box.setFrame(False)
        box.addItem(BOARD_DEFAULT, "")
        for c in conductors():
            box.addItem(c.name, c.name)
        box.setToolTip(f"What {layer} is made of. Choosing a conductor sets the layer's "
                       "resistivity, TCR and thermal conductivity. Not part of KiCad's "
                       "stackup, so the lock does not apply.")
        settings = self._setup.settings if self._setup is not None else None
        chosen = settings.conductor_by_layer.get(layer, "") if settings else ""
        if find(chosen) is None:
            chosen = ""
            if settings is not None and layer in settings.rho_by_layer:
                box.addItem(CUSTOM, CUSTOM)        # a resistivity typed in by hand
                chosen = CUSTOM
        box.setCurrentIndex(box.findData(chosen))
        box.currentIndexChanged.connect(partial(self._conductor_chosen, layer, box))
        box.setEnabled(self._setup is not None)
        return box

    def _show_conductor(self, layer: str, row: Optional[int] = None):
        """A copper layer's thermal conductivity, from its conductor."""
        if row is None:
            row = next(i for i, e in enumerate(self._order)
                       if isinstance(e, CopperLayer) and e.name == layer)
        settings = self._setup.settings if self._setup is not None else None
        k = layer_k_w_mk(settings.conductor_by_layer if settings else {}, layer)
        self.table.item(row, C_K).setText(f"{k:g}")

    def _material_box(self, d: int, kicad_name: str) -> QComboBox:
        """A dielectric's material: the ones KiPI knows, and the name KiCad gives it when
        that is not one of them."""
        box = QComboBox(self.table)
        box.setFrame(False)
        names = [m.material for m in catalog()]
        if kicad_name not in names:
            box.addItem(kicad_name or "(unnamed)", kicad_name)
        for m in catalog():
            box.addItem(m.material, m.material)
        box.currentIndexChanged.connect(partial(self._material_chosen, d, box))
        return box

    def _show_values(self):
        """Show ``self._mm``, ``self._er`` and ``self._mat`` in the unit chosen."""
        unit = self.unit()
        for e, spin, t in zip(self._order, self._thk_spins, self._mm):
            u = row_unit(unit, isinstance(e, CopperLayer))
            f = THICKNESS_UNITS[u]
            spin.blockSignals(True)
            spin.setSuffix(f" {u}")
            spin.setShownDecimals(UNIT_DECIMALS[u])
            spin.setRange(MIN_MM / f, MAX_MM / f)
            spin.setSingleStep(10 ** -UNIT_DECIMALS[u] * (10 if u != "oz" else 500))
            spin.setValue(t / f)
            spin.blockSignals(False)
        for spin, er in zip(self._er_spins, self._er):
            spin.blockSignals(True)
            spin.setValue(er)
            spin.blockSignals(False)
        for box, mat in zip(self._mat_boxes, self._mat):
            box.blockSignals(True)
            i = box.findData(mat)
            if i < 0:                    # a name from an older setup: keep it as a choice
                box.addItem(mat or "(unnamed)", mat)
                i = box.count() - 1
            box.setCurrentIndex(i)
            box.blockSignals(False)
        self._show_k()
        self._show_total()

    def _show_k(self):
        """Each dielectric's in-plane thermal conductivity, from its material."""
        rows = [i for i, e in enumerate(self._order) if not isinstance(e, CopperLayer)]
        for row, mat in zip(rows, self._mat):
            info = lookup(mat)
            item = self.table.item(row, C_K)
            item.setText(f"{info.k_w_mk:g}" + (" ⚠" if info.assumed else ""))
            item.setToolTip(f"Taken as {info.matched}"
                            + (" (name not recognized)" if info.assumed else ""))

    def _show_total(self):
        st, imp = self._current, self._imported
        if st is None:
            return
        text = f"Board {st.board_thickness_mm:.3f} mm"
        if imp is not None and st.edited:
            text += f" (KiCad {imp.board_thickness_mm:.3f} mm)"
        if not st.from_board:
            text += " — default stackup, KiCad gave none"
        self.lbl_total.setText(text)

    def _update_enabled(self):
        unlocked = not self.chk_lock.isChecked() and self._current is not None
        for spin in self._thk_spins + self._er_spins:
            spin.setReadOnly(not unlocked)
            spin.setButtonSymbols(UnitSpinBox.UpDownArrows if unlocked
                                  else UnitSpinBox.NoButtons)
        for box in self._mat_boxes:
            box.setEnabled(unlocked)
        self.btn_reset.setEnabled(unlocked)
        for w in (self.spin_rho, self.spin_tcr):
            w.setEnabled(self._setup is not None)

    def _unit_changed(self, unit: str):
        QSettings("KiPI", "KiPI").setValue("stackup/thickness_unit", unit)
        self._show_values()

    # ------------------------------------------------------------------ edits
    def _lock_toggled(self, locked: bool):
        self._update_enabled()
        if locked:
            self.reset()

    def _thickness_edited(self, i: int, value: float):
        u = row_unit(self.unit(), isinstance(self._order[i], CopperLayer))
        self._mm[i] = value * THICKNESS_UNITS[u]
        self._timer.start()

    def _er_edited(self, d: int, value: float):
        self._er[d] = value
        self._timer.start()

    def _material_chosen(self, d: int, box: QComboBox, _index: int):
        """A material picked from the list: its name, and its typical εr where KiPI knows
        one (the name KiCad gave goes back to KiCad's εr)."""
        mat = box.currentData()
        self._mat[d] = mat
        known = {m.material: m for m in catalog()}
        imp = self._imported
        if mat in known:
            self._er[d] = known[mat].er
        elif imp is not None and dielectric_materials(imp)[d] == mat:
            self._er[d] = dielectric_er(imp)[d]
        self._er_spins[d].blockSignals(True)
        self._er_spins[d].setValue(self._er[d])
        self._er_spins[d].blockSignals(False)
        self._show_k()
        self._timer.start()

    def _emit_edit(self):
        if self._imported is None or self.chk_lock.isChecked():
            return
        imp = self._imported
        same = (all(abs(a - b) < 1e-9 for a, b in zip(self._mm, thicknesses(imp)))
                and all(abs(a - b) < 1e-9 for a, b in zip(self._er, dielectric_er(imp)))
                and self._mat == dielectric_materials(imp))
        self._apply(imp if same else restack(imp, self._mm, self._er, materials=self._mat))

    def _apply(self, st: Stackup):
        cur = self._current
        unchanged = cur is st or (cur is not None and cur.edited == st.edited
                                  and thicknesses(cur) == thicknesses(st)
                                  and dielectric_er(cur) == dielectric_er(st)
                                  and dielectric_materials(cur) == dielectric_materials(st))
        self._current = st
        self._show_total()
        if not unchanged:
            self.stackup_changed.emit(st)

    # ------------------------------------------------------------------ copper settings
    def _copper_changed(self, *_):
        if self._setup is None:
            return
        s = self._setup.settings
        s.rho_ohm_mm = self.spin_rho.value() * UOHM_CM
        s.tcr_per_c = self.spin_tcr.value()
        self.settings_changed.emit()

    def _layer_rho_changed(self, name: str, value: float):
        """A resistivity typed for a layer; 'default' also drops its conductor."""
        if self._setup is None:
            return
        s = self._setup.settings
        if value > 0:
            s.rho_by_layer[name] = value * UOHM_CM
        else:
            s.rho_by_layer.pop(name, None)
            s.tcr_by_layer.pop(name, None)
            s.conductor_by_layer.pop(name, None)
            box = self._cond_boxes.get(name)
            if box is not None:
                box.blockSignals(True)
                box.setCurrentIndex(box.findData(""))
                box.blockSignals(False)
            self._show_conductor(name)
        self.settings_changed.emit()

    def _conductor_chosen(self, layer: str, box: QComboBox, _index: int):
        """A conductor picked for a copper layer: its resistivity, TCR and thermal
        conductivity, or the board-wide settings again."""
        if self._setup is None:
            return
        s = self._setup.settings
        c = find(box.currentData())
        if c is not None:
            s.conductor_by_layer[layer] = c.name
            s.rho_by_layer[layer] = c.rho_ohm_mm
            s.tcr_by_layer[layer] = c.tcr_per_c
        elif box.currentData() == "":
            for d in (s.conductor_by_layer, s.rho_by_layer, s.tcr_by_layer):
                d.pop(layer, None)
        else:                                       # "Custom resistivity": left as it is
            return
        spin = self._rho_spins[list(self._cond_boxes).index(layer)]
        spin.blockSignals(True)
        spin.setValue(s.rho_by_layer.get(layer, 0.0) / UOHM_CM)
        spin.blockSignals(False)
        self._show_conductor(layer)
        self.settings_changed.emit()
