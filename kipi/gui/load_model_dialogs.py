"""The "Model" menu of the DC Loads and AC Probes lists (#17): apply a load model file to
any number of loads or probes, add the loads or probes of a model for other copies of a
part, and export the model of a part. See :mod:`kipi.dc.pi.load_model`."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from qtpy.QtCore import QSettings, Qt
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

from kipi.dc.pi.load_model import (
    SUFFIX, LoadModel, ModelError, best_supply, model_of, model_refs, new_items,
)

FILTER = "KiPI load models (*.yml *.yaml)"
# The DC loads also read a HyperLynx PI model's DC sink currents (#149).
DC_FILTER = ("Load models (*.yml *.yaml *.hlpimodel);;KiPI load models (*.yml *.yaml);;"
             "HyperLynx PI models (*.hlpimodel)")
_DIR_KEY = "load_model/dir"
SKIP = "(leave as is)"


def _last_dir() -> str:
    return str(QSettings("KiPI", "KiPI").value(_DIR_KEY, "") or "")


def _remember_dir(path: str) -> None:
    QSettings("KiPI", "KiPI").setValue(_DIR_KEY, str(Path(path).parent))


def open_model(parent, hyperlynx: bool = False) -> Optional[LoadModel]:
    """Ask for a model file and read it; None if canceled or it cannot be read (said).
    With ``hyperlynx``, a HyperLynx ``.hlpimodel`` can be chosen too."""
    path, _ = QFileDialog.getOpenFileName(parent, "Open load model", _last_dir(),
                                          DC_FILTER if hyperlynx else FILTER)
    if not path:
        return None
    _remember_dir(path)
    try:
        model = LoadModel.load(path)
    except ModelError as e:
        QMessageBox.warning(parent, "Load model", f"{Path(path).name}: {e}")
        return None
    notes = "\n\n" + "\n".join(f"• {n}" for n in model.notes) if model.notes else ""
    if not model.supplies:
        QMessageBox.information(parent, "Load model",
                                f"{Path(path).name} has no supplies.{notes}")
        return None
    if notes:
        QMessageBox.information(parent, "Load model", f"Notes on {Path(path).name}:{notes}")
    return model


def _title(model: LoadModel) -> str:
    return model.part or "the model"


class ApplyDialog(QDialog):
    """Which of a model's supplies to apply to each load or probe: ``items`` are
    (name, PinRef) pairs. Rows the model's supplies match are ticked."""

    def __init__(self, model: LoadModel, items: Sequence[Tuple[str, object]], noun: str,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Apply load model to {noun}s")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        lay = QVBoxLayout(self)
        intro = QLabel(f"Apply <b>{_title(model)}</b> to the ticked {noun}s, each from the "
                       "supply chosen beside it. Only what the model gives is set "
                       + ("(current or pin groups, tolerance, heat)." if noun == "load"
                          else "(the target, or one from the load step)."), self)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.table = QTableWidget(len(items), 2, self)
        self.table.setHorizontalHeaderLabels([noun.capitalize(), "Supply in the model"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.checks: List[QCheckBox] = []
        self.boxes: List[QComboBox] = []
        for row, (name, pins) in enumerate(items):
            chk = QCheckBox(name, self.table)
            box = QComboBox(self.table)
            box.addItem(SKIP, -1)
            for i, s in enumerate(model.supplies):
                box.addItem(f"{s.name}  ({s.summary()})", i)
            # Ticked where its pins or name match; a model of one supply is chosen
            # beside every row, to tick by hand.
            sure = best_supply(model, name, pins, only=False)
            guess = best_supply(model, name, pins)
            if guess is not None:
                box.setCurrentIndex(guess + 1)
            chk.setChecked(sure is not None)
            box.currentIndexChanged.connect(
                lambda _i, c=chk, b=box: c.setChecked(b.currentData() >= 0))
            self.table.setCellWidget(row, 0, chk)
            self.table.setCellWidget(row, 1, box)
            self.checks.append(chk)
            self.boxes.append(box)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, on in (("Tick all", True), ("Untick all", False)):
            b = QPushButton(text, self)
            b.clicked.connect(lambda _=False, on=on: [c.setChecked(on) for c in self.checks])
            row.addWidget(b)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self.resize(640, 420)

    def chosen(self) -> List[Tuple[int, int]]:
        """[(item row, supply index)] for the ticked rows with a supply."""
        return [(r, b.currentData()) for r, (c, b) in enumerate(zip(self.checks, self.boxes))
                if c.isChecked() and b.currentData() >= 0]


class PartsDialog(QDialog):
    """Which parts to add a model's loads or probes for: ``parts`` are (ref, value, number
    of the model's supplies it has the pins of)."""

    def __init__(self, model: LoadModel, parts: Sequence[Tuple[str, str, int]], noun: str,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Add {noun}s from load model")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        lay = QVBoxLayout(self)
        n = len(model.supplies)
        split = any(s.split_by_net for s in model.supplies)
        if split:       # a HyperLynx model's pins, one load per net they are on (#149)
            text = (f"Add a {noun} for each net the pins of <b>{_title(model)}</b> are on, "
                    "on the ticked parts. Listed are the parts that have any of its pins.")
        else:
            text = (f"Add a {noun} for each supply of <b>{_title(model)}</b> on the ticked "
                    f"parts. Listed are the parts that have the pins of at least one of its "
                    f"{n} suppl{'y' if n == 1 else 'ies'}; a supply's net is the one its pins "
                    "are on.")
        intro = QLabel(text, self)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText("Filter by reference or value…")
        self.filter.textChanged.connect(self._filter)
        lay.addWidget(self.filter)
        self.list = QListWidget(self)
        for ref, value, k in parts:
            it = QListWidgetItem(f"{ref}  {value}  ({k} net{'' if k == 1 else 's'})" if split
                                 else f"{ref}  {value}  ({k} of {n} supplies)")
            it.setData(Qt.UserRole, ref)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        bb.button(QDialogButtonBox.Ok).setText("Add")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(460, 420)

    def _filter(self, text: str):
        t = text.lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def chosen(self) -> List[str]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


def candidate_parts(model: LoadModel, snap) -> List[Tuple[str, str, int]]:
    """(ref, value, supplies matched) for the parts with a model supply's pins."""
    from kipi.dc.pi.autofill import part_nets
    if snap is None:
        return []
    values = {f.reference: f.value for f in snap.footprints}
    out = []
    for ref in {p.footprint for p in snap.pads if p.footprint}:
        k = len(new_items(model, ref, part_nets(snap, ref)))
        if k:
            out.append((ref, values.get(ref, ""), k))
    return sorted(out, key=lambda t: (-t[2], _natural(t[0])))


def _natural(ref: str):
    import re
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", ref)]


def export_model(parent, setup, snap) -> None:
    """Ask for a part with loads or probes and a file, and write its model there."""
    refs = model_refs(setup)
    if not refs:
        QMessageBox.information(parent, "Export load model",
                                "No load or probe in the setup names a part yet.")
        return
    ref, ok = QInputDialog.getItem(parent, "Export load model",
                                   "Part (its loads and probes go in the model):", refs, 0,
                                   False)
    if not ok:
        return
    value = next((f.value for f in (snap.footprints if snap else []) if f.reference == ref),
                 "")
    part, ok = QInputDialog.getText(parent, "Export load model",
                                    "What the model is of (part number or description):",
                                    QLineEdit.Normal, value or ref)
    if not ok:
        return
    stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in (part or ref))
    start = str(Path(_last_dir() or ".") / f"{stem}{SUFFIX}")
    path, _ = QFileDialog.getSaveFileName(parent, "Export load model", start, FILTER)
    if not path:
        return
    _remember_dir(path)
    try:
        saved = model_of(setup, ref, part.strip()).save(path)
    except OSError as e:
        QMessageBox.warning(parent, "Export load model", f"Could not write {path}: {e}")
        return
    QMessageBox.information(parent, "Export load model", f"Saved the model of {ref} to\n{saved}")


def model_button(parent: QWidget, noun: str, apply: Callable[[], None],
                 add: Callable[[], None], export: Callable[[], None]) -> QPushButton:
    """The "Model ▾" button of a list of loads or probes."""
    b = QPushButton("Model ▾", parent)
    b.setToolTip(f"Load models: a part's supplies in a file of their own (YAML), to apply "
                 f"to many {noun}s at once, to add the {noun}s of other copies of the part, "
                 "or to use on other boards"
                 + (". A HyperLynx PI model (.hlpimodel) gives its DC sink currents pin by "
                    "pin, as a load on each net its pins are on" if noun == "load" else ""))
    menu = QMenu(b)
    menu.addAction(f"Apply model file to {noun}s…", apply)
    menu.addAction(f"Add {noun}s from model file…", add)
    menu.addSeparator()
    menu.addAction("Export a part's model…", export)
    b.setMenu(menu)
    return b
