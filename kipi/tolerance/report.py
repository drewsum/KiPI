"""One load's tolerance stack-up as a report (#167): each part, where it came from, the DC,
AC and total sums against the load's tolerances, and the verdict. A web page or a PDF, as
the other reports are."""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Dict, Optional

from kipi.dc.report.html import CSS, _e, _table, _verdict, finish
from kipi.tolerance.evaluate import CHECK_NAMES, Stack
from kipi.tolerance.model import KIND_NAMES, LoadEntry

PART_NAMES = {**KIND_NAMES, "accuracy": "VRM DC accuracy"}
METHOD = ("Each side is added apart: DC = DC drop (mean of the load's pins) + VRM DC "
          "accuracy, against the DC tolerance; AC = ripple + transient, against the AC "
          "tolerance; total = DC + AC, against the tolerance. Ripple from several switchers "
          "at one probe is their root sum of squares. A check with a part not known yet can "
          "fail but not pass.")


def report_name(board_stem: str, load: str, when: Optional[_dt.datetime] = None,
                fmt: str = "html") -> str:
    """'<board>_tolerance_<load>_<time stamp>.html' (or .pdf)."""
    from kipi.core.naming import timestamp
    safe = re.sub(r"[^\w.-]+", "_", load).strip("_") or "load"
    return f"{board_stem}_tolerance_{safe}_{timestamp(when)}.{fmt}"


def _mv(v: Optional[float]) -> str:
    return "" if v is None else f"{v * 1e3:.3f}"


def _pct(v: Optional[float], nominal: Optional[float]) -> str:
    return "" if v is None or not nominal else f"{100 * v / abs(nominal):.3f}"


def write_load_report(path: Path, board: str, load, stack: Stack, entry: Optional[LoadEntry],
                      bw: bool, hints: Dict[str, str]) -> Path:
    """Write ``load``'s stack-up to ``path`` (.pdf: a PDF, else a web page). ``hints``
    say what to run for the parts not known."""
    path = Path(path)
    pdf = path.suffix.lower() == ".pdf"
    entry = entry or LoadEntry()
    nominal = stack.nominal
    title = f"Tolerance stack-up: {load.name}"
    when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"<h1>{_e(title)}</h1>",
             f"<div class='meta'>{_e(board)} · {_e(when)}</div>"]
    verdict = stack.verdict
    shown = _verdict(verdict, "" if verdict is not None else "not known yet")
    parts.append(f"<p><b>Verdict:</b> {shown}</p>")
    info = [["Supply", _e(f"{load.supply.ref} on {load.supply.net}")],
            ["Ground", _e(f"{load.ground.ref} on {load.ground.net}" if load.ground else "–")],
            ["VRM", _e(stack.vrm or "none drives this rail")],
            ["Nominal", _e(f"{nominal:g} V" if nominal is not None else "–")],
            ["Tolerance", _e(load.tolerance_label() or "not set")],
            ["DC tolerance", _e(load.dc_tolerance_label() or "not set")],
            ["AC tolerance", _e(load.ac_tolerance_label() or "not set")],
            ["Ripple and transient", "through a 20 MHz bandwidth limit" if bw
             else "full bandwidth"]]
    parts.append("<h2>Load</h2>" + _table(["", ""], info))

    rows = []
    for kind in ("dc", "accuracy", "ripple", "transient"):
        p = stack.parts.get(kind)
        c = entry.get(kind) if kind != "accuracy" else None
        if kind == "accuracy":
            source = "VRM setup" if stack.accuracy_set else "not set on the VRM (taken as 0)"
            detail, date = "", ""
        elif c is not None:
            source, date, detail = c.source, c.when, c.detail
        elif p is not None:
            source, date, detail = "not applicable", "", "no switcher on this rail"
        else:
            source, date, detail = "not known", "", hints.get(kind, "")
        rows.append([_e(PART_NAMES[kind]),
                     _mv(p[0]) if p else "", _mv(p[1]) if p else "",
                     _pct(p[0], nominal) if p else "", _pct(p[1], nominal) if p else "",
                     _e(source), _e(date), _e(detail)])
    parts.append("<h2>Parts</h2>" + _table(
        ["Part", "Below (mV)", "Above (mV)", "Below (%)", "Above (%)", "Source", "When",
         "Detail"], rows))

    rows = []
    for kind, c in stack.checks.items():
        m_lo, m_hi = c.margins()
        rows.append([_e(CHECK_NAMES[kind]), _mv(c.below_v), _mv(c.above_v),
                     _mv(c.allowed[0]) or "–", _mv(c.allowed[1]) or "–",
                     _mv(m_lo), _mv(m_hi),
                     _verdict(c.passed, "" if c.passed is not None else
                              ("no tolerance" if not c.has_limit else "parts missing"))])
    parts.append("<h2>Checks</h2>" + _table(
        ["Check", "Below (mV)", "Above (mV)", "Allowed below (mV)", "Allowed above (mV)",
         "Margin below (mV)", "Margin above (mV)", "Verdict"], rows))
    parts.append(f"<p class='note'>{_e(METHOD)}</p>")
    return finish(path, title, parts, CSS, pdf)
