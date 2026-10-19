# Portions Copyright (c) 2026 Tomachie LLC.
"""The Coupled Noise setup panel (#69), shown in place of the DC panel in Coupled Noise
mode.

It edits the same :class:`~kipi.dc.pi.model.PISetup` as the other panels. An aggressor
names a switching VRM (set up in DC or AC mode) and adds what its waveforms need; the
capacitor models are AC mode's, and the aggressor only says which capacitors are its input
and output capacitors. A solve covers one aggressor.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Dict, List, Optional

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QColor
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QTabWidget, QVBoxLayout, QWidget,
)

from kipi.ac.gui.caps import describe_cap, models_from_values, new_cap
from kipi.ac.gui.panel import CapEditor
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.nets import classify_nets, sorted_nets
from kipi.core.model.parts import is_capacitor_ref, is_inductor_ref, parse_inductance
from kipi.dc.gui.pi_panel import (
    NONE, ItemList, PartSelection, _Editor, _get_optional, _optional_spin, _set_optional,
    _spin, _unique, make_searchable, tip,
)
from kipi.dc.pi.model import PISetup
from kipi.gui.report_option import ReportOption
from kipi.gui.tooltip_art import art_tip
from kipi.gui.sortable import make_sortable, reset_sort, set_row
from kipi.noise.checks import (
    NoiseError, guess_ends, operating_point, seed_phases, smps_vrms, validate_noise,
)
from kipi.noise.model import (
    CUSTOM, DEFAULT_COSS_F, DEFAULT_QRR_C, DEFAULT_RDS_ON_OHM, LOGIC_LEVELS, VICTIM_PRESETS, Aggressor,
    AggressorPhase, Victim,
)

TITLE = "Coupled Noise setup"


def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


# ---------------------------------------------------------------------------
# Aggressors
# ---------------------------------------------------------------------------

class AggressorEditor(_Editor):
    """An aggressor's shared values and, below them, one phase's parts at a time."""

    def __init__(self, panel: "NoisePanel", parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.phase = 0                       # the phase shown below
        self.name = QLineEdit()
        self.vrm = QComboBox()
        self.topology = QComboBox()
        self.lbl_op = QLabel()
        self.lbl_op.setWordWrap(True)
        self.lbl_op.setTextFormat(Qt.RichText)
        self.vin = _optional_spin(0, 1000, 3, " V", 0.1)
        self.vin.setSpecialValueText("from the VRM")
        self.load_a = _optional_spin(0, 10000, 3, " A", 0.5)
        self.load_a.setSpecialValueText("from the DC loads")
        self.fsw = _spin(1, 1e5, 500, 1, " kHz", 50)
        self.rise = _spin(0.01, 1e4, 2, 2, " ns", 0.5)
        self.fall = _spin(0.01, 1e4, 2, 2, " ns", 0.5)
        self.duty = _optional_spin(0.1, 99.9, 2, " %", 0.5)
        self.duty.setSpecialValueText("computed")
        f.addRow("Name", self.name)
        f.addRow("VRM", self.vrm)
        f.addRow("Topology", self.topology)
        f.addRow(self.lbl_op)
        f.addRow("Input voltage", self.vin)
        f.addRow("Output current", self.load_a)
        f.addRow("Switching frequency", self.fsw)
        f.addRow("Rise time", self.rise)
        f.addRow("Fall time", self.fall)
        f.addRow("Duty cycle", self.duty)
        # AC probes to show the ripple at (#157).
        self.probes = QListWidget()
        self.probes.setMinimumHeight(50)
        self.probes.setMaximumHeight(100)
        self.probes.itemChanged.connect(self._store)
        f.addRow("Ripple at AC probes", self.probes)
        for w, text, art in (
                (self.name, "A name for this aggressor", None),
                (self.vrm, "The switching regulator (a VRM of type Switcher, set up in DC or "
                           "AC mode) this aggressor is. Its input rail and set voltage give "
                           "the input and output voltages.", ("converter",)),
                (self.topology, "Buck or boost. Auto takes it from the VRM's voltages: an "
                                "input above the output is a buck, below it a boost.",
                 ("converter", "buck", "boost")),
                (self.vin, "The input voltage. Blank uses the VRM's: the middle of its input "
                           "range if it has one, else the set voltage of the VRM feeding its "
                           "input rail.", ("converter", "vin")),
                (self.load_a, "The total output current, all phases. Blank uses the DC "
                              "setup's loads on the VRM's output rail, plus the input current "
                              "of every regulator it feeds.", ("converter", "iout")),
                (self.fsw, "Switching frequency of each phase. Phases are interleaved "
                           "evenly: N phases are 360°/N apart.",
                 ("sw_period", "period", "phase")),
                (self.rise, "The switch node's rising edge, 0 to 100 %. Faster edges reach "
                            "higher harmonics: the spectrum falls at 40 dB/decade above "
                            "1/(π·rise time).", ("sw_ringing", "edge")),
                (self.fall, "The switch node's falling edge, 0 to 100 %.",
                 ("sw_ringing", "edge")),
                (self.duty, "The high side's (buck) or low side's (boost) on-time, as % of "
                            "the period. Blank computes it from the voltages, raised by the "
                            "inductor DCR and on-resistances entered for a buck, and by the "
                            "VRM's efficiency for a boost.", ("sw_period", "duty")),
                (self.probes, "The AC mode's probes to show this aggressor's ripple at: "
                              "the voltage between each probe's rail and ground pins, "
                              "from the board model (Settings tab), which carries the "
                              "inductor's and the switch's currents through the rails' "
                              "copper and capacitors. A probe must be on the aggressor's "
                              "input or output rail, or a net a series link joins to one. "
                              "Shown in the Ripple at AC probes plot.",
                 ("board_model", "probe"))):
            tip(f, w, text, art)
        self._watch(self.name, self.vrm, self.topology, self.vin, self.load_a, self.fsw,
                    self.rise, self.fall, self.duty)

        # Phases: one shown at a time, chosen above its fields.
        row = QHBoxLayout()
        self.cmb_phase = QComboBox()
        self.cmb_phase.setToolTip("The phase whose parts are shown below")
        self.cmb_phase.currentIndexChanged.connect(self._phase_changed)
        for text, slot, what in (
                ("Add phase", self._add_phase, "Add a phase (a copy of the shown one)"),
                ("Remove phase", self._remove_phase, "Delete the shown phase"),
                ("Fill from VRM", self._fill_from_vrm,
                 "Replace the phases with one per VRM phase, filled from the board: the "
                 "VRM's input (buck) or output (boost) and ground pins, the inductor between "
                 "the switching device and the output (buck) or input (boost) rail, its "
                 "value, the switch node, and the capacitors from each rail to ground "
                 "(each to the nearest phase)")):
            b = QPushButton(text)
            b.setToolTip(what)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.insertWidget(0, self.cmb_phase, 1)
        f.addRow(row)
        g = QGroupBox("Phase")
        pf = self.phase_form = QFormLayout(g)
        self.inductor = QComboBox()
        make_searchable(self.inductor)
        self.inductance = _spin(0, 1e6, 0, 1, " nH", 10)
        self.inductance.setSpecialValueText("required")
        self.dcr = _spin(0, 1e5, 0, 3, " mΩ", 0.1)
        self.switch_net = QComboBox()
        make_searchable(self.switch_net)
        self.high_side = self._picker()
        self.low_side = self._picker()
        self.input_caps = self._cap_list()
        self.output_caps = self._cap_list()
        self.coss = _optional_spin(0, 1e6, 1, " pF", 10)
        self.coss.setSpecialValueText(f"not set ({DEFAULT_COSS_F * 1e12:g} pF)")
        self.qrr = _optional_spin(0, 1e4, 1, " nC", 1)
        self.qrr.setSpecialValueText(f"not set ({DEFAULT_QRR_C * 1e9:g} nC)")
        self.rds_hs = _optional_spin(0, 1e5, 3, " mΩ", 0.5)
        self.rds_hs.setSpecialValueText(f"not set ({DEFAULT_RDS_ON_OHM * 1e3:g} mΩ)")
        self.rds_ls = _optional_spin(0, 1e5, 3, " mΩ", 0.5)
        self.rds_ls.setSpecialValueText(f"not set ({DEFAULT_RDS_ON_OHM * 1e3:g} mΩ)")
        self.package_l = _spin(0, 1e4, 0, 3, " nH", 0.05)
        pf.addRow("Inductor", self.inductor)
        pf.addRow("Inductance", self.inductance)
        pf.addRow("DCR", self.dcr)
        pf.addRow("Switch node", self.switch_net)
        pf.addRow("High-side pins", self.high_side)
        pf.addRow("Low-side pins", self.low_side)
        pf.addRow("Input capacitors", self.input_caps)
        pf.addRow("Output capacitors", self.output_caps)
        pf.addRow("Coss", self.coss)
        pf.addRow("Qrr", self.qrr)
        pf.addRow("High-side Rds(on)", self.rds_hs)
        pf.addRow("Low-side Rds(on)", self.rds_ls)
        pf.addRow("Package inductance", self.package_l)
        for w, text, art in (
                (self.inductor, "The phase's power inductor; picking one reads its value",
                 ("hot_loop", "ind")),
                (self.inductance, "The inductor's inductance at the operating current",
                 ("hot_loop", "ind")),
                (self.dcr, "The inductor's DC resistance, from its datasheet (optional)",
                 ("hot_loop", "ind")),
                (self.switch_net, "The switch node: the net between the switching device "
                                  "and the inductor. Its copper's capacitance to the planes "
                                  "adds to Coss.", ("hot_loop", "sw")),
                (self.high_side, "The switching device's pins on the rail its hot loop is "
                                 "fed from: VIN for a buck, the output (the synchronous "
                                 "rectifier) for a boost. A discrete design's high-side "
                                 "FET may be a different part from its low-side FET.",
                 ("hot_loop", "hs")),
                (self.low_side, "The switching device's power ground pins (the low-side "
                                "FET's source)", ("hot_loop", "ls")),
                (self.input_caps, "Tick the capacitors on the input rail that this phase "
                                  "draws from. In a buck they close its hot loop; the "
                                  "ripple current is shared among them by their impedance, "
                                  "from their models in the Capacitors tab.",
                 ("hot_loop", "cin")),
                (self.output_caps, "Tick the capacitors on the output rail this phase feeds. "
                                   "In a boost they close its hot loop.",
                 ("hot_loop", "cout")),
                (self.coss, "The switching device's capacitance at the switch node (Coss of "
                            "the low-side FET, from its datasheet at the input voltage). With "
                            "the hot loop's inductance it sets the ringing on the switch "
                            "node's edges. Not set: a typical value, and the ringing is "
                            "approximate.", ("sw_ringing", "ring")),
                (self.qrr, "The reverse-recovery charge of the body diode the hard edge "
                           "switches off (a buck's low-side FET, a boost's rectifier), from "
                           "its datasheet. The control switch's turn-on must sweep it out "
                           "before the node can move, and the diode's snap-off rings the "
                           "node higher. 0 for a Schottky or GaN switch, which has none. "
                           f"Not set: {DEFAULT_QRR_C * 1e9:g} nC, a low-voltage MOSFET's "
                           "after a short dead time.", ("sw_edges", "hard")),
                (self.rds_hs, "The high-side switch's on-resistance; it damps the ringing "
                              "and raises a buck's duty cycle", ("sw_ringing", "ring")),
                (self.rds_ls, "The low-side switch's on-resistance; it raises a buck's duty "
                              "cycle", ("hot_loop", "ls")),
                (self.package_l, "Loop inductance inside the switching device's package, "
                                 "from its datasheet (0 if not given); added to the loop "
                                 "simulated on the board", ("hot_loop", "loop"))):
            tip(pf, w, text, art)
        self._watch(self.inductance, self.dcr, self.switch_net, self.coss, self.qrr,
                    self.rds_hs,
                    self.rds_ls, self.package_l)
        self.inductor.currentIndexChanged.connect(self._inductor_changed)
        f.addRow(g)

    def _cap_list(self) -> QListWidget:
        lst = QListWidget()
        lst.setMinimumHeight(70)
        lst.setMaximumHeight(120)
        lst.itemChanged.connect(self._store)
        return lst

    # ------------------------------------------------------------ choices
    def refresh_choices(self):
        """Fill the VRM, inductor and switch-node choices from the setup and board."""
        p: NoisePanel = self.panel
        loading, self._loading = self._loading, True
        try:
            self.vrm.clear()
            for v in smps_vrms(p.setup):
                self.vrm.addItem(f"{v.name}: {v.output.net}", v.name)
            self.topology.clear()
            for text, key in (("Auto", "auto"), ("Buck", "buck"), ("Boost", "boost")):
                self.topology.addItem(text, key)
            self.inductor.clear()
            self.inductor.addItem(NONE, "")
            self.switch_net.clear()
            self.switch_net.addItem(NONE, "")
            snap = p.snapshot
            if snap is not None:
                for fp in sorted(snap.footprints, key=lambda x: _natural(x.reference)):
                    if is_inductor_ref(fp.reference):
                        self.inductor.addItem(f"{fp.reference} {fp.value}", fp.reference)
                for n in sorted_nets(snap):
                    self.switch_net.addItem(n, n)
        finally:
            self._loading = loading

    def _cap_candidates(self, net: str) -> List[tuple]:
        """(ref, value) of the capacitors with a pad on ``net``."""
        snap = self.panel.snapshot
        if snap is None or not net:
            return []
        _, _, pads, _ = snap.items_on_net(net)
        values = {f.reference: f.value for f in snap.footprints}
        refs = sorted({p.footprint for p in pads if is_capacitor_ref(p.footprint)},
                      key=_natural)
        return [(r, values.get(r, "")) for r in refs]

    def _fill_caps(self, lst: QListWidget, net: str, chosen: List[str]):
        lst.blockSignals(True)
        lst.clear()
        cands = self._cap_candidates(net)
        known = {r for r, _ in cands}
        cands += [(r, "not on this rail") for r in chosen if r not in known]
        for ref, value in cands:
            it = QListWidgetItem(f"{ref}  {value}")
            it.setData(Qt.UserRole, ref)
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            it.setCheckState(Qt.Checked if ref in chosen else Qt.Unchecked)
            lst.addItem(it)
        lst.blockSignals(False)

    def _fill_probes(self, chosen: List[str]):
        """The AC setup's probes, those ``chosen`` ticked."""
        lst = self.probes
        lst.blockSignals(True)
        lst.clear()
        names = [p.name for p in self.panel.setup.probes]
        rows = [(n, "") for n in names] + [(n, " (no such probe)") for n in chosen
                                          if n not in names]
        for name, note in rows:
            it = QListWidgetItem(name + note)
            it.setData(Qt.UserRole, name)
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            it.setCheckState(Qt.Checked if name in chosen else Qt.Unchecked)
            lst.addItem(it)
        if not rows:
            it = QListWidgetItem("No AC probes: add them in AC mode")
            it.setFlags(Qt.NoItemFlags)
            lst.addItem(it)
        lst.blockSignals(False)

    @staticmethod
    def _ticked(lst: QListWidget) -> List[str]:
        return [lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                if lst.item(i).checkState() == Qt.Checked]

    def _rails(self, agg: Aggressor):
        vrm = next((v for v in self.panel.setup.vrms if v.name == agg.vrm), None)
        if vrm is None:
            return "", ""
        return (vrm.input.net if vrm.input else ""), vrm.output.net

    # ------------------------------------------------------------ values
    def _load(self, a: Aggressor):
        self.refresh_choices()
        self.name.setText(a.name)
        i = self.vrm.findData(a.vrm)
        if i < 0 and a.vrm:
            self.vrm.addItem(f"{a.vrm} (not a switcher)", a.vrm)
            i = self.vrm.count() - 1
        self.vrm.setCurrentIndex(i)
        self.topology.setCurrentIndex(max(self.topology.findData(a.topology), 0))
        _set_optional(self.vin, a.vin_v)
        _set_optional(self.load_a, a.load_a)
        self.fsw.setValue(a.fsw_hz / 1e3)
        self.rise.setValue(a.rise_s * 1e9)
        self.fall.setValue(a.fall_s * 1e9)
        _set_optional(self.duty, a.duty * 100 if a.duty is not None else None)
        self._fill_probes(a.ripple_probes)
        self.phase = min(self.phase, max(len(a.phases) - 1, 0))
        self._fill_phase_combo()
        self._load_phase()
        self._show_operating_point()

    def _fill_phase_combo(self):
        a = self.obj
        self.cmb_phase.blockSignals(True)
        self.cmb_phase.clear()
        for k, ph in enumerate(a.phases if a else []):
            self.cmb_phase.addItem(f"Phase {k + 1}" + (f" ({ph.inductor})" if ph.inductor
                                                       else ""))
        self.cmb_phase.setCurrentIndex(self.phase if a and a.phases else -1)
        self.cmb_phase.blockSignals(False)

    def _load_phase(self):
        a = self.obj
        ph = a.phases[self.phase] if a is not None and a.phases else None
        loading, self._loading = self._loading, True
        try:
            for w in self._phase_widgets():
                w.setEnabled(ph is not None)
            ph = ph or AggressorPhase()
            i = self.inductor.findData(ph.inductor)
            if i < 0 and ph.inductor:
                self.inductor.addItem(ph.inductor, ph.inductor)
                i = self.inductor.count() - 1
            self.inductor.setCurrentIndex(max(i, 0))
            self.inductance.setValue(ph.inductance_h * 1e9)
            self.dcr.setValue(ph.dcr_ohm * 1e3)
            i = self.switch_net.findData(ph.switch_net)
            if i < 0 and ph.switch_net:
                self.switch_net.addItem(ph.switch_net, ph.switch_net)
                i = self.switch_net.count() - 1
            self.switch_net.setCurrentIndex(max(i, 0))
            self.high_side.set_value(ph.high_side)
            self.low_side.set_value(ph.low_side)
            vin_net, vout_net = self._rails(a) if a is not None else ("", "")
            self._fill_caps(self.input_caps, vin_net, ph.input_caps)
            self._fill_caps(self.output_caps, vout_net, ph.output_caps)
            _set_optional(self.coss, ph.coss_f * 1e12 if ph.coss_f is not None else None)
            _set_optional(self.qrr, ph.qrr_c * 1e9 if ph.qrr_c is not None else None)
            _set_optional(self.rds_hs, ph.rds_on_hs_ohm * 1e3
                          if ph.rds_on_hs_ohm is not None else None)
            _set_optional(self.rds_ls, ph.rds_on_ls_ohm * 1e3
                          if ph.rds_on_ls_ohm is not None else None)
            self.package_l.setValue(ph.package_l_h * 1e9)
        finally:
            self._loading = loading

    def _phase_widgets(self):
        return (self.inductor, self.inductance, self.dcr, self.switch_net, self.high_side,
                self.low_side, self.input_caps, self.output_caps, self.coss, self.qrr,
                self.rds_hs,
                self.rds_ls, self.package_l)

    def _save(self, a: Aggressor):
        was_vrm = a.vrm
        a.name = self.name.text().strip() or a.name
        a.vrm = self.vrm.currentData() or ""
        a.topology = self.topology.currentData() or "auto"
        a.vin_v = _get_optional(self.vin)
        a.load_a = _get_optional(self.load_a)
        a.fsw_hz = self.fsw.value() * 1e3
        a.rise_s = self.rise.value() * 1e-9
        a.fall_s = self.fall.value() * 1e-9
        duty = _get_optional(self.duty)
        a.duty = duty / 100 if duty is not None else None
        a.ripple_probes = self._ticked(self.probes)
        if a.phases and 0 <= self.phase < len(a.phases):
            ph = a.phases[self.phase]
            ph.inductor = self.inductor.currentData() or ""
            ph.inductance_h = self.inductance.value() * 1e-9
            ph.dcr_ohm = self.dcr.value() * 1e-3
            ph.switch_net = self.switch_net.currentData() or ""
            ph.high_side = self.high_side.value()
            ph.low_side = self.low_side.value()
            ph.input_caps = self._ticked(self.input_caps)
            ph.output_caps = self._ticked(self.output_caps)
            coss = _get_optional(self.coss)
            ph.coss_f = coss * 1e-12 if coss is not None else None
            qrr = _get_optional(self.qrr)
            ph.qrr_c = qrr * 1e-9 if qrr is not None else None
            hs, ls = _get_optional(self.rds_hs), _get_optional(self.rds_ls)
            ph.rds_on_hs_ohm = hs * 1e-3 if hs is not None else None
            ph.rds_on_ls_ohm = ls * 1e-3 if ls is not None else None
            ph.package_l_h = self.package_l.value() * 1e-9
        if a.vrm != was_vrm:                 # another rail: other capacitors to tick
            self._load_phase()
        self._fill_phase_combo()
        self._show_operating_point()

    def _show_operating_point(self):
        a, setup = self.obj, self.panel.setup
        if a is None:
            self.lbl_op.clear()
            return
        try:
            op = operating_point(setup, a)
        except NoiseError as e:
            self.lbl_op.setText(f'<span style="color:#ff8080">{html.escape(str(e))}</span>')
            return
        auto = " (auto)" if a.topology == "auto" else ""
        self.lbl_op.setText(
            f'<span style="color:#aaa">{op.topology.capitalize()}{auto}: '
            f"{op.vin:g} V → {op.vout:g} V, {op.iout:.4g} A out, duty {op.duty * 100:.2f} %"
            "</span>")

    # ------------------------------------------------------------ phases
    def _phase_changed(self, i: int):
        if i < 0:
            return
        self.phase = i
        self._load_phase()

    def _add_phase(self):
        a = self.obj
        if a is None:
            return
        import copy
        a.phases.append(copy.deepcopy(a.phases[self.phase]) if a.phases else AggressorPhase())
        self.phase = len(a.phases) - 1
        self._fill_phase_combo()
        self._load_phase()
        self.changed.emit()

    def _remove_phase(self):
        a = self.obj
        if a is None or not a.phases:
            return
        del a.phases[self.phase]
        self.phase = max(min(self.phase, len(a.phases) - 1), 0)
        self._fill_phase_combo()
        self._load_phase()
        self.changed.emit()

    def _fill_from_vrm(self):
        a, p = self.obj, self.panel
        if a is None or p.snapshot is None:
            return
        vrm = next((v for v in p.setup.vrms if v.name == a.vrm), None)
        if vrm is None:
            return
        a.phases = seed_phases(p.snapshot, p.setup, vrm, _topology_or_buck(p.setup, a))
        self.phase = 0
        self._fill_phase_combo()
        self._load_phase()
        self.changed.emit()

    def _inductor_changed(self, _i):
        """Picking an inductor reads its value, when the inductance is still empty."""
        if self._loading or self.obj is None:
            return
        ref = self.inductor.currentData() or ""
        snap = self.panel.snapshot
        if ref and snap is not None and not self.inductance.value():
            value = next((f.value for f in snap.footprints if f.reference == ref), "")
            h = parse_inductance(value)
            if h:
                self.inductance.blockSignals(True)
                self.inductance.setValue(h * 1e9)
                self.inductance.blockSignals(False)
        self._store()


