"""AC results to files.

* :func:`write_csv`  |Z|, R and X at every probe and sweep frequency
* :func:`save_results` / :func:`load_results`  reopen solved rails without solving again
"""

from __future__ import annotations

import csv
import gzip
import time
from pathlib import Path
from typing import Dict

from kipi import __version__, compat
from kipi.core import results_io

RESULTS_FORMAT = 2          # 2: JSON and arrays (#99); 1: a gzipped pickle
RESULTS_KIND = "ac"
RESULTS_SUFFIX = ".kipi-ac-results"


def write_csv(path, result) -> Path:
    """One row per sweep frequency: |Z|, R and X at every probe, in ohms."""
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([f"# KiPI AC impedance, rail {result.rail} "
                    f"({' + '.join(result.rail_nets)}) against {result.ground}"])
        head = ["frequency_hz"]
        for c in result.probes:
            head += [f"{c.name} |Z| (ohm)", f"{c.name} R (ohm)", f"{c.name} X (ohm)"]
        w.writerow(head)
        for k, f in enumerate(result.freqs):
            row = [f"{f:.6g}"]
            for c in result.probes:
                z = c.z[k]
                row += [f"{abs(z):.6g}", f"{z.real:.6g}", f"{z.imag:.6g}"]
            w.writerow(row)
    return path


def save_results(path, results: Dict[str, object], board_name: str, setup) -> Path:
    """Write every solved rail's :class:`ACResult` (keyed by VRM name) to a results file:
    JSON and NumPy arrays in a zip (:mod:`kipi.core.results_io`), which opens on any machine
    and runs no code."""
    header = {"format": RESULTS_FORMAT, "kind": RESULTS_KIND, "kipi": __version__,
              "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "board": board_name,
              "setup": setup.to_dict() if setup is not None else None}
    return results_io.write(path, header, {"results": dict(results)})


def load_results(path, legacy: bool = False) -> dict:
    """``{"results", "setup", "board", "saved", ...}`` from a results file. Files saved
    before 0.8.0 are pickles, read only with ``legacy=True`` (see
    :func:`kipi.dc.pi.report.load_results`)."""
    if results_io.is_legacy(path):
        if not legacy:
            raise results_io.LegacyResults("Results saved by KiPI 0.7 or earlier")
        with gzip.open(Path(path), "rb") as fh:
            payload = compat.Unpickler(fh).load()
        if not isinstance(payload, dict) or payload.get("format", 0) > 1:
            raise ValueError("Not a KiPI results file")
        payload["legacy"] = True
    else:
        payload = results_io.read(path)
        if payload.get("format", 0) > RESULTS_FORMAT:
            raise ValueError("These results are from a newer KiPI")
    if payload.get("kind") != RESULTS_KIND:
        mode = "Coupled Noise" if payload.get("kind") == "noise" else "DC"
        raise ValueError(f"This holds {mode} results: open it in {mode} mode")
    if not payload.get("legacy"):
        from kipi.dc.pi.model import PISetup
        payload["results"] = payload.pop("data")["results"]
        payload["setup"] = PISetup.from_dict(payload["setup"]) if payload.get("setup") else None
    for result in payload["results"].values():
        # The HTML report is a separate file; offer it only if it is still there.
        report = getattr(result, "report_path", None)
        if report is not None and not Path(report).is_file():
            result.report_path = None
    return payload
