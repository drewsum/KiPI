"""Import a capacitor's or series part's SPICE model (#51).

The capacitor model and series link editors each have a button for it. It asks for a
SPICE file and lets the user pick one of its subcircuits. That subcircuit (with any it
calls) is kept in the setup, so the AC and Coupled Noise solves can take the part's
impedance from it, along with the values fitted to it (:mod:`kipi.core.model.spice`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from qtpy.QtCore import QSettings
from qtpy.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
    QVBoxLayout,
)

from kipi.core.model.spice import (
    LINK_ESL_HZ, CapValues, Library, LinkValues, SpiceError, SpiceRef, best_match,
    capacitor_ref, link_ref,
)

CAPACITOR, LINK = "capacitor", "link"
FILTER = ("SPICE models (*.lib *.mod *.sub *.subckt *.cir *.ckt *.sp *.spi *.inc *.txt);;"
          "All files (*)")
_DIR_KEY = "spice/dir"
Values = Union[CapValues, LinkValues]


def si(v: float, unit: str) -> str:
    """``v`` with an SI prefix: 1.5e-9, "H" -> "1.5 nH"."""
    for scale, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
                     (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f")):
        if abs(v) >= scale * 0.9995:
            return f"{v / scale:.4g} {p}{unit}"
    return f"{v:.3g} {unit}"


def part_values(snap, refs: Iterable[str]) -> List[str]:
    """The values of footprints ``refs`` (a part number, often), to find their subcircuit
    by."""
    if snap is None:
        return []
    by_ref = {f.reference: f.value for f in snap.footprints}
    return [by_ref[r] for r in refs if by_ref.get(r)]


def describe(values: Values) -> str:
    if isinstance(values, CapValues):
        return (f"C {si(values.capacitance_f, 'F')}, ESL {si(values.esl_h, 'H')}, "
                f"ESR {si(values.esr_ohm, 'Ω')} (self-resonant at "
                f"{si(values.srf_hz, 'Hz')})")
    esl = (f"ESL {si(values.esl_h, 'H')}" if values.esl_h is not None
           else f"no ESL (not inductive at {si(LINK_ESL_HZ, 'Hz')})")
    return f"DCR {si(values.resistance_ohm, 'Ω')}, {esl}"


class SpiceModelDialog(QDialog):
    """Pick one of a SPICE file's two-pin subcircuits, seeing the values it gives."""

    def __init__(self, lib: Library, kind: str, hints: Sequence[str] = (), parent=None):
        super().__init__(parent)
        self.lib, self.kind = lib, kind
        self.values: Optional[Values] = None
        self.ref: Optional[SpiceRef] = None
        self._cache: Dict[str, Union[Tuple[SpiceRef, Values], str]] = {}
        self.setWindowTitle(f"SPICE model: {lib.name}")
        self.resize(520, 420)
        lay = QVBoxLayout(self)
        what = ("capacitance, ESL and ESR" if kind == CAPACITOR
                else "DC resistance (used by DC) and ESL")
        hint = QLabel("Pick the part's subcircuit. It is saved with the setup, and the AC "
                      "and Coupled Noise solves take the part's impedance from it at every "
                      f"frequency. Its {what} are fitted to it and shown below.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter subcircuits…")
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)
        self.list = QListWidget()
        names = lib.two_pin()
        self.list.addItems(names)
        self.list.currentTextChanged.connect(self._picked)
        self.list.itemDoubleClicked.connect(lambda _it: self._accept_if_ok())
        lay.addWidget(self.list, 1)
        self.result = QLabel()
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        first = best_match(names, hints) or (names[0] if names else None)
        if first is not None:
            self.list.setCurrentRow(names.index(first))
        self._picked(self.list.currentItem().text() if self.list.currentItem() else "")

    @property
    def name(self) -> str:
        it = self.list.currentItem()
        return it.text() if it is not None else ""

    def _apply_filter(self, text: str):
        text = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def _picked(self, name: str):
        got = self._cache.get(name) if name else "Pick a subcircuit."
        if got is None:
            try:
                got = (capacitor_ref if self.kind == CAPACITOR else link_ref)(self.lib, name)
            except SpiceError as e:
                got = str(e)
            self._cache[name] = got
        ok = not isinstance(got, str)
        self.ref, self.values = got if ok else (None, None)
        self.result.setText(describe(self.values) if ok else got)
        self.result.setStyleSheet("" if ok else "color: #d9534f;")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def _accept_if_ok(self):
        if self.values is not None:
            self.accept()


def import_spice(parent, kind: str, hints: Sequence[str] = ()
                 ) -> Optional[Tuple[SpiceRef, Values]]:
    """Ask for a SPICE file and one of its subcircuits: the model to keep and the values
    fitted to it, or None if canceled or the file cannot be used (said)."""
    settings = QSettings("KiPI", "KiPI")
    path, _ = QFileDialog.getOpenFileName(parent, "Import SPICE model",
                                          str(settings.value(_DIR_KEY, "") or ""), FILTER)
    if not path:
        return None
    settings.setValue(_DIR_KEY, str(Path(path).parent))
    try:
        lib = Library.read(path)
    except (OSError, SpiceError) as e:
        QMessageBox.warning(parent, "Import SPICE model", f"{Path(path).name}: {e}")
        return None
    if not lib.two_pin():
        QMessageBox.information(parent, "Import SPICE model",
                                f"{lib.name} has no two-pin subcircuit (.SUBCKT) to read.")
        return None
    dlg = SpiceModelDialog(lib, kind, hints, parent)
    if dlg.exec() != QDialog.Accepted or dlg.ref is None:
        return None
    return dlg.ref, dlg.values


def spice_text(ref: SpiceRef) -> str:
    """What a part's SPICE model is, for under its editor's fields."""
    where = f" from {ref.file}" if ref.file else ""
    how = ("its impedance at every frequency comes from the model; the values above are "
           "fitted to it" if ref.use else "not in use: the values above are used")
    return f"SPICE model {ref.subckt}{where}: {how}."
