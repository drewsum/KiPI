"""The capacitor-model list's helpers, shared by the AC and Coupled Noise panels (#69).

Both panels edit the same models (``PISetup.caps``) with :class:`kipi.ac.gui.panel.CapEditor`;
these make, describe, bulk-create, export and import them.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from qtpy.QtWidgets import QFileDialog, QHBoxLayout, QMessageBox, QPushButton, QWidget

from kipi.ac.checks import capacitor_candidates
from kipi.ac.model import AGING_PCT_PER_DECADE, CapModel
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.parts import (
    is_class2, parse_capacitance, parse_dielectric, parse_voltage_rating,
)
from kipi.dc.gui.pi_panel import _unique
from kipi.dc.pi.model import PISetup


def fmt_farads(f: float) -> str:
    for scale, unit in ((1e-3, "mF"), (1e-6, "µF"), (1e-9, "nF"), (1e-12, "pF")):
        if f >= scale * 0.999:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.3g} F"


def new_cap(setup: PISetup) -> CapModel:
    return CapModel(_unique("Model1", [m.name for m in setup.caps]))


def describe_cap(m: CapModel) -> str:
    vals = (f"{fmt_farads(m.capacitance_f)}, {m.esl_h * 1e9:g} nH, {m.esr_ohm * 1e3:g} mΩ"
            if m.capacitance_f and m.esl_h and m.esr_ohm else "values needed")
    bias = f", DC bias {m.rated_v:g} V rated" if m.dc_bias else ""
    bias += f", {m.dielectric}" if m.dielectric else ""
    bias += f", ±{m.mfg_tolerance_pct:g} %" if m.mfg_tolerance_pct else ""
    bias += f", SPICE {m.spice.subckt}" if m.uses_spice else ""
    return (f"{m.name}: {vals}{bias} "
            f"({len(m.refs)} part{'s' if len(m.refs) != 1 else ''})")


def models_from_values(parent: QWidget, snapshot: BoardSnapshot, setup: PISetup,
                       nets: List[str]) -> bool:
    """One model per distinct value of the capacitors between ``nets`` that have none yet;
    True if any was added."""
    assigned = {ref for m in setup.caps for ref in m.refs}
    by_value = {}
    for c in capacitor_candidates(snapshot, nets):
        if c.ref not in assigned:
            by_value.setdefault(c.value or "(no value)", []).append(c.ref)
    if not by_value:
        QMessageBox.information(parent, "KiPI", "Every capacitor between ticked nets "
                                                "already has a model.")
        return False
    taken = [m.name for m in setup.caps]
    for value, refs in sorted(by_value.items()):
        name = _unique(value, taken)
        taken.append(name)
        farads = parse_capacitance(value) or 0.0
        rated = parse_voltage_rating(value)
        diel = parse_dielectric(value)
        # A rating turns on the DC bias estimate: only for class II ceramics.
        setup.caps.append(CapModel(name, farads, 0.0, 0.0, refs,
                                   rated_v=rated if is_class2(value) else None,
                                   dielectric=diel,
                                   aging_pct_per_decade=AGING_PCT_PER_DECADE.get(diel, 0.0)))
    return True


FILE_FILTER = "KiPI capacitor model (*.yml *.yaml *.json)"


def export_cap(parent: QWidget, m: Optional[CapModel], start: str = "") -> Optional[Path]:
    """Write ``m`` to a YAML file named after it (#229), where the user picks; the path,
    or None if cancelled."""
    from kipi.ac import cap_file
    if m is None:
        return None
    name = cap_file.file_name(m)
    path, _ = QFileDialog.getSaveFileName(parent, "Export capacitor model",
                                          str(Path(start) / name) if start else name,
                                          FILE_FILTER)
    if not path:
        return None
    try:
        return cap_file.write(path, m)
    except OSError as e:
        QMessageBox.warning(parent, "KiPI", f"Could not write {path}:\n{e}")
        return None


def import_caps(parent: QWidget, setup: PISetup, start: str = "") -> List[str]:
    """Add the models in the YAML files the user picks to ``setup`` (#229), renamed where
    a model of that name exists; their names (none: cancelled or nothing read)."""
    from kipi.ac import cap_file
    paths, _ = QFileDialog.getOpenFileNames(parent, "Import capacitor models", start,
                                            FILE_FILTER)
    added, failed = [], []
    for path in paths:
        try:
            m = cap_file.read(path)
        except (OSError, ValueError, KeyError, TypeError) as e:
            failed.append(f"{Path(path).name}: {e}")
            continue
        # " (2)", not _unique's counting on: a part number ending ...ME15 is not ...ME16.
        taken, base, k = {x.name for x in setup.caps}, m.name, 2
        while m.name in taken:
            m.name, k = f"{base} ({k})", k + 1
        setup.caps.append(m)
        added.append(m.name)
    if failed:
        QMessageBox.warning(parent, "KiPI", "Could not import:\n" + "\n".join(failed))
    return added


def add_file_buttons(cap_list, panel) -> None:
    """Import… and Export… under a panel's capacitor-model list (#229). ``panel`` has
    ``setup``, ``snapshot`` and ``_on_changed``."""
    row = QHBoxLayout()
    b_import = QPushButton("Import…")
    b_import.setToolTip("Add capacitor models from model files (YAML) exported from KiPI, "
                        "e.g. from another board's setup. They start with no parts: tick "
                        "the capacitors that use each one.")
    b_export = QPushButton("Export…")
    b_export.setToolTip("Write the selected capacitor model to a YAML file named after it: "
                        "its values, DC bias, dielectric, aging, tolerance and SPICE "
                        "model, without the parts that use it, to import into another "
                        "setup")

    def start() -> str:
        snap = panel.snapshot
        return str(snap.project_dir) if snap is not None and snap.project_dir else ""

    def do_import():
        names = import_caps(panel, panel.setup, start())
        if names:
            cap_list.reload()
            cap_list.select_name(names[-1])
            panel._on_changed()

    b_import.clicked.connect(do_import)
    b_export.clicked.connect(lambda: export_cap(panel, cap_list.current(), start()))
    row.addWidget(b_import)
    row.addWidget(b_export)
    cap_list.layout().insertLayout(1, row)
