# Copyright (c) 2026 Tomachie LLC.
"""Solve a board's power tree with no window and write ``pdn_results.json``
(:mod:`kipi.pdn_results`) and an AC report per swept rail.

    python -m kipi --solve OUT_DIR BOARD --pdn-json PDN.json [--ac all|none|U35 VCC_CORE,...]
                   [--coarse] [--threads N] [--memory-gb GB] [--normal-priority]

On a shared machine the solve runs below normal priority, with at most N solver threads
and GB of memory (kipi.core.limits): a rail that needs more is listed not solved, out of
memory, and the next rail is tried.

DC is solved at the typical and the worst case. AC sweeps each rail asked for at the
typical case, with the capacitors' DC bias from the typical solve; a rail that cannot be
swept (no control loop bandwidth, a capacitor model without a value) is listed with why.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from kipi import pdn_results
from kipi.core import limits
from kipi.core.progress import ProgressReporter

# A coarser mesh for boards with thousands of vias, DC and AC: what a 10-layer board
# needs to solve in minutes rather than hours (2026-10-26: 1.9M unknowns instead of 8.5M
# for DC; 3.3M instead of 4.75M for AC, with 6 copper solves a decade, not 20).
COARSE_DC = dict(max_edge_mm=1.5, min_edge_mm=0.1, grading=0.5)
# A rail with a hundred parts spends as long again on the sweep after the copper solves
# (a dense solve over all its parts at each sweep point) as on them: 50 points a decade.
COARSE_AC = dict(max_edge_mm=2.0, min_edge_mm=0.1, grading=0.5, field_points_per_decade=6,
                 points_per_decade=50)


class _Print(ProgressReporter):
    """Every message and stage with the time since start, on stdout."""

    def __init__(self):
        super().__init__()
        self.t0 = time.monotonic()
        # A console or a redirected file in a legacy code page (cp1252 on Windows) cannot
        # encode "εr" or "µm": the print raised inside the AC solve and every rail was
        # listed not swept (2026-11-02). Print what it can, a "?" for the rest.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(errors="replace")
                except (ValueError, OSError):
                    pass

    def message(self, text, level=20):
        if level < 20:                  # debug detail: the log file's, not the console's
            return
        print(f"[{time.monotonic() - self.t0:7.0f} s] {text}", flush=True)

    def stage(self, name):
        print(f"[{time.monotonic() - self.t0:7.0f} s] == {name}", flush=True)


def _ac_rails(setup, ask: str) -> List[str]:
    if ask == "none":
        return []
    if ask == "all":
        return [v.name for v in setup.vrms]
    names = [n.strip() for n in ask.split(",") if n.strip()]
    known = {v.name for v in setup.vrms}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit(f"--ac: no regulator named {', '.join(unknown)} "
                         f"(the file has {', '.join(sorted(known))})")
    return names


def _gb(x: Optional[float]) -> Optional[float]:
    return round(x, 2) if x is not None else None


def _failure(e: BaseException) -> str:
    """Why a solve stopped, for the file: out of memory said as such (a MemoryError, or
    PARDISO's own error -2 when the memory cap stops its factor)."""
    text = str(e) or type(e).__name__
    if isinstance(e, MemoryError) or "memory" in text.lower() or "error code -2" in text:
        return f"out of memory ({type(e).__name__}: {text})"
    return f"{type(e).__name__}: {text}"


def _incomplete_off_rail(setup, snap, typ, vrm: str) -> list:
    """Capacitor models missing a value (no ESL in the file) with no part on ``vrm``'s
    rail: they would stop its sweep without being in it."""
    nets = {n for g in typ.groups.values() if g.kind == "rail" and g.driver == vrm
            for n in g.nets}
    on_rail = {p.footprint for p in snap.pads if p.net in nets}
    return [m for m in setup.caps
            if m.refs and not all(v and v > 0 for v in (m.capacitance_f, m.esl_h, m.esr_ohm))
            and not set(m.refs) & on_rail]


def _dc_cases(snap, setup, progress, path: str = ""):
    """DC at the worst case, let go once written, then the typical, returned for the AC
    sweeps' DC bias (holding both doubled the peak): ``(cases, typical tree, error)``."""
    from kipi.dc.pi.tree import solve_tree
    cases, typ, mesh_cache, dc_error = [], None, {}, None
    for corner in ("max", "typ"):
        setup.corner = corner
        label = f"{pdn_results.CASE_NAMES[corner]} case" + (f", {path}" if path else "")
        progress.stage(f"DC, {label}")
        try:
            tree = solve_tree(snap, setup, progress, mesh_cache=mesh_cache)
        except Exception as e:  # noqa: BLE001 - said in the file; out of memory above all
            dc_error = f"{label}: {_failure(e)}"
            progress.message(f"DC not solved: {dc_error}")
            gc.collect()
            continue
        case = pdn_results.dc_case(tree, setup, corner)
        case["peak_memory_gb"] = _gb(limits.peak_memory_gb())
        if path:
            case["path"] = path
        cases.insert(0 if corner == "typ" else len(cases), case)
        progress.message(tree.summary())
        if corner == "typ":
            typ = tree
        del tree
        gc.collect()
    setup.corner = "typ"
    return cases, typ, dc_error


def _feeding_vrm(setup, net: str):
    """The VRM whose rail ``net`` is on, through the setup's series links."""
    from kipi.dc.pi.model import net_groups
    group = net_groups(setup, [net]).get(net, frozenset([net]))
    return next((v for v in setup.vrms
                 if any(ph.output.net in group for ph in v.all_phases())), None)


def _sweep(snap, setup, typ, name: str, out_dir: Path, progress, path: str = "",
           suffix: str = "") -> dict:
    """The rail driven by ``name`` swept at the typical case ``typ``; the file's entry."""
    from kipi.ac.html_report import write_ac_report
    from kipi.ac.solver.sweep import ACError, solve_ac
    if typ is None:
        entry = pdn_results.ac_not_solved(name, "no typical DC solve to take the DC bias from")
        return dict(entry, path=path) if path else entry
    v = next(x for x in setup.vrms if x.name == name)
    progress.stage(f"AC, {name}" + (f", {path}" if path else ""))
    left_out = _incomplete_off_rail(setup, snap, typ, name)
    for m in left_out:
        setup.caps.remove(m)
        progress.message(f"{name}: capacitor model {m.name} ({', '.join(m.refs)}) is "
                         "missing a value and is on another rail; left out of this sweep")
    try:
        r = solve_ac(snap, setup, name, v.ground.net if v.ground else "",
                     progress, dc_result=typ)
        html = write_ac_report(out_dir / f"ac_{name.split()[0]}{suffix}.html", r, snap, setup)
        entry = pdn_results.ac_rail(r, setup, Path(html).name)
        entry.update(unknowns=r.stats.n_unknowns, peak_memory_gb=_gb(limits.peak_memory_gb()))
        del r                       # its factor and fields, before the next rail
    except ACError as e:
        entry = pdn_results.ac_not_solved(name, str(e))
        progress.message(f"{name}: not swept: {e}")
    except Exception as e:  # noqa: BLE001 - out of memory above all; the next rail
        entry = pdn_results.ac_not_solved(name, _failure(e))  # may still fit
        progress.message(f"{name}: not swept: {_failure(e)}")
    finally:
        setup.caps.extend(left_out)
        gc.collect()
    if left_out:
        entry["capacitor_models_left_out"] = [
            {"model": m.name, "refdes": list(m.refs), "why": "missing_value_other_rail"}
            for m in left_out]
    if path:
        entry["path"] = path
    return entry


def run(out_dir, board, pdn_json, ac: str = "all", coarse: bool = False,
        progress: Optional[ProgressReporter] = None,
        memory_gb: Optional[float] = None, overrides: Optional[str] = None) -> Path:
    """``memory_gb``: the cap :func:`kipi.core.limits.apply` set, recorded in the file.
    ``overrides``: a file of figures the engineer states (pdn_overrides), merged into the
    pdn.json before import with basis ``user``."""
    from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter, source_text
    from kipi import __version__ as VERSION
    from kipi.sources import registry

    progress = progress or _Print()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    fmt = registry.format_for(board)
    snap = registry.open_board(board).load(progress)
    pdn = json.loads(Path(pdn_json).read_text(encoding="utf-8"))
    stated: list = []
    if overrides:
        from kipi.dc.pi.importers.pdn_overrides import apply_overrides, load_overrides
        applied, unmatched = apply_overrides(pdn, load_overrides(overrides))
        for a in applied:
            progress.info(f"Stated: {a}")
        if unmatched:
            raise SystemExit("overrides name nothing in the pdn.json: " + ", ".join(unmatched))
        stated = ["Stated by the engineer (overrides file): " + "; ".join(applied)] if applied else []
    setup, notes = TomachiePdnImporter(pdn, snap).run()
    notes = list(notes) + stated
    setup.import_source = source_text(pdn, pdn_json)
    if coarse:
        for k, v in COARSE_DC.items():
            setattr(setup.settings, k, v)
        for k, v in COARSE_AC.items():
            setattr(setup.ac, k, v)

    cases, typ, dc_error = _dc_cases(snap, setup, progress)
    # Without the typical DC solve there is no DC bias to sweep with: each rail asked for
    # is listed with why, rather than left out of the file.
    rails = []
    for name in _ac_rails(setup, ac):
        rails.append(_sweep(snap, setup, typ, name, out_dir, progress))
    del typ
    gc.collect()

    # A power mux: DC and the rail feeding the input, once per input.
    for mux, k, group in setup.mux_paths():
        path = f"through {', '.join(x.name for x in group)}"
        setup2 = setup.with_mux_input(mux, k)
        cases2, typ2, err2 = _dc_cases(snap, setup2, progress, path)
        cases += cases2
        if err2:
            dc_error = f"{dc_error}; {path}: {err2}" if dc_error else f"{path}: {err2}"
        feed = _feeding_vrm(setup2, group[0].side_a.net)
        if feed is not None and feed.name in _ac_rails(setup2, ac):
            rails.append(_sweep(snap, setup2, typ2, feed.name, out_dir, progress, path,
                                f"_{group[0].name.split()[0]}"))
        del typ2
        gc.collect()

    solve = {"dc_mesh": {k: getattr(setup.settings, k) for k in COARSE_DC},
             "ac_mesh": {k: getattr(setup.ac, k) for k in COARSE_AC},
             "seconds": round(time.monotonic() - t0, 1),
             "limits": {"threads": int(os.environ["OMP_NUM_THREADS"])
                        if os.environ.get("OMP_NUM_THREADS", "").isdigit() else None,
                        "memory_gb": memory_gb},
             "peak_memory_gb": _gb(limits.peak_memory_gb())}
    if dc_error:
        solve["dc_error"] = dc_error
    doc = pdn_results.document("KiPI", VERSION, pdn, pdn_json, board,
                               fmt.name if fmt else "", solve, cases, rails, notes)
    path = pdn_results.write(out_dir / "pdn_results.json", doc)
    progress.message(f"Wrote {path}")
    return path
