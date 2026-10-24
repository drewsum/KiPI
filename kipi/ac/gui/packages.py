"""The Packages tab of the AC setup (#53): package PI models from Touchstone files, each on
a board part, and what each of its ports connects to."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from kipi.ac.model import DIE, OPEN, PCB, SHORT, PackageModel, PackagePort
from kipi.ac.package import guess_ports, part_nets, rename_package
from kipi.core.model.touchstone import TouchstoneError, ports_from_name, read_text
from kipi.dc.gui.pi_panel import REQUIRED, _Editor, _unique, make_searchable, tip
from kipi.gui.tooltip_art import art_tip

ROLE_TEXT = {PCB: "PCB pins", DIE: "Die probe", OPEN: "Open", SHORT: "Shorted"}
FILTER = "Touchstone (*.s*p *.ts *.S*P);;All files (*)"
COLS = ("Port", "Name", "Connects to", "Net", "Pins", "Ground net", "Ground pins")


def summary(k: PackageModel) -> str:
    """The file, its ports and its frequency range, or why it cannot be read."""
    try:
        net = k.network()
    except TouchstoneError as e:
        return f"{k.file or 'model'}: cannot be read ({e})"
    f = net.freqs_hz
    return (f"{k.file or 'model'}: {net.n_ports} ports, {len(f)} frequencies, "
            f"{_hz(f[0])} to {_hz(f[-1])}")


def _hz(f: float) -> str:
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


def describe(k: PackageModel) -> str:
    n = {r: sum(p.role == r for p in k.ports) for r in (PCB, DIE)}
    return (f"{k.name}: {k.ref or '?'} · {len(k.ports)} ports "
            f"({n[PCB]} to the PCB, {n[DIE]} on the die)")


def read_model(parent: QWidget, path: str) -> Optional[tuple]:
    """(text, port count from the name, names) of the Touchstone file at ``path``; None,
    having said why, if it cannot be read."""
    try:
        text = read_text(path)
        n = ports_from_name(Path(path).name)
        net = PackageModel("", touchstone=text, n_ports=n).network()
    except (OSError, TouchstoneError) as e:
        QMessageBox.warning(parent, "Package model", f"Cannot read {Path(path).name}:\n{e}")
        return None
    return text, n, net.names


def new_package(panel) -> Optional[PackageModel]:
    """A package model from a Touchstone file the user picks, on a part they pick, with
    its ports connected by their names where they can be (None if cancelled)."""
    if panel.snapshot is None:
        return None
    path, _ = QFileDialog.getOpenFileName(panel, "Package model (Touchstone)", "", FILTER)
    if not path:
        return None
    read = read_model(panel, path)
    if read is None:
        return None
    text, n, names = read
    counts = {}
    for p in panel.snapshot.pads:
        if p.footprint:
            counts[p.footprint] = counts.get(p.footprint, 0) + 1
    values = {f.reference: f.value for f in panel.snapshot.footprints}
    refs = sorted(counts, key=lambda r: (-counts[r], r))      # the biggest part first
    items = [f"{r} {values.get(r, '')} ({counts[r]} pads)" for r in refs]
    item, ok = QInputDialog.getItem(panel, "Package model",
                                    f"{Path(path).name}: the part it is the package of",
                                    items, 0, True)
    if not ok or not item:
        return None
    ref = item.split()[0]
    k = PackageModel(_unique(f"{ref} package", [x.name for x in panel.setup.packages]), ref,
                     text, n, Path(path).name)
    k.ports = guess_ports(k, panel.snapshot, names, panel.ground())
    return k


class PackageEditor(_Editor):
    def __init__(self, panel, parent=None):
        super().__init__(panel, parent)
        f = self.form
        self.name = QLineEdit()
        self.part = QComboBox()
        make_searchable(self.part)
        self.lbl_file = QLabel()
        self.lbl_file.setWordWrap(True)
        self.btn_file = QPushButton("Replace file…")
        self.btn_file.setToolTip("Read the model again from another Touchstone file: ports "
                                 "it still has keep their connections")
        self.btn_file.clicked.connect(self._replace_file)
        file_row = QWidget()
        fl = QHBoxLayout(file_row)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(self.lbl_file, 1)
        fl.addWidget(self.btn_file)
        self.ports = QTableWidget(0, len(COLS))
        self.ports.setHorizontalHeaderLabels(COLS)
        self.ports.verticalHeader().setVisible(False)
        self.ports.setMinimumHeight(180)
        hdr = self.ports.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setStretchLastSection(True)
        self.btn_guess = QPushButton("Connect ports by their names")
        self.btn_guess.clicked.connect(self._guess)
        hint = QLabel("A PCB port joins the board at the part's pins on its net (blank pins: "
                      "every one) against its ground pins. A die port becomes a probe in the "
                      "Probes tab, on the rail of its net, with no place on the board. Other "
                      "ports are left open or shorted. PCB ports on another rail are left "
                      "open when this one is solved.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999;")
        f.addRow("Name", self.name)
        f.addRow("Part", self.part)
        f.addRow("Model", file_row)
        f.addRow(self.ports)
        f.addRow(self.btn_guess)
        f.addRow(hint)
        self._legend()
        self._mark({self.name: REQUIRED, self.part: REQUIRED, file_row: REQUIRED})
        for w, text, art in (
                (self.name, "A name for this package model; its die probes are named "
                            "after it", None),
                (self.part, "The board part the package is: its pins are where the "
                            "model's PCB ports meet the board", ("package_ports", "pcb")),
                (file_row, "The Touchstone file (.sNp: S, Y or Z data, Touchstone 1 or 2), "
                           "kept whole in the setup file. Outside its frequency range the "
                           "model is held at its first and last frequency.", None)):
            tip(f, w, text, art)
        self.ports.setToolTip(art_tip(
            "What each of the model's ports connects to: the part's pins on the PCB, a die "
            "probe, or nothing (open or shorted)", "package_ports", "pcb", "die", "open"))
        self.btn_guess.setToolTip("Connect each port named for one of the part's nets to "
                                  "that net: on the die if its name says so (die, bump), "
                                  "else to the PCB; the others are left open")
        self._watch(self.name)
        self.part.currentIndexChanged.connect(self._part_changed)

    # ------------------------------------------------------------ ports table
    def _fill_parts(self, ref: str):
        snap = self.panel.snapshot
        self.part.blockSignals(True)
        self.part.clear()
        if snap is not None:
            for fp in sorted(snap.footprints, key=lambda x: x.reference):
                self.part.addItem(f"{fp.reference} {fp.value}", fp.reference)
        i = self.part.findData(ref)
        if i < 0 and ref:
            self.part.addItem(ref, ref)
            i = self.part.count() - 1
        self.part.setCurrentIndex(i)
        self.part.blockSignals(False)

    def _net_combo(self, nets: List[str], value: str) -> QComboBox:
        c = QComboBox()
        c.addItem("", "")
        for n in nets:
            c.addItem(n, n)
        if value and c.findData(value) < 0:
            c.addItem(value, value)
        c.setCurrentIndex(max(c.findData(value), 0))
        c.currentIndexChanged.connect(self._store)
        return c

    def _pins_edit(self, pins: List[str]) -> QLineEdit:
        e = QLineEdit(",".join(pins))
        e.setPlaceholderText("all pins")
        e.editingFinished.connect(self._store)
        return e

    def _fill_ports(self, k: PackageModel):
        nets = part_nets(self.panel.snapshot, k.ref)
        t = self.ports
        t.setRowCount(0)
        for p in sorted(k.ports, key=lambda p: p.number):
            r = t.rowCount()
            t.insertRow(r)
            for col, text in ((0, str(p.number)), (1, p.name)):
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                t.setItem(r, col, it)
            role = QComboBox()
            for key, text in ROLE_TEXT.items():
                role.addItem(text, key)
            role.setCurrentIndex(max(role.findData(p.role), 0))
            role.currentIndexChanged.connect(self._store)
            t.setCellWidget(r, 2, role)
            t.setCellWidget(r, 3, self._net_combo(nets, p.net))
            t.setCellWidget(r, 4, self._pins_edit(p.pins))
            t.setCellWidget(r, 5, self._net_combo(nets, p.ground_net))
            t.setCellWidget(r, 6, self._pins_edit(p.ground_pins))
        self._enable_cells()

    def _enable_cells(self):
        t = self.ports
        for r in range(t.rowCount()):
            role = t.cellWidget(r, 2).currentData()
            for col in (3, 5):
                t.cellWidget(r, col).setEnabled(role in (PCB, DIE))
            for col in (4, 6):
                t.cellWidget(r, col).setEnabled(role == PCB)

    def _ports_value(self) -> List[PackagePort]:
        t, out = self.ports, []

        def pins(r, col):
            if t.cellWidget(r, 2).currentData() != PCB:
                return []
            return [x for x in re.split(r"[,\s]+", t.cellWidget(r, col).text().strip()) if x]
        for r in range(t.rowCount()):
            role = t.cellWidget(r, 2).currentData()
            out.append(PackagePort(int(t.item(r, 0).text()), t.item(r, 1).text(), role,
                                   t.cellWidget(r, 3).currentData() or "", pins(r, 4),
                                   t.cellWidget(r, 5).currentData() or "", pins(r, 6)))
        return out

    # ------------------------------------------------------------ actions
    def _guess(self):
        k = self.obj
        if k is None:
            return
        k.ports = guess_ports(k, self.panel.snapshot, [p.name for p in k.ports],
                              self.panel.ground())
        self.load(k)
        self.changed.emit()

    def _part_changed(self, _):
        if self._loading or self.obj is None:
            return
        self.obj.ref = self.part.currentData() or ""
        self._fill_ports(self.obj)          # the new part's nets
        self._store()

    def _replace_file(self):
        k = self.obj
        if k is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Package model (Touchstone)", "", FILTER)
        read = read_model(self, path) if path else None
        if read is None:
            return
        k.touchstone, k.n_ports, names = read
        k.file = Path(path).name
        old = {p.number: p for p in k.ports}
        guessed = guess_ports(k, self.panel.snapshot, names, self.panel.ground())
        # Ports the new file still has keep their connections; the rest are guessed.
        k.ports = [PackagePort(g.number, g.name, old[g.number].role, old[g.number].net,
                               old[g.number].pins, old[g.number].ground_net,
                               old[g.number].ground_pins) if g.number in old else g
                   for g in guessed]
        self.load(k)
        self.changed.emit()

    def _load(self, k: PackageModel):
        self.name.setText(k.name)
        self._fill_parts(k.ref)
        self.lbl_file.setText(summary(k))
        self._fill_ports(k)

    def _save(self, k: PackageModel):
        typed = self.name.text().strip() or k.name
        if typed != k.name:
            typed = _unique(typed, [x.name for x in self.panel.setup.packages if x is not k])
            rename_package(self.panel.setup, k.name, typed)
            k.name = typed
            self.name.setText(typed)
        k.ref = self.part.currentData() or k.ref
        k.ports = self._ports_value()
        self._enable_cells()
