"""The Stackup window: the board's layers with their thicknesses, materials, εr and
resistivity, for every analysis (#61), and beside them a cross-section drawn to scale that
follows the thicknesses as they are typed (#104).

Locked (the default), the thicknesses, materials and εr are the ones read from KiCad, and
the copper layers' conductors and resistivities cannot be changed.
Unlocked, each can be changed to compare stackups (choosing a dielectric's material sets its
typical εr and in-plane thermal conductivity, and both can then be typed over); every
analysis then uses the edited stackup. KiCad's
API cannot change a board's stackup yet, so edits stay in KiPI. A copper layer's conductor
and resistivity are setup settings, changed while unlocked: choosing a conductor sets the
layer's resistivity, TCR and thermal conductivity. Locking keeps them as set. The board's
copper resistivity and TCR, and the dielectric loss tangent and Default εr the AC solve uses
(#134), can always be changed.

The solder masks and silkscreens (#82) are listed above and below the copper. Their
thicknesses can be changed while unlocked, for the electro-thermal solve; they sit outside
the board and do not add to its thickness.
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

from kipi.core.model.board import MASK, Coating, CopperLayer, Stackup
from kipi.core.model.conductors import UOHM_CM, conductors, find, layer_k_w_mk
from kipi.core.model.stackup import (
    coating_thicknesses, dielectric_er, dielectric_k, dielectric_materials, restack,
    stack_order, thicknesses,
)
from kipi.core.viz.layer_colors import coating_rgb
from kipi.dc.thermal.materials import catalog, coating_k, lookup
from kipi.gui.stackup_view import Band, StackupView
from kipi.gui.tooltip_art import art_tip
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


def full_order(stackup: Stackup) -> list:
    """Every layer the window lists, top to bottom: the top coatings, the board's copper
    and dielectrics, the bottom coatings."""
    coat = stackup.coatings
    return ([c for c in coat if c.side == "top"] + stack_order(stackup)
            + [c for c in coat if c.side != "top"])


def full_thicknesses(stackup: Stackup) -> List[float]:
    """Each layer's thickness in mm, in :func:`full_order`."""
    return [e.thickness_mm for e in full_order(stackup)]


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
    settings_changed = Signal()          # resistivity, TCR, loss tangent or Default εr

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
        self._k: List[float] = []                    # dielectrics' k set here (0: material's)
        self._mat_boxes: List[QComboBox] = []
        self._cond_boxes: Dict[str, QComboBox] = {}  # copper layer name -> conductor
        self._thk_spins: List[UnitSpinBox] = []
        self._er_spins: List[UnitSpinBox] = []
        self._k_spins: List[UnitSpinBox] = []
        self._rho_spins: List[UnitSpinBox] = []
        self._timer = QTimer(self)                   # one update after a burst of edits
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._emit_edit)

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.chk_lock = QCheckBox("Lock to imported values", self)
        self.chk_lock.setChecked(True)
        self.chk_lock.setToolTip("Use the thicknesses, materials, εr and the materials' "
                                 "thermal conductivity read from KiCad, and keep each copper "
                                 "layer's conductor and resistivity as they are. "
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
        self.view = StackupView(self)
        self.view.layer_clicked.connect(self._focus_row)

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
                      "table, overrides them; those are changed while unlocked. These two "
                      "are not locked.", g_cu)
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        f.addRow(note)

        g_diel = QGroupBox("Dielectric", self)
        f = QFormLayout(g_diel)
        self.spin_tand = _spin(0.0, 0.2, 0.02, 4, "", 0.001)
        self.spin_tand.setToolTip("Dielectric loss tangent of every dielectric, for the AC "
                                  "solve (FR4 is about 0.02). It damps the plane resonances.")
        self.spin_er = _spin(1.0, 20.0, 4.3, 2, "", 0.1)
        self.spin_er.setToolTip("Dielectric constant, for the AC solve, of every dielectric "
                                "whose εr is 'default' (KiCad gives none, or it is a metal)")
        f.addRow("Loss tangent", self.spin_tand)
        f.addRow("Default εr", self.spin_er)
        for sp in (self.spin_tand, self.spin_er):
            f.labelForField(sp).setToolTip(sp.toolTip())
            sp.valueChanged.connect(self._dielectric_changed)
        note = QLabel("Used by the AC analysis. Like the copper settings, these are not "
                      "locked.", g_diel)
        note.setWordWrap(True)
        note.setStyleSheet("color: #999;")
        f.addRow(note)
        settings = QHBoxLayout()
        settings.addWidget(g_cu, 1)
        settings.addWidget(g_diel, 1)

        row = QHBoxLayout()
        self.btn_reset = QPushButton("Reset", self)
        self.btn_reset.setToolTip("Go back to the thicknesses, materials, εr and thermal "
                                  "conductivity read from KiCad")
        self.btn_reset.clicked.connect(self.reset)
        btn_close = QPushButton("Close", self)
        btn_close.setToolTip("Close the window; the stackup stays as set here")
        btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_reset)
        row.addStretch(1)
        row.addWidget(btn_close)

        lay.addLayout(top)
        mid = QHBoxLayout()
        mid.addWidget(self.table, 1)
        mid.addWidget(self.view)
        lay.addLayout(mid, 1)
        lay.addWidget(self.lbl_total)
        lay.addLayout(settings)
        lay.addLayout(row)
        self.resize(1150, 600)
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
                          (self.spin_tcr, setup.settings.tcr_per_c),
                          (self.spin_tand, setup.ac.loss_tangent),
                          (self.spin_er, setup.ac.default_er)):
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
        """Back to the imported thicknesses, materials, εr and k (the lock is left as it is)."""
        if self._imported is None:
            return
        self._mm = full_thicknesses(self._imported)
        self._er = dielectric_er(self._imported)
        self._mat = dielectric_materials(self._imported)
        self._k = dielectric_k(self._imported)
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
        self._thk_spins, self._er_spins, self._k_spins, self._rho_spins = [], [], [], []
        self._mat_boxes = []
        self._cond_boxes = {}
        self.table.setRowCount(0)
        if st is None:
            self._order, self._mm, self._er, self._mat, self._k = [], [], [], [], []
            self.lbl_total.setText("Load a board to see its stackup.")
            self.view.set_bands([])
            return
        names = {c.id: c.name for c in st.copper}
        self._order = full_order(st)
        self._mm = full_thicknesses(st)
        self._er = dielectric_er(st)
        self._mat = dielectric_materials(st)
        self._k = dielectric_k(st)
        kicad_mats = dielectric_materials(self._imported) if self._imported else self._mat
        overrides = self._setup.settings.rho_by_layer if self._setup is not None else {}
        self.table.setRowCount(len(self._order))
        d = 0
        for i, e in enumerate(self._order):
            copper = isinstance(e, CopperLayer)
            if isinstance(e, Coating):
                self._fill_coating(i, e)
                continue
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
                er.setToolTip("Dielectric constant ('default' uses the Default εr "
                              "setting below)")
                er.valueChanged.connect(partial(self._er_edited, d))
                self.table.setCellWidget(i, C_ER, er)
                self._er_spins.append(er)
                k = _spin(0.0, 1000.0, 0.0, 3, "", 0.1)
                k.setFrame(False)
                k.valueChanged.connect(partial(self._k_edited, d))
                self.table.setCellWidget(i, C_K, k)
                self._k_spins.append(k)
                self.table.setItem(i, C_RHO, QTableWidgetItem(""))
                d += 1
        self._show_values()

    def _fill_coating(self, i: int, c: Coating):
        """A solder mask's or silkscreen's row: its thickness, editable while unlocked."""
        item = QTableWidgetItem(c.name)
        pm = QPixmap(12, 12)
        pm.fill(QColor.fromRgbF(*coating_rgb(c)))
        item.setIcon(QIcon(pm))
        what = "solder mask" if c.kind == MASK else "silkscreen"
        tip = (f"The {what} over the {c.side} copper. It holds a little heat in the copper "
               "in the electro-thermal solve" + ("" if c.kind == MASK else
                                                 ", where it prints")
               + "; it does not add to the board's thickness.")
        item.setToolTip(art_tip(tip, "coatings", c.kind))
        self.table.setItem(i, C_NAME, item)
        mat = QTableWidgetItem(c.material or ("Solder mask" if c.kind == MASK
                                              else "Silkscreen ink"))
        mat.setToolTip(tip)
        self.table.setItem(i, C_MAT, mat)
        thk = UnitSpinBox(self.table)
        thk.setFrame(False)
        thk.setToolTip(f"Thickness of the {c.side} {what}")
        thk.valueChanged.connect(partial(self._thickness_edited, i))
        self.table.setCellWidget(i, C_THK, thk)
        self._thk_spins.append(thk)
        k = QTableWidgetItem(f"{coating_k(c):g}")
        k.setTextAlignment(Qt.AlignCenter)
        k.setToolTip(f"Thermal conductivity of a typical {what}")
        self.table.setItem(i, C_K, k)
        for col in (C_ER, C_RHO):
            self.table.setItem(i, col, QTableWidgetItem(""))

    def _split(self, mm: List[float]):
        """The thicknesses in the window's order as (the board's, in stack order; the
        coatings', top to bottom)."""
        board = [t for e, t in zip(self._order, mm) if not isinstance(e, Coating)]
        coat = [t for e, t in zip(self._order, mm) if isinstance(e, Coating)]
        return board, coat

    def _conductor_box(self, layer: str) -> QComboBox:
        """A copper layer's conductor: the setup's board-wide copper, or one KiPI knows."""
        box = QComboBox(self.table)
        box.setFrame(False)
        box.addItem(BOARD_DEFAULT, "")
        for c in conductors():
            box.addItem(c.name, c.name)
        box.setToolTip(f"What {layer} is made of. Choosing a conductor sets the layer's "
                       "resistivity, TCR and thermal conductivity. Untick Lock to imported "
                       "values to change it.")
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
        self._show_view()

    def _show_view(self):
        """The cross-section, with each layer's thickness in the unit chosen."""
        unit = self.unit()
        mats = iter(self._mat)
        bands = []
        for e, t in zip(self._order, self._mm):
            copper = isinstance(e, CopperLayer)
            u = row_unit(unit, copper)
            thk = f"{t / THICKNESS_UNITS[u]:.{UNIT_DECIMALS[u]}f} {u}"
            if isinstance(e, Coating):
                bands.append(Band(f"{e.name}  {thk}", t, coating_rgb(e)))
            elif copper:
                bands.append(Band(f"{e.name}  {thk}", t, self._colors.get(e.id), True))
            else:
                mat = next(mats, "")
                bands.append(Band(f"{mat or 'Dielectric'}  {thk}", t, material=mat))
        self.view.set_bands(bands)

    def _focus_row(self, i: int):
        """A layer clicked in the cross-section: its thickness in the table."""
        if 0 <= i < len(self._thk_spins):
            self.table.scrollToItem(self.table.item(i, C_NAME))
            self._thk_spins[i].setFocus()
            self._thk_spins[i].selectAll()

    def _show_k(self):
        """Each dielectric's in-plane thermal conductivity: typed here, or its material's."""
        for spin, mat, k in zip(self._k_spins, self._mat, self._k):
            info = lookup(mat)
            spin.blockSignals(True)
            spin.setValue(k or info.k_w_mk)
            spin.setSuffix(" ⚠" if info.assumed and not k else "")
            spin.blockSignals(False)
            spin.setToolTip("In-plane thermal conductivity, for the electro-thermal solve. "
                            + ("Set here" if k else f"Taken as {info.matched}"
                               + (" (name not recognized)" if info.assumed else ""))
                            + "; choosing a material sets its typical value.")

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
        for spin in self._thk_spins + self._er_spins + self._k_spins:
            spin.setReadOnly(not unlocked)
            spin.setButtonSymbols(UnitSpinBox.UpDownArrows if unlocked
                                  else UnitSpinBox.NoButtons)
        for box in self._mat_boxes:
            box.setEnabled(unlocked)
        # A layer's conductor and resistivity are setup settings, but locked with the rest:
        # locking keeps them as set rather than resetting them.
        has_setup = self._setup is not None
        for box in self._cond_boxes.values():
            box.setEnabled(unlocked and has_setup)
        for spin in self._rho_spins:
            spin.setReadOnly(not (unlocked and has_setup))
            spin.setButtonSymbols(UnitSpinBox.UpDownArrows if unlocked and has_setup
                                  else UnitSpinBox.NoButtons)
        self.btn_reset.setEnabled(unlocked)
        for w in (self.spin_rho, self.spin_tcr, self.spin_tand, self.spin_er):
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
        self._show_view()                # at once; the analyses wait for the timer
        self._timer.start()

    def _er_edited(self, d: int, value: float):
        self._er[d] = value
        self._timer.start()

    def _k_edited(self, d: int, value: float):
        """A conductivity typed for a dielectric; its material's typical value unsets it."""
        self._k[d] = 0.0 if abs(value - lookup(self._mat[d]).k_w_mk) < 1e-9 else value
        self._show_k()
        self._timer.start()

    def _material_chosen(self, d: int, box: QComboBox, _index: int):
        """A material picked from the list: its name, and its typical εr where KiPI knows
        one (the name KiCad gave goes back to KiCad's εr)."""
        mat = box.currentData()
        self._mat[d] = mat
        self._k[d] = 0.0                 # the new material's typical k
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
        self._show_view()
        self._timer.start()

    def _emit_edit(self):
        if self._imported is None or self.chk_lock.isChecked():
            return
        imp = self._imported
        board, coat = self._split(self._mm)
        same = (all(abs(a - b) < 1e-9 for a, b in zip(self._mm, full_thicknesses(imp)))
                and all(abs(a - b) < 1e-9 for a, b in zip(self._er, dielectric_er(imp)))
                and self._mat == dielectric_materials(imp)
                and self._k == dielectric_k(imp))
        self._apply(imp if same else restack(imp, board, self._er, materials=self._mat,
                                             k=self._k, coating_mm=coat))

    def _apply(self, st: Stackup):
        cur = self._current
        unchanged = cur is st or (cur is not None and cur.edited == st.edited
                                  and thicknesses(cur) == thicknesses(st)
                                  and coating_thicknesses(cur) == coating_thicknesses(st)
                                  and dielectric_er(cur) == dielectric_er(st)
                                  and dielectric_materials(cur) == dielectric_materials(st)
                                  and dielectric_k(cur) == dielectric_k(st))
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

    def _dielectric_changed(self, *_):
        if self._setup is None:
            return
        self._setup.ac.loss_tangent = self.spin_tand.value()
        self._setup.ac.default_er = self.spin_er.value()
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