# ---------------------------------------------------------------------------
# Victims
# ---------------------------------------------------------------------------

class VictimEditor(_Editor):
    """A victim net, its driver and receiver, and its noise margin."""

    def __init__(self, panel: "NoisePanel", parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.net = QComboBox()
        make_searchable(self.net)
        self.name = QLineEdit()
        self.preset = QComboBox()
        for key, p in VICTIM_PRESETS.items():
            self.preset.addItem(p.label, key)
        self.preset.addItem("Custom", CUSTOM)
        self.driver = self._picker(optional=True)
        self.receiver = self._picker(optional=True)
        self.driver_ohm = _spin(0.01, 1e9, 33, 2, " Ω", 1)
        self.driver_low_ohm = _optional_spin(0.1, 1e9, 2, " Ω", 1)
        self.driver_low_ohm.setSpecialValueText("as driving high")
        self.receiver_ohm = _spin(1, 1e12, 1e6, 0, " Ω", 1000)
        self.receiver_pf = _spin(0, 1e5, 3, 2, " pF", 0.5)
        self.termination = _optional_spin(1, 1e6, 1, " Ω", 1)
        self.termination.setSpecialValueText("none")
        self.logic = QComboBox()
        for v in LOGIC_LEVELS:
            self.logic.addItem(f"{v:g} V", v)
        self.margin = _optional_spin(0, 100, 1, " mV", 1)
        self.lbl_margin = QLabel()
        self.lbl_margin.setStyleSheet("color: #aaa;")
        self.lbl_margin.setWordWrap(True)
        f.addRow("Net", self.net)
        f.addRow("Name", self.name)
        f.addRow("Type", self.preset)
        f.addRow("Driver pins", self.driver)
        f.addRow("Receiver pins", self.receiver)
        f.addRow("Driver resistance", self.driver_ohm)
        f.addRow("Driving low", self.driver_low_ohm)
        f.addRow("Receiver resistance", self.receiver_ohm)
        f.addRow("Receiver capacitance", self.receiver_pf)
        f.addRow("Termination", self.termination)
        f.addRow("Logic level", self.logic)
        f.addRow("Noise margin", self.margin)
        f.addRow(self.lbl_margin)
        for w, text, art in (
                (self.net, "The signal net the noise couples onto (type to search)",
                 ("victim_terms", "trace")),
                (self.name, "A name for this victim; blank is the net's name", None),
                (self.preset, "What drives and loads the net. Picking a type fills in the "
                              "resistances, capacitance and termination below (all still "
                              "editable) and, for a differential leg or an analog input, the "
                              "noise margin.", ("victim_terms", "drv", "rcv", "term")),
                (self.driver, "The driver's pin on the net: where the trace's source "
                              "impedance is. Blank: one end of the net, guessed.",
                 ("victim_terms", "drv")),
                (self.receiver, "The receiver's pin: where the coupled noise is judged "
                                "against the noise margin. Blank: the other end of the net.",
                 ("victim_terms", "rcv")),
                (self.driver_ohm, "The driver's output impedance while it holds the line "
                                  "high (or a pull-up's resistance for an open-drain line "
                                  "released high). Lower holds the line harder against "
                                  "capacitive coupling.",
                 ("victim_terms", "drv")),
                (self.driver_low_ohm, "The driver's output impedance while it pulls the line "
                                      "low, where it differs from driving high: an open "
                                      "drain's switch on-resistance. The noise is solved for "
                                      "the line held high and held low.",
                 ("victim_terms", "drv")),
                (self.receiver_ohm, "The receiver's input resistance to ground",
                 ("victim_terms", "rcv")),
                (self.receiver_pf, "The receiver's input capacitance", ("victim_terms", "rcv")),
                (self.termination, "A parallel termination at the receiver, if any",
                 ("victim_terms", "term")),
                (self.logic, "The logic family's supply: its JESD8 input and output levels "
                             "give the noise margin, min(VIL − VOL, VOH − VIH)",
                 ("noise_margin", "levels")),
                (self.margin, "The noise the receiver tolerates; blank takes it from the "
                              "logic level (or the type). A victim fails when its peak "
                              "coupled noise exceeds it.", ("noise_margin", "margin"))):
            tip(f, w, text, art)
        self._watch(self.name, self.driver_ohm, self.driver_low_ohm, self.receiver_ohm,
                    self.receiver_pf, self.termination, self.logic, self.margin)
        self.net.currentIndexChanged.connect(self._net_changed)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        for w in (self.driver_ohm, self.driver_low_ohm, self.receiver_ohm, self.receiver_pf,
                  self.termination):
            w.valueChanged.connect(self._custom)

    def refresh_choices(self):
        p = self.panel
        loading, self._loading = self._loading, True
        try:
            self.net.clear()
            snap = p.snapshot
            if snap is not None:
                kinds = classify_nets(snap)
                nets = sorted_nets(snap)
                for n in sorted(nets, key=lambda n: kinds.get(n) != "signal"):
                    self.net.addItem(n, n)
        finally:
            self._loading = loading

    def _load(self, v: Victim):
        self.refresh_choices()
        i = self.net.findData(v.net)
        if i < 0:
            self.net.addItem(v.net, v.net)
            i = self.net.count() - 1
        self.net.setCurrentIndex(i)
        self.name.setText(v.name if v.name != v.net else "")
        self.preset.setCurrentIndex(max(self.preset.findData(v.preset), 0))
        self.driver.set_value(v.driver)
        self.receiver.set_value(v.receiver)
        self.driver_ohm.setValue(v.driver_ohm)
        _set_optional(self.driver_low_ohm, v.driver_low_ohm)
        self.receiver_ohm.setValue(v.receiver_ohm)
        self.receiver_pf.setValue(v.receiver_pf)
        _set_optional(self.termination, v.termination_ohm)
        i = self.logic.findData(v.logic_v)
        if i < 0:
            self.logic.addItem(f"{v.logic_v:g} V", v.logic_v)
            i = self.logic.count() - 1
        self.logic.setCurrentIndex(i)
        _set_optional(self.margin, v.noise_margin_v * 1e3
                      if v.noise_margin_v is not None else None)
        self._show_margin(v)

    def _save(self, v: Victim):
        v.net = self.net.currentData() or v.net
        v.name = self.name.text().strip() or v.net
        v.preset = self.preset.currentData() or CUSTOM
        v.driver = self.driver.value()
        v.receiver = self.receiver.value()
        v.driver_ohm = self.driver_ohm.value()
        v.driver_low_ohm = _get_optional(self.driver_low_ohm)
        v.receiver_ohm = self.receiver_ohm.value()
        v.receiver_pf = self.receiver_pf.value()
        v.termination_ohm = _get_optional(self.termination)
        v.logic_v = self.logic.currentData() or v.logic_v
        m = _get_optional(self.margin)
        v.noise_margin_v = m * 1e-3 if m is not None else None
        self._show_margin(v)

    def _show_margin(self, v: Victim):
        hi, lo = v.states()
        self.lbl_margin.setText(f"Margin used: {v.margin() * 1e3:.4g} mV (high: "
                                f"{hi.level_v:.3g} V against {hi.threshold_v:.3g} V; low: "
                                f"{lo.level_v:.3g} V against {lo.threshold_v:.3g} V)")

    def _net_changed(self, _i):
        """Another net: its pins are guessed afresh."""
        if self._loading or self.obj is None:
            return
        net = self.net.currentData() or ""
        snap = self.panel.snapshot
        if snap is not None and net:
            drv, rcv = guess_ends(snap, net)
            for picker, ref in ((self.driver, drv), (self.receiver, rcv)):
                self._show(picker, ref)
        self._store()

    def _preset_changed(self, _i):
        if self._loading or self.obj is None:
            return
        key = self.preset.currentData() or CUSTOM
        self.obj.apply_preset(key)
        loading, self._loading = self._loading, True
        try:
            self._load(self.obj)
        finally:
            self._loading = loading
        self._store()

    def _custom(self, *_):
        """Values edited by hand no longer match the type."""
        if self._loading or self.obj is None:
            return
        p = VICTIM_PRESETS.get(self.preset.currentData() or "")
        if p is not None and (self.driver_ohm.value(), _get_optional(self.driver_low_ohm),
                              self.receiver_ohm.value(), self.receiver_pf.value(),
                              _get_optional(self.termination)) \
                != (p.driver_ohm, p.driver_low_ohm, p.receiver_ohm, p.receiver_pf,
                    p.termination_ohm):
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(self.preset.findData(CUSTOM))
            self.preset.blockSignals(False)


def _topology_or_buck(setup: PISetup, a: Aggressor) -> str:
    """The aggressor's topology, or a buck where its voltages do not say yet."""
    try:
        return operating_point(setup, a).topology
    except NoiseError:
        return a.topology if a.topology in ("buck", "boost") else "buck"


def describe_aggressor(setup: PISetup):
    def describe(a: Aggressor) -> str:
        try:
            topo = operating_point(setup, a).topology
        except NoiseError:
            topo = a.topology if a.topology != "auto" else "?"
        n = len(a.phases)
        return (f"{a.name}: {a.vrm or 'no VRM'}, {topo}, {n} phase{'s' if n != 1 else ''}, "
                f"{a.fsw_hz / 1e3:g} kHz")
    return describe


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class NoisePanel(QWidget, PartSelection):
    solve_requested = Signal(object, str)          # PISetup, aggressor name
    result_selected = Signal(object)               # NoiseResult shown in the plot; None clears
    victim_selected = Signal(str)                  # the victim picked in the results table
    coupling_changed = Signal()                    # the 3D coupling overlay to draw changed
    setup_changed = Signal()
    results_saved = Signal(str)
    part_selected = Signal(str, str, bool)         # see ACPanel.part_selected

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.snapshot: Optional[BoardSnapshot] = None
        self.setup = PISetup()
        self._nets: List[str] = []
        self.result = None
        self._results: Dict[str, object] = {}     # the last NoiseResult of each aggressor
        self._victim = ""                         # the victim selected in the results
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget(self)
        self.agg_list = ItemList(lambda: self.setup.aggressors, AggressorEditor(self),
                                 self._new_aggressor, self._describe_aggressor,
                                 noun="aggressor")
        self.cap_list = ItemList(lambda: self.setup.caps, CapEditor(self),
                                 lambda: new_cap(self.setup), describe_cap,
                                 noun="capacitor model")
        b_values = QPushButton("Models from values")
        b_values.setToolTip("Make one model per distinct value of the capacitors that have "
                            "none yet (capacitance read from the value; enter ESL and ESR)")
        b_values.clicked.connect(self._models_from_values)
        self.cap_list.layout().insertWidget(0, b_values)
        self.victim_list = ItemList(lambda: self.setup.victims, VictimEditor(self),
                                    self._new_victim, self._describe_victim, noun="victim")
        self.tabs.addTab(self.agg_list, "Aggressors")
        self.tabs.addTab(self.victim_list, "Victims")
        self.tabs.addTab(self.cap_list, "Capacitors")
        self.tabs.addTab(self._settings_tab(), "Settings")
        for i, text in enumerate((
                "Switching regulators as noise sources: their switching, edges and power "
                "parts, per phase",
                "Signal nets the noise couples onto, with their driver, receiver and "
                "noise margin",
                "RLC models for the capacitors, shared with AC mode; they split the "
                "switching currents among the input and output capacitors",
                "How far up in frequency the waveforms go, and how much of them is shown")):
            self.tabs.setTabToolTip(i, text)
        for il in self._lists():
            il.changed.connect(self._on_changed)
        self._watch_part_lists()
        lay.addWidget(self.tabs, 3)

        self.lbl_errors = QLabel(self)
        self.lbl_errors.setWordWrap(True)
        self.lbl_errors.setStyleSheet("color: #ff8080;")
        lay.addWidget(self.lbl_errors)

        row = QHBoxLayout()
        self.cmb_agg = QComboBox(self)
        self.cmb_agg.setToolTip("The aggressor to simulate (one at a time)")
        self.cmb_agg.currentIndexChanged.connect(self._agg_changed)
        self.btn_solve = QPushButton("Solve", self)
        self.btn_solve.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_solve.setToolTip("Save the setup, then build the aggressor's waveforms from "
                                  "its parameters and the board's hot loop. Problems that "
                                  "stop the solve are listed above in red.")
        self.btn_solve.clicked.connect(self._solve)
        lbl = QLabel("Aggressor")
        lbl.setToolTip(self.cmb_agg.toolTip())
        row.addWidget(lbl)
        row.addWidget(self.cmb_agg, 2)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.cmb_victims = QComboBox(self)
        self.cmb_victims.setToolTip("The victims to solve: all of them, one, or none (the "
                                    "aggressor's waveforms only)")
        self.cmb_victims.currentIndexChanged.connect(self._victims_changed)
        self.chk_scan = QCheckBox("Scan nearby nets", self)
        self.chk_scan.setToolTip(art_tip(
            "Also look for victims: estimate the noise on every signal net with a trace near "
            "the aggressor and solve the worst in full, with the scan's default driver and "
            "logic level (see Settings)", "scan_radius", "radius", "top"))
        self.chk_scan.toggled.connect(self._scan_toggled)
        lbl = QLabel("Victims")
        lbl.setToolTip(self.cmb_victims.toolTip())
        row.addWidget(lbl)
        row.addWidget(self.cmb_victims, 2)
        row.addWidget(self.chk_scan)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.report_option = ReportOption("waveforms, victims and scan, after each solve",
                                          self._report_changed, self)
        self.btn_report = QPushButton("Open report", self)
        self.btn_report.setToolTip("Open the shown aggressor's report")
        self.btn_report.setVisible(False)
        self.btn_report.clicked.connect(self._open_report)
        row.addWidget(self.btn_solve, 1)
        row.addWidget(self.report_option)
        row.addWidget(self.btn_report)
        lay.addLayout(row)

        g_res = QGroupBox("Results", self)
        lr = QVBoxLayout(g_res)
        self.tbl_victims = QTableWidget(0, 6, g_res)
        self.tbl_victims.setHorizontalHeaderLabels(
            ["Victim", "Peak (mV)", "Margin (mV)", "Of margin", "Mechanism", ""])
        self.tbl_victims.verticalHeader().setVisible(False)
        self.tbl_victims.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_victims.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_victims.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_victims.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_victims.setToolTip("The peak noise each victim's receiver sees over a "
                                    "switching period, against its noise margin. Select a "
                                    "victim to plot its noise.")
        self.tbl_victims.itemSelectionChanged.connect(self._victim_row)
        make_sortable(self.tbl_victims)
        self.tbl_victims.setVisible(False)
        lr.addWidget(self.tbl_victims)
        from kipi.noise.viz import VIEWS as COUPLING_VIEWS
        self.coupling_row = QWidget(g_res)
        row = QHBoxLayout(self.coupling_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.cmb_coupling = QComboBox(self.coupling_row)
        for key, text in COUPLING_VIEWS:
            self.cmb_coupling.addItem(text, key)
        tip_3d = ("Show in the 3D view where the selected victim's peak noise couples in: "
                  "each piece of its traces colored by its part of the receiver's peak (red "
                  "adds to it, blue takes from it), by one mechanism or all of them. The "
                  "common-ground part is a dot at the driver; the capacitor loops that "
                  "induce the inductive part are drawn in magenta. Tick the switch node's net "
                  "in the Board panel to see the capacitive source.")
        tip_3d = art_tip(tip_3d, "coupling", "cap", "ind", "gnd")
        self.cmb_coupling.setToolTip(tip_3d)
        self.cmb_coupling.currentIndexChanged.connect(self._coupling_view_changed)
        lbl = QLabel("Coupling in 3D", self.coupling_row)
        lbl.setToolTip(tip_3d)
        row.addWidget(lbl)
        row.addWidget(self.cmb_coupling, 1)
        self.lbl_coupling = QLabel(g_res)
        self.lbl_coupling.setWordWrap(True)
        self.lbl_coupling.setStyleSheet("color: #aaa;")
        self.coupling_row.setVisible(False)
        self.lbl_coupling.setVisible(False)
        lr.addWidget(self.coupling_row)
        lr.addWidget(self.lbl_coupling)
        self.txt_notes = QLabel(g_res)
        self.txt_notes.setWordWrap(True)
        self.txt_notes.setTextFormat(Qt.RichText)
        self.txt_notes.setStyleSheet("color: #bbb;")
        self.txt_notes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.txt_notes.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        row = QHBoxLayout()
        self.btn_save = QPushButton("Save results…", g_res)
        self.btn_save.setEnabled(False)
        self.btn_save.setToolTip("Save every solved aggressor's results to reopen later with "
                                 "Open results… (in Coupled Noise mode), without solving "
                                 "again")
        self.btn_save.clicked.connect(self._save_results)
        row.addStretch(1)
        row.addWidget(self.btn_save)
        lr.addWidget(self.txt_notes)
        lr.addLayout(row)
        lay.addWidget(g_res, 1)
        self.setEnabled(False)

    def _lists(self):
        return (self.agg_list, self.victim_list, self.cap_list)

    def _part_lists(self):
        return {"victim": self.victim_list, "cap": self.cap_list}

    def _settings_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.setup.noise
        self.spin_fmax = _optional_spin(1, 3000, 1, " MHz", 10)
        self.spin_fmax.setSpecialValueText("from the edges")
        self.spin_harm = QSpinBox()
        self.spin_harm.setRange(10, 100000)
        self.spin_harm.setValue(s.max_harmonics)
        self.spin_periods = QSpinBox()
        self.spin_periods.setRange(1, 20)
        self.spin_periods.setValue(s.periods_shown)
        self.spin_zeta = _spin(0, 1, s.min_ring_zeta, 3, "", 0.01)
        self.spin_soft_zeta = _spin(0, 5, s.soft_edge_zeta, 2, "", 0.05)
        self.spin_dead = _spin(0, 1000, s.dead_time_ns, 1, " ns", 5)
        self.spin_segment = _spin(0.05, 10, s.segment_mm, 2, " mm", 0.1)
        self.spin_window = _spin(0.2, 50, s.window_mm, 1, " mm", 0.5)
        self.chk_board = QCheckBox("Board model")
        self.chk_board.setChecked(s.board_model)
        self.spin_fpd = QSpinBox()
        self.spin_fpd.setRange(2, 100)
        self.spin_fpd.setValue(s.field_per_decade)
        f.addRow("Highest frequency", self.spin_fmax)
        f.addRow("Harmonic limit", self.spin_harm)
        f.addRow("Least ringing damping", self.spin_zeta)
        f.addRow("Soft edge damping", self.spin_soft_zeta)
        f.addRow("Dead time", self.spin_dead)
        f.addRow("Periods shown", self.spin_periods)
        f.addRow("Victim segment", self.spin_segment)
        f.addRow("Coupling window", self.spin_window)
        f.addRow(self.chk_board)
        f.addRow("Field solves per decade", self.spin_fpd)
        self.spin_radius = _spin(0.5, 500, s.scan_radius_mm, 1, " mm", 1)
        self.spin_top = QSpinBox()
        self.spin_top.setRange(1, 1000)
        self.spin_top.setValue(s.scan_top_n)
        self.cmb_scan_type = QComboBox()
        for key, p in VICTIM_PRESETS.items():
            self.cmb_scan_type.addItem(p.label, key)
        self.cmb_scan_type.addItem("Custom driver", CUSTOM)
        self.cmb_scan_type.setCurrentIndex(max(self.cmb_scan_type.findData(s.scan_preset), 0))
        self.spin_scan_drv = _spin(0.01, 1e9, s.scan_driver_ohm, 2, " Ω", 1)
        self.spin_scan_drv.setEnabled(s.scan_preset == CUSTOM)
        self.cmb_scan_logic = QComboBox()
        for v in LOGIC_LEVELS:
            self.cmb_scan_logic.addItem(f"{v:g} V", v)
        self.cmb_scan_logic.setCurrentIndex(max(self.cmb_scan_logic.findData(s.scan_logic_v), 0))
        f.addRow("Scan radius", self.spin_radius)
        f.addRow("Scan: solve in full", self.spin_top)
        f.addRow("Scan: victim type", self.cmb_scan_type)
        f.addRow("Scan: driver resistance", self.spin_scan_drv)
        f.addRow("Scan: logic level", self.cmb_scan_logic)
        tip(f, self.spin_radius, "The scan looks at signal nets with a trace this close to the "
                                 "aggressor's parts", ("scan_radius", "radius"))
        tip(f, self.spin_top, "How many of the scanned nets with the highest estimates are "
                              "solved in full (every mechanism)", ("scan_radius", "top"))
        tip(f, self.cmb_scan_type, "The type a scanned net is solved as: its real driver "
                                   "is not known. High impedance (undriven), the default, is "
                                   "the worst case for capacitive coupling: nothing holds "
                                   "the line, so the coupled charge divides between the "
                                   "coupling and the net's own capacitance. (Inductive noise "
                                   "reaches a receiver whole only through a low-impedance "
                                   "driver.) A net that is a victim already keeps its own "
                                   "type.", ("victim_terms", "drv", "rcv"))
        tip(f, self.spin_scan_drv, "The driver resistance a scanned net is solved with, for "
                                   "a custom driver", ("victim_terms", "drv"))
        self.cmb_scan_type.currentIndexChanged.connect(self._settings_changed)
        tip(f, self.cmb_scan_logic, "The logic level a scanned net is judged against",
            ("noise_margin", "levels", "margin"))
        for wdg in (self.spin_radius, self.spin_top, self.spin_scan_drv):
            wdg.valueChanged.connect(self._settings_changed)
        self.cmb_scan_logic.currentIndexChanged.connect(self._settings_changed)
        tip(f, self.spin_fmax, "The highest harmonic frequency the waveforms keep. From the "
                               "edges: ten times the faster edge's corner frequency "
                               "(1/(π·rise time)) or three times the ringing frequency, "
                               "whichever is higher, at most 3 GHz.", ("spectrum", "fmax"))
        tip(f, self.spin_harm, "The most harmonics of the switching frequency kept; a slow "
                               "switcher with fast edges needs many", ("spectrum", "harm"))
        tip(f, self.spin_zeta, "The least damping ratio ζ of the switch node's ringing. The "
                               "hot loop's resistance (on-resistance, capacitor ESR, copper) "
                               "alone damps it far less than a real switch node: the "
                               "switches' output capacitance, reverse recovery and radiation "
                               "take energy too, and measured ringing usually dies within "
                               "five to ten cycles (ζ about 0.05 to 0.1). 0 uses the loop's "
                               "resistance alone.", ("sw_ringing", "ring"))
        tip(f, self.spin_soft_zeta, "The least damping ratio ζ of the soft edge: the one "
                                    "the inductor current carries across into the other "
                                    "switch's conducting body diode (a buck's falling "
                                    "edge, a boost's rising one). The diode's clamp takes "
                                    "the ringing's energy, so it overshoots once and "
                                    "settles: ζ about 0.5. The hard edge, where the "
                                    "control switch turns on against a conducting diode, "
                                    "keeps the least ringing damping above. Set it to "
                                    "that to ring both edges alike.", ("sw_edges", "soft"))
        tip(f, self.spin_dead, "The controller's dead time: how long the inductor current "
                               "has to carry a soft edge across, C_sw·V/I, before the next "
                               "switch turns on. At light load, or a valley current just "
                               "below zero, it cannot: the switch turns on part way and "
                               "finishes the edge hard, at the entered edge time and the "
                               "least ringing damping. 0 lets the current take as long as "
                               "it needs.", ("sw_edges", "soft"))
        tip(f, self.spin_periods, "Switching periods shown in the waveform plot",
            ("sw_period", "period"))
        tip(f, self.spin_segment, "Victim traces are cut into pieces this long; each is a "
                                  "short transmission line with its own cross-section. "
                                  "Shorter follows the copper beside it more closely.",
            ("xsection", "seg"))
        tip(f, self.spin_window, "How far either side of a victim trace copper is taken "
                                 "into its cross-section: switch-node copper further away "
                                 "does not couple, and the planes within it are its "
                                 "references", ("xsection", "win"))
        tip(f, self.chk_board, "Solve the converter's rails and ground with AC mode's field "
                               "model: the switching currents divide among all the modelled "
                               "capacitors on the rails through the copper's own impedance, "
                               "the rails' ripple is taken at the device's pins, and the "
                               "ground noise between each victim's driver and receiver is "
                               "its shared-ground (common-impedance) coupling. Untick for a "
                               "quick solve: the capacitors then share the current by their "
                               "own impedance and there is no shared-ground coupling.",
            ("board_model", "split", "gnd"))
        tip(f, self.spin_fpd, "Copper (field) solves per decade of the harmonics; the board's "
                              "response in between is interpolated, as in AC mode's sweep",
            ("interp", "solve"))
        self.chk_board.toggled.connect(self._settings_changed)
        self.chk_board.toggled.connect(self.spin_fpd.setEnabled)
        for wdg in (self.spin_fmax, self.spin_harm, self.spin_periods, self.spin_zeta,
                    self.spin_soft_zeta, self.spin_dead, self.spin_segment, self.spin_window,
                    self.spin_fpd):
            wdg.valueChanged.connect(self._settings_changed)
        return w

    def _settings_changed(self, *_):
        s = self.setup.noise
        fmax = _get_optional(self.spin_fmax)
        s.f_max_hz = fmax * 1e6 if fmax is not None else None
        s.max_harmonics = self.spin_harm.value()
        s.periods_shown = self.spin_periods.value()
        s.min_ring_zeta = self.spin_zeta.value()
        s.soft_edge_zeta = self.spin_soft_zeta.value()
        s.dead_time_ns = self.spin_dead.value()
        s.segment_mm = self.spin_segment.value()
        s.window_mm = self.spin_window.value()
        s.board_model = self.chk_board.isChecked()
        s.field_per_decade = self.spin_fpd.value()
        s.scan_radius_mm = self.spin_radius.value()
        s.scan_top_n = self.spin_top.value()
        s.scan_preset = self.cmb_scan_type.currentData() or s.scan_preset
        self.spin_scan_drv.setEnabled(s.scan_preset == CUSTOM)
        s.scan_driver_ohm = self.spin_scan_drv.value()
        s.scan_logic_v = self.cmb_scan_logic.currentData() or s.scan_logic_v
        self._on_changed()

    def _load_settings(self):
        s = self.setup.noise
        for wdg, v in ((self.spin_harm, s.max_harmonics), (self.spin_periods, s.periods_shown),
                       (self.spin_zeta, s.min_ring_zeta),
                       (self.spin_soft_zeta, s.soft_edge_zeta), (self.spin_dead, s.dead_time_ns),
                       (self.spin_segment, s.segment_mm),
                       (self.spin_window, s.window_mm), (self.spin_fpd, s.field_per_decade),
                       (self.spin_radius, s.scan_radius_mm), (self.spin_top, s.scan_top_n),
                       (self.spin_scan_drv, s.scan_driver_ohm)):
            wdg.blockSignals(True)
            wdg.setValue(v)
            wdg.blockSignals(False)
        self.spin_fmax.blockSignals(True)
        _set_optional(self.spin_fmax, s.f_max_hz / 1e6 if s.f_max_hz else None)
        self.spin_fmax.blockSignals(False)
        self.chk_board.blockSignals(True)
        self.chk_board.setChecked(s.board_model)
        self.chk_board.blockSignals(False)
        self.spin_fpd.setEnabled(s.board_model)
        for w, on in ((self.chk_scan, s.scan),):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        self.cmb_scan_logic.blockSignals(True)
        self.cmb_scan_logic.setCurrentIndex(max(self.cmb_scan_logic.findData(s.scan_logic_v), 0))
        self.cmb_scan_logic.blockSignals(False)
        self.cmb_scan_type.blockSignals(True)
        self.cmb_scan_type.setCurrentIndex(max(self.cmb_scan_type.findData(s.scan_preset), 0))
        self.cmb_scan_type.blockSignals(False)
        self.spin_scan_drv.setEnabled(s.scan_preset == CUSTOM)
        self.report_option.set_value(s.html_report, s.report_format)

    # ------------------------------------------------------------------ data
    def nets(self) -> List[str]:
        return list(self._nets)

    def set_board(self, snap: BoardSnapshot, setup: PISetup):
        self.snapshot = snap
        self.setup = setup
        self._load_settings()
        self.clear_results()
        self.setEnabled(True)
        self.reload()

    def set_nets(self, nets: List[str]):
        """Nets offered in the pin pickers (the ones ticked in the Board panel)."""
        if self.snapshot is None:
            return
        order = {n: i for i, n in enumerate(sorted_nets(self.snapshot))}
        nets = sorted(set(nets), key=lambda n: order.get(n, len(order)))
        if nets == self._nets:
            return
        self._nets = nets
        self.reload()

    def reload(self):
        """Refill everything from the setup (the other panels may have edited the VRMs
        and capacitor models)."""
        for il in self._lists():
            il.editor.refresh_nets(self._nets)
        for il in self._lists():
            il.reload()
        self._fill_aggressors()
        self._on_changed()

    def _fill_aggressors(self):
        want = self.setup.noise.aggressor or self.cmb_agg.currentData()
        self.cmb_agg.blockSignals(True)
        self.cmb_agg.clear()
        for a in self.setup.aggressors:
            self.cmb_agg.addItem(a.name, a.name)
        i = self.cmb_agg.findData(want)
        self.cmb_agg.setCurrentIndex(i if i >= 0 else (0 if self.setup.aggressors else -1))
        self.cmb_agg.blockSignals(False)
        self.setup.noise.aggressor = self.cmb_agg.currentData() or ""
        self._fill_victim_choice()
        names = {a.name for a in self.setup.aggressors}
        for gone in set(self._results) - names:
            del self._results[gone]
        self.btn_save.setEnabled(bool(self._results))
        self._show_aggressor_result()

    def _fill_victim_choice(self):
        want = self.setup.noise.victims
        self.cmb_victims.blockSignals(True)
        self.cmb_victims.clear()
        self.cmb_victims.addItem("All victims", "all")
        for v in self.setup.victims:
            self.cmb_victims.addItem(v.name, v.name)
        self.cmb_victims.addItem("None (waveforms only)", "none")
        i = self.cmb_victims.findData(want)
        self.cmb_victims.setCurrentIndex(i if i >= 0 else 0)
        self.cmb_victims.blockSignals(False)
        self.setup.noise.victims = self.cmb_victims.currentData() or "all"

    def _victims_changed(self, _i):
        self.setup.noise.victims = self.cmb_victims.currentData() or "all"
        self.setup_changed.emit()

    def _scan_toggled(self, on: bool):
        self.setup.noise.scan = on
        self.setup_changed.emit()

    def _report_changed(self, on: bool, fmt: str):
        self.setup.noise.html_report, self.setup.noise.report_format = on, fmt

    def _open_report(self):
        path = getattr(self.result, "report_path", "")
        if path:
            from kipi.gui.browser import open_in_browser
            open_in_browser(path)

    def _aggressor(self) -> Optional[Aggressor]:
        name = self.cmb_agg.currentData()
        return next((a for a in self.setup.aggressors if a.name == name), None)

    def _agg_changed(self, _i):
        self.setup.noise.aggressor = self.cmb_agg.currentData() or ""
        self._on_changed()
        self._show_aggressor_result()

    def _on_changed(self):
        self.setup_changed.emit()
        if self.cmb_agg.count() != len(self.setup.aggressors) or any(
                self.cmb_agg.itemData(i) != a.name
                for i, a in enumerate(self.setup.aggressors)):
            self._fill_aggressors()             # an aggressor was added, removed or renamed
        elif self.cmb_victims.count() != len(self.setup.victims) + 2 or any(
                self.cmb_victims.itemData(i + 1) != v.name
                for i, v in enumerate(self.setup.victims)):
            self._fill_victim_choice()          # a victim was added, removed or renamed
        agg = self._aggressor()
        if agg is None:
            errors = (["Add a VRM of type Switcher (in DC or AC mode), then an aggressor "
                       "for it"] if not smps_vrms(self.setup) else ["Add an aggressor"])
        else:
            errors = validate_noise(self.setup, self.snapshot, agg)
        self.lbl_errors.setText("\n".join(f"• {e}" for e in errors))
        self.btn_solve.setEnabled(not errors)

    def _solve(self):
        self.solve_requested.emit(self.setup, self.cmb_agg.currentData() or "")

    def set_busy(self, busy: bool, queued: bool = False):
        self.btn_solve.setEnabled(not busy and not self.lbl_errors.text())
        self.btn_solve.setText(("Queued…" if queued else "Solving…") if busy else "Solve")

    # ------------------------------------------------------------------ new items
    def _new_aggressor(self) -> Optional[Aggressor]:
        vrms = smps_vrms(self.setup)
        if not vrms:
            self.lbl_errors.setText("• Add a VRM of type Switcher (in DC or AC mode) first")
            return None
        taken = {a.vrm for a in self.setup.aggressors}
        vrm = next((v for v in vrms if v.name not in taken), vrms[0])
        a = Aggressor(_unique(vrm.name, [x.name for x in self.setup.aggressors]), vrm.name)
        if self.snapshot is not None:
            a.phases = seed_phases(self.snapshot, self.setup, vrm,
                                   _topology_or_buck(self.setup, a))
        return a

    def _describe_aggressor(self, a: Aggressor) -> str:
        return describe_aggressor(self.setup)(a)

    def _new_victim(self) -> Optional[Victim]:
        if self.snapshot is None:
            return None
        taken = {v.net for v in self.setup.victims}
        kinds = classify_nets(self.snapshot)
        sw = {ph.switch_net for a in self.setup.aggressors for ph in a.phases}
        net = next((n for n in sorted_nets(self.snapshot)
                    if kinds.get(n) == "signal" and n not in taken and n not in sw), None)
        if net is None:
            self.lbl_errors.setText("• No signal net is left to add")
            return None
        drv, rcv = guess_ends(self.snapshot, net)
        return Victim(net, driver=drv, receiver=rcv)

    @staticmethod
    def _describe_victim(v: Victim) -> str:
        label = VICTIM_PRESETS[v.preset].label if v.preset in VICTIM_PRESETS else "Custom"
        name = f"{v.name} ({v.net})" if v.name != v.net else v.name
        return f"{name}: {label}, {v.margin() * 1e3:.4g} mV margin"

    def _models_from_values(self):
        if self.snapshot is None:
            return
        if models_from_values(self, self.snapshot, self.setup, self._nets):
            self.cap_list.reload()
            self._on_changed()

    # ------------------------------------------------------------------ results
    def clear_results(self):
        """Forget every aggressor's results (a new board)."""
        self._results.clear()
        self.btn_save.setEnabled(False)
        self._clear_notes()

    def _clear_notes(self):
        self.result = None
        self.txt_notes.clear()
        reset_sort(self.tbl_victims)
        self.tbl_victims.setRowCount(0)
        self.coupling_row.setVisible(False)
        self._set_victim("")

    def show_error(self, message: str):
        self.txt_notes.setText(f'<span style="color:#ff7070"><b>Cannot solve:</b><br>'
                               f'{html.escape(message).replace(chr(10), "<br>")}</span>')

    def _show_aggressor_result(self):
        result = self._results.get(self.cmb_agg.currentData() or "")
        if result is not None:
            self._display(result)
        elif self.result is not None:
            self._clear_notes()
            self.result_selected.emit(None)

    def show_result(self, result):
        """A finished solve (or its waveforms, as they are ready): kept for its aggressor and
        shown if that aggressor is still selected."""
        self._results[result.aggressor] = result
        self.btn_save.setEnabled(True)
        if result.aggressor == (self.cmb_agg.currentData() or ""):
            self._display(result)

    def load_results(self, results: Dict[str, object]) -> List[str]:
        """Results opened from a file, in place of the ones held. Aggressors not in the setup
        are left out; their names are returned."""
        names = {a.name for a in self.setup.aggressors}
        self._results = {k: r for k, r in results.items() if k in names}
        self.btn_save.setEnabled(bool(self._results))
        cur = self.cmb_agg.currentData() or ""
        if cur not in self._results and self._results:
            first = next(a.name for a in self.setup.aggressors if a.name in self._results)
            self.cmb_agg.setCurrentIndex(self.cmb_agg.findData(first))   # shows it
        else:
            self._show_aggressor_result()
        return sorted(set(results) - names)

    def _display(self, result):
        self.result = result
        lines = [html.escape(x) for x in result.summary_lines()
                 if not x.startswith("Victim ")]
        lines += [f"⚠ {html.escape(w)}" for w in result.warnings]
        lines += [f"⚠ {html.escape(v.name)}: {html.escape(w)}"
                  for v in getattr(result, "victims", []) for w in v.warnings]
        self.txt_notes.setText("<br>".join(lines))
        self._fill_victims(result)
        self.btn_report.setVisible(bool(getattr(result, "report_path", "")))
        self.result_selected.emit(result)

    def _fill_victims(self, result):
        t = self.tbl_victims
        victims = getattr(result, "victims", [])
        t.blockSignals(True)
        reset_sort(t)
        t.setRowCount(len(victims))
        for r, v in enumerate(victims):
            set_row(t, r, [v.name + (" (scan)" if getattr(v, "scanned", False) else ""),
                           (f"{v.peak_v * 1e3:.4g}", v.peak_v),
                           (f"{v.margin_v * 1e3:.4g}", v.margin_v),
                           (f"{v.ratio * 100:.3g} %", v.ratio), v.dominant,
                           "PASS" if v.passed else "FAIL"])
            t.item(r, 5).setForeground(QColor("#60d060" if v.passed else "#ff6060"))
        t.blockSignals(False)
        t.setVisible(bool(victims))
        self.coupling_row.setVisible(bool(victims))
        if victims:
            worst = max(range(len(victims)), key=lambda i: victims[i].ratio)
            t.selectRow(worst)
        else:
            self._set_victim("")

    def _victim_row(self):
        rows = self.tbl_victims.selectionModel().selectedRows()
        if rows:
            item = self.tbl_victims.item(rows[0].row(), 0)
            name = item.text().removesuffix(" (scan)")
            self._set_victim(name)
            self.victim_selected.emit(name)

    def _set_victim(self, name: str):
        self._victim = name
        self._coupling_view_changed()

    def _coupling_view_changed(self, *_):
        ov = self.coupling_overlay()
        self.lbl_coupling.setText(ov.note if ov is not None else "")
        self.lbl_coupling.setVisible(ov is not None)
        self.coupling_changed.emit()

    def coupling_overlay(self):
        """The selected victim's coupling for the 3D view (#152), or None."""
        from kipi.noise.viz import coupling_overlay
        return coupling_overlay(self.result, self._victim,
                                self.cmb_coupling.currentData() or "off")

    def _save_results(self):
        if not self._results or self.snapshot is None:
            return
        from kipi.core.naming import stamped
        from kipi.noise.report import RESULTS_SUFFIX, save_results
        start = self.snapshot.project_dir
        stem = f"{stamped(Path(self.snapshot.board_name).stem or 'kipi')}{RESULTS_SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(self, "Save Coupled Noise results",
                                              f"{start}/{stem}" if start else stem,
                                              f"KiPI Coupled Noise results (*{RESULTS_SUFFIX})")
        if not path:
            return
        try:
            save_results(path, self._results, self.snapshot.board_name, self.setup)
        except OSError as e:
            QMessageBox.warning(self, "KiPI", f"Saving failed: {e}")
            return
        self.results_saved.emit(path)

