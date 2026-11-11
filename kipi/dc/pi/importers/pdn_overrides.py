# Copyright (c) 2026 Tomachie LLC.
"""Figures the engineer states for a headless run, merged into the Tomachie pdn.json
before import, in the file's own shape with basis ``user``: the importer, the report
and pdn_results.json then carry them like any other value, marked as stated.

The overrides file::

    {"regulators": {"IC30": {"f_sw_hz": 800000, "bandwidth_hz": 80000}},
     "loads": {"P1V2:IC14": {"i_step_a": 0.7, "t_rise_s": 1e-6, "supply_min_v": 1.14, "supply_max_v": 1.26}}}

A regulator is named by its refdes, a load by ``<rail>:<refdes>``. A name the file
does not hold is reported, never silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

FROM = "stated by the engineer (overrides file)"

_REG_KEYS = {"f_sw_hz": ("f_sw", "hz"), "bandwidth_hz": ("bandwidth", "hz")}
_LOAD_KEYS = {"i_step_a": ("i_step", "amps"), "t_rise_s": ("t_rise", "s"),
              "slew_a_per_s": ("slew", "amps_per_s"), "f_max_hz": ("f_max", "hz")}
# A load's supply range sits on the load itself, not in its "ac" block.
_LOAD_RANGE = {"supply_min_v": "supply_min", "supply_max_v": "supply_max"}


def _value(v: float, unit: str) -> dict:
    return {unit: float(v), "basis": "user", "from": FROM}


def apply_overrides(pdn: dict, overrides: dict) -> Tuple[List[str], List[str]]:
    """Merge ``overrides`` into ``pdn``. Returns (applied, unmatched) notes."""
    applied: List[str] = []
    unmatched: List[str] = []
    regs: Dict[str, dict] = {g.get("refdes", ""): g for g in pdn.get("regulators", [])}
    for refdes, vals in (overrides.get("regulators") or {}).items():
        g = regs.get(refdes)
        if g is None:
            unmatched.append(f"regulator {refdes}")
            continue
        if not isinstance(g.get("ac"), dict):
            g["ac"] = {}
        ac = g["ac"]
        for key, (field, unit) in _REG_KEYS.items():
            if vals.get(key) is not None and float(vals[key]) > 0:
                ac[field] = _value(vals[key], unit)
                applied.append(f"{refdes} {field} = {float(vals[key]):g} {unit}, stated")
    loads: Dict[str, dict] = {}
    for r in pdn.get("rails", []):
        for l in r.get("loads", []):
            loads[f"{r.get('name')}:{l.get('refdes')}"] = l
    for name, vals in (overrides.get("loads") or {}).items():
        l = loads.get(name)
        if l is None:
            unmatched.append(f"load {name}")
            continue
        if not isinstance(l.get("ac"), dict):
            l["ac"] = {}
        ac = l["ac"]
        for key, (field, unit) in _LOAD_KEYS.items():
            if vals.get(key) is not None and float(vals[key]) > 0:
                ac[field] = _value(vals[key], unit)
                if field == "i_step":
                    ac.pop("i_step_because", None)
                applied.append(f"{name} {field} = {float(vals[key]):g} {unit}, stated")
        for key, field in _LOAD_RANGE.items():
            if vals.get(key) is not None and float(vals[key]) > 0:
                l[field] = {"volts": float(vals[key]), "basis": "user", "from": FROM}
                applied.append(f"{name} {field} = {float(vals[key]):g} V, stated")
    return applied, unmatched


def load_overrides(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
