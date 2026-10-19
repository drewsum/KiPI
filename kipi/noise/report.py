"""Coupled Noise results to files (#69).

* :func:`save_results` / :func:`load_results`  reopen solved aggressors without solving
  again, as JSON and arrays in a zip (:mod:`kipi.core.results_io`).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict

from kipi import __version__
from kipi.core import results_io

RESULTS_FORMAT = 1
RESULTS_KIND = "noise"
RESULTS_SUFFIX = ".kipi-noise-results"


def save_results(path, results: Dict[str, object], board_name: str, setup) -> Path:
    """Write every solved aggressor's :class:`~kipi.noise.result.NoiseResult` (keyed by
    aggressor name) to a results file."""
    header = {"format": RESULTS_FORMAT, "kind": RESULTS_KIND, "kipi": __version__,
              "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "board": board_name,
              "setup": setup.to_dict() if setup is not None else None}
    return results_io.write(path, header, {"results": dict(results)})


def load_results(path) -> dict:
    """``{"results", "setup", "board", "saved", ...}`` from a results file."""
    if results_io.is_legacy(path):
        raise ValueError("Not a Coupled Noise results file")
    payload = results_io.read(path)
    if payload.get("format", 0) > RESULTS_FORMAT:
        raise ValueError("These results are from a newer KiPI")
    kind = payload.get("kind")
    if kind != RESULTS_KIND:
        mode = "AC" if kind == "ac" else "DC"
        raise ValueError(f"This holds {mode} results: open it in {mode} mode")
    from kipi.dc.pi.model import PISetup
    payload["results"] = payload.pop("data")["results"]
    payload["setup"] = PISetup.from_dict(payload["setup"]) if payload.get("setup") else None
    for result in payload["results"].values():
        report = getattr(result, "report_path", "")
        if report and not Path(report).is_file():
            result.report_path = ""
    return payload
