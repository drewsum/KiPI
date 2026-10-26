"""Import a capacitor's or series part's SPICE model (#51).

The capacitor model and series link editors each have a button for it. It asks for a
SPICE file and lets the user pick one of its subcircuits, or for a series part one of its
diode models (#191). That model (with any subcircuits and diode models it uses) is kept
in the setup, so the AC and Coupled Noise solves can take the part's impedance from it,
along with the values fitted to it (:mod:`kipi.core.model.spice`).

A series part may also be a subcircuit with more than two pins (#193: a common-mode
choke, a feedthrough capacitor). The dialog then maps each of its pins to one of the
part's pins, and each becomes a side of the link.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from qtpy.QtCore import QSettings
from qtpy.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QLabel,
    QLineEdit, QListWidget, QMessageBox, QVBoxLayout,
)

from kipi.core.model.spice import (
    LINK_BIAS_A, LINK_ESL_HZ, CapValues, Library, LinkValues, SpiceError, SpiceRef, best_match,
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


def part_pins(snap, ref: str) -> List[Tuple[str, str]]:
    """Footprint ``ref``'s pins on nets, (pin number, net), in pin order: what a multi-pin
    model's pins can be mapped to (#193)."""
    if snap is None or not ref:
        return []
    pins = {p.number: p.net for p in snap.pads if p.footprint == ref and p.net and p.number}
    return sorted(pins.items(), key=lambda kv: (len(kv[0]), kv[0]))


def default_pins(model_pins: Sequence[str], pins: Sequence[Tuple[str, str]]) -> List[str]:
    """The part pin each of a model's pins starts mapped to: the one numbered like it,
    else the next unused one in order."""
    numbers = [n for n, _net in pins]
    out: List[Optional[str]] = [next((n for n in numbers if n.lower() == m.lower()), None)
                                for m in model_pins]
    free = iter([n for n in numbers if n not in out])
    return [n if n is not None else next(free, numbers[0] if numbers else "") for n in out]


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
    if values.forward:
        drops = ", ".join(f"{v:.3g} V at {si(i, 'A')}" for i, v in values.forward)
        esl = f", ESL {si(values.esl_h, 'H')}" if values.esl_h is not None else ""
        text = (f"Diode, side A to side B: {drops}; slope resistance "
                f"{si(values.resistance_ohm, 'Ω')} at {si(LINK_BIAS_A, 'A')}{esl}")
        if values.forward[0][1] > 5:
            text += (". It hardly conducts from its first pin to its second: is its first "
                     "pin the cathode?")
        return text
    if values.paths is not None:
        paths = ", ".join(f"{a + 1}–{b + 1} {si(r, 'Ω')}" if r else f"{a + 1}–{b + 1} short"
                          for a, b, r in values.paths)
        return (f"{values.pins} pins, joined at DC: {paths}. DC takes its conductance "
                "between its sides, AC and Coupled Noise its admittance at every "
                "frequency.")
    esl = (f"ESL {si(values.esl_h, 'H')}" if values.esl_h is not None
           else f"no ESL (not inductive at {si(LINK_ESL_HZ, 'Hz')})")
    return f"DCR {si(values.resistance_ohm, 'Ω')}, {esl}"


