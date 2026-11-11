# Copyright (c) 2026 Tomachie LLC.
"""``pdn_results.json``: what the copper solve found, for the tool that wrote the pdn.json
to read back (Tomachie's report). One file for any PDN analyzer; ``analyzer`` names the
one that wrote it.

Every result is a fixed word (``pass``, ``fail``, ``no_window`` ...), never a sentence:
the reader words it in its own language. Values are SI, a value not found is ``None``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

SCHEMA = "pdn_results"
SCHEMA_VERSION = 1
CASE_NAMES = {"typ": "typical", "max": "worst"}
# |Z| reported at these frequencies in each probe's ``z_at``.
Z_AT_HZ = (1e5, 1e6, 1e7, 1e8)


def sha256(path) -> Optional[str]:
    """The file's SHA-256, or None for a folder (an unpacked ODB++ job)."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _num(x) -> Optional[float]:
    """A float for JSON, None where there is none (NaN, infinity)."""
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _rail(label: str) -> str:
    """The rail's first net from a group label ("VDD_CORE (+5 nets)" -> "VDD_CORE")."""
    return label.split(" (+")[0]


def _pins(ref) -> List[str]:
    return [f"{ref.ref}_{p}" for p in ref.pins] if ref is not None else []


def load_result(r) -> str:
    if r.status:
        return "not_solved"
    if r.passed is None:
        return "no_window"
    return "pass" if r.passed else "fail"


def spare_v(r) -> Optional[float]:
    """How far the load's worst pin is inside its window (negative: outside)."""
    ends = []
    if r.limit_low is not None and r.v_low is not None:
        ends.append(r.v_low - r.limit_low)
    if r.limit_high is not None and r.v_high is not None:
        ends.append(r.limit_high - r.v_high)
    return _num(min(ends)) if ends else None


def _load(r, setup) -> dict:
    load = next((l for l in setup.loads if l.name == r.name), None)
    return {"load": r.name,
            "refdes": load.supply.ref if load else r.name.split()[0],
            "rail": load.supply.net if load else _rail(r.group),
            "pins": _pins(load.supply) if load else [],
            "current_a": _num(r.current), "current_basis": r.basis or None,
            "current_known": bool(load.current_known) if load else True,
            "v_low": _num(r.v_low), "v_high": _num(r.v_high),
            "limit_low": _num(r.limit_low), "limit_high": _num(r.limit_high),
            "result": load_result(r), "spare_v": spare_v(r), "reason": r.status or None}


def _regulator(v, setup) -> dict:
    vrm = next((x for x in setup.vrms if x.name == v.name), None)
    if v.regulation:
        result = "out_of_regulation"
    elif v.passed is None:
        result = "no_limit"
    else:
        result = "within" if v.passed else "above"
    return {"regulator": v.name, "refdes": vrm.output.ref if vrm else v.name.split()[0],
            "rail": _rail(v.group), "v_set": _num(v.v_set), "v_out": _num(v.v_out),
            "i_out_a": _num(v.i_out), "i_limit_a": _num(v.current_limit_a),
            "result": result, "reason": v.regulation or None}


def _series(k) -> dict:
    if k.rated_a is None:
        result = "no_rating"
    elif k.within is None:
        result = "not_solved"
    else:
        result = "within" if k.within else "above"
    return {"refdes": k.name, "rail": _rail(k.group), "current_a": _num(k.current),
            "rated_a": _num(k.rated_a), "result": result}


def _headroom(h) -> dict:
    return {"rail": _rail(h.group), "regulator": h.vrm, "known_a": _num(h.known_a),
            "unknown": list(h.unknown), "room_a": _num(h.room_a),
            "tightest": h.tightest or None, "margin_v": _num(h.margin_v),
            "per_amp_v": _num(h.per_amp_v)}


def dc_case(tree, setup, corner: str) -> dict:
    """One solved case of the power tree (``corner`` "typ" or "max")."""
    loads = [_load(r, setup) for r in tree.loads]
    series = [_series(k) for k in tree.link_ratings]
    count = lambda rows, word: sum(1 for x in rows if x["result"] == word)   # noqa: E731
    return {"case": CASE_NAMES.get(corner, corner),
            "summary": {"loads": len(loads), "pass": count(loads, "pass"),
                        "fail": count(loads, "fail"), "no_window": count(loads, "no_window"),
                        "not_solved": count(loads, "not_solved"),
                        "series_within": count(series, "within"),
                        "series_above": count(series, "above"),
                        "series_no_rating": count(series, "no_rating"),
                        "series_not_solved": count(series, "not_solved")},
            "loads": loads,
            "regulators": [_regulator(v, setup) for v in tree.vrms],
            "series_parts": series,
            "headroom": [_headroom(h) for h in tree.headroom],
            "seconds": _num(tree.solve_s)}


def _probe(p, freqs, setup) -> dict:
    probe = next((x for x in setup.probes if x.name == p.name), None)
    passed = p.passed(freqs)
    worst_ohm, worst_hz = p.worst(freqs)
    return {"load": p.name,
            "refdes": probe.rail.ref if probe else p.name.split()[0],
            "target_ohm": _num(p.target_ohm), "target_hz": _num(p.target_hz),
            "target_points": ([[_num(f), _num(z)] for f, z in p.target_points]
                              if p.target_points else None),
            "target_from_hz": _num(p.from_hz),
            "target_from":(probe.target_from if probe else "") or None,
            "result": "no_target" if passed is None else ("pass" if passed else "fail"),
            "worst_ohm": _num(worst_ohm), "worst_hz": _num(worst_hz),
            "z_at": {f"{hz:g}": _num(np.interp(hz, freqs, p.mag)) for hz in Z_AT_HZ
                     if freqs[0] <= hz <= freqs[-1]}}


def ac_rail(r, setup, report_html: Optional[str] = None) -> dict:
    """One swept rail (an :class:`kipi.ac.solver.sweep.ACResult`)."""
    f = np.asarray(r.freqs)
    return {"regulator": r.rail, "rail_nets": list(r.rail_nets), "ground": r.ground,
            "result": "solved", "reason": None, "bandwidth_hz": _num(r.bandwidth_hz),
            "f_start_hz": _num(f[0]), "f_stop_hz": _num(f[-1]),
            "capacitors": len(r.caps), "capacitors_without_model": list(r.missing_caps),
            "probes": [_probe(p, f, setup) for p in r.probes],
            "curve": {"freqs_hz": [float(x) for x in f],
                      "z_ohm": {p.name: [float(x) for x in p.mag] for p in r.probes}},
            "report_html": report_html, "seconds": _num(r.solve_s)}


def ac_not_solved(regulator: str, reason: str) -> dict:
    return {"regulator": regulator, "result": "not_solved", "reason": reason}


def document(analyzer: str, version: str, pdn: dict, pdn_path, board_path, board_format: str,
             solve: dict, dc_cases: List[dict], ac_rails: List[dict],
             notes: List[str]) -> dict:
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION,
            "analyzer": {"name": analyzer, "version": version},
            "date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pdn_json": {"design": pdn.get("design"), "generated_by": pdn.get("generated_by"),
                         "date": pdn.get("date"), "sha256": sha256(pdn_path)},
            "board": {"file": Path(board_path).name, "format": board_format,
                      "sha256": sha256(board_path)},
            "solve": solve,
            "dc": {"cases": dc_cases},
            "ac": {"rails": ac_rails},
            "import_notes": list(notes)}


def write(path, doc: Dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path
