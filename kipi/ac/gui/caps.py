"""The capacitor-model list's helpers, shared by the AC and Coupled Noise panels (#69).

Both panels edit the same models (``PISetup.caps``) with :class:`kipi.ac.gui.panel.CapEditor`;
these make, describe and bulk-create them.
"""

from __future__ import annotations

from typing import List

from qtpy.QtWidgets import QMessageBox, QWidget

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