class SpiceModelDialog(QDialog):
    """Pick one of a SPICE file's two-pin subcircuits, seeing the values it gives; for a
    series part with ``pins`` (its (pin number, net)), a multi-pin one too, its pins
    mapped to the part's."""

    def __init__(self, lib: Library, kind: str, hints: Sequence[str] = (), parent=None,
                 pins: Sequence[Tuple[str, str]] = ()):
        super().__init__(parent)
        self.lib, self.kind, self.part_pins = lib, kind, list(pins)
        self.values: Optional[Values] = None
        self.ref: Optional[SpiceRef] = None
        self.combos: List[QComboBox] = []
        self._cache: Dict[str, Union[Tuple[SpiceRef, Values], str]] = {}
        self.setWindowTitle(f"SPICE model: {lib.name}")
        self.resize(520, 420)
        lay = QVBoxLayout(self)
        what = ("capacitance, ESL and ESR" if kind == CAPACITOR
                else "DC resistance (used by DC) and ESL; a diode model's forward drop "
                     "(DC takes it from the model at each current)")
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
        names = _names(lib, kind, len(self.part_pins))
        self.list.addItems(names)
        self.list.currentTextChanged.connect(self._picked)
        self.list.itemDoubleClicked.connect(lambda _it: self._accept_if_ok())
        lay.addWidget(self.list, 1)
        self.result = QLabel()
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        # A multi-pin model's pins, each mapped to a pin of the part: its sides.
        self.pin_box = QGroupBox("The part's pin for each of the model's pins")
        self.pin_form = QFormLayout(self.pin_box)
        self.pin_box.setVisible(False)
        lay.addWidget(self.pin_box)
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

    @property
    def pins(self) -> Optional[List[str]]:
        """The part's pin for each of a multi-pin model's pins, in order; None for a
        two-pin model."""
        return [c.currentData() for c in self.combos] if self.combos else None

    def _show_pins(self, model_pins: Sequence[str]):
        while self.pin_form.rowCount():
            self.pin_form.removeRow(0)
        self.combos = []
        if len(model_pins) <= 2:
            self.pin_box.setVisible(False)
            return
        starts = default_pins(model_pins, self.part_pins)
        for i, (m, start) in enumerate(zip(model_pins, starts)):
            c = QComboBox()
            for number, net in self.part_pins:
                c.addItem(f"{number} · {net}", number)
            c.setCurrentIndex(max(c.findData(start), 0))
            c.currentIndexChanged.connect(lambda _i: self._check_pins())
            side = "side A" if i == 0 else "side B" if i == 1 else f"side {chr(65 + i)}"
            self.pin_form.addRow(f"{i + 1}: {m} ({side})", c)
            self.combos.append(c)
        self.pin_box.setVisible(True)

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
        self._show_pins(self.lib.pins(name) if ok else [])
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)
        if ok:
            self._check_pins()

    def _check_pins(self):
        """A multi-pin model's pins each on a pin of their own."""
        pins = self.pins
        ok = pins is None or len(set(pins)) == len(pins)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok and self.values is not None)
        if self.values is None:
            return
        self.result.setText(describe(self.values) if ok else
                            "Map each of the model's pins to a different pin of the part.")
        self.result.setStyleSheet("" if ok else "color: #d9534f;")

    def _accept_if_ok(self):
        if self.buttons.button(QDialogButtonBox.Ok).isEnabled():
            self.accept()


def _names(lib: Library, kind: str, n_pins: int = 0) -> List[str]:
    """What a part can be: two-pin subcircuits, and for a series part diode models and,
    for a part with ``n_pins`` pins on nets, subcircuits with up to that many pins."""
    if kind != LINK:
        return lib.two_pin()
    multi = [n for n in lib.multi_pin() if len(lib.pins(n)) <= n_pins]
    return lib.two_pin() + multi + lib.diodes()


def import_spice(parent, kind: str, hints: Sequence[str] = (),
                 pins: Sequence[Tuple[str, str]] = ()
                 ) -> Optional[Tuple[SpiceRef, Values, Optional[List[str]]]]:
    """Ask for a SPICE file and one of its subcircuits: the model to keep, the values
    fitted to it and, for a multi-pin model (a series part with ``pins``, its (pin
    number, net)), the part's pin for each of its pins; or None if canceled or the file
    cannot be used (said)."""
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
    if not _names(lib, kind, len(pins)):
        what = "subcircuit (.SUBCKT)" if kind == CAPACITOR else "subcircuit or diode model"
        QMessageBox.information(parent, "Import SPICE model",
                                f"{lib.name} has no two-pin {what} to read.")
        return None
    dlg = SpiceModelDialog(lib, kind, hints, parent, pins)
    if dlg.exec() != QDialog.Accepted or dlg.ref is None:
        return None
    return dlg.ref, dlg.values, dlg.pins


def spice_text(ref: SpiceRef) -> str:
    """What a part's SPICE model is, for under its editor's fields."""
    where = f" from {ref.file}" if ref.file else ""
    how = ("its impedance at every frequency comes from the model; the values above are "
           "fitted to it" if ref.use else "not in use: the values above are used")
    return f"SPICE model {ref.subckt}{where}: {how}."
