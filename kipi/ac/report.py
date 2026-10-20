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


def write_touchstone(path, result, probe) -> Path:
    """Write one probe's sampled driving-point impedance as a Touchstone Z1P file."""
    path = Path(path)
    if path.suffix.lower() != ".s1p":
        path = path.with_suffix(".s1p")
    curve = next((c for c in result.probes if c.name == probe), None)
    if curve is None:
        raise ValueError(f"No impedance curve for probe {probe}")
    with path.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(f"! KiPI AC impedance, rail {result.rail}, probe {probe}\n")
        fh.write("# Hz Z RI R 50\n")
        for freq, z in zip(result.freqs, curve.z):
            fh.write(f"{freq:.12g} {z.real:.12g} {z.imag:.12g}\n")
    return path


def write_fitted_poles(path, result, probe, poles: int = 12) -> Path:
    """Fit one driving-point curve with stable real poles and write HyperLynx PLS.

    The PLS file contains a one-port Z model. Each probe is exported independently because
    ACResult stores driving-point impedances, not the mutual impedance matrix.
    """
    import numpy as np

    path = Path(path)
    if path.suffix.lower() != ".pls":
        path = path.with_suffix(".pls")
    curve = next((c for c in result.probes if c.name == probe), None)
    if curve is None:
        raise ValueError(f"No impedance curve for probe {probe}")
    f = np.asarray(result.freqs, dtype=float)
    z = np.asarray(curve.z, dtype=complex)
    valid = np.isfinite(f) & (f > 0) & np.isfinite(z.real) & np.isfinite(z.imag)
    f, z = f[valid], z[valid]
    if len(f) < 2:
        raise ValueError("At least two finite positive-frequency points are required")
    # Fixed logarithmic poles keep the model stable; least squares chooses their residues.
    alpha_hz = np.geomspace(f.min() / 10, f.max() * 10, max(1, min(poles, len(f) - 2)))
    # PLS expresses both pole coordinates and s in Hz (j*f), rather than rad/s.
    s = 1j * f
    basis = 1 / (s[:, None] + alpha_hz[None, :])
    design = np.column_stack((basis, np.ones(len(f)), s))
    real_design = np.vstack((design.real, design.imag))
    real_target = np.r_[z.real, z.imag]
    coeff, *_ = np.linalg.lstsq(real_design, real_target, rcond=None)
    # PLS real poles are represented with omega=0 and the real residue in A1.
    # The final high-frequency pole encodes the fitted constant asymptote.
    terms = [(float(a), 0.0, float(c), 0.0)
             for a, c in zip(alpha_hz, coeff[:len(alpha_hz)])]
    terms.append((1e25, 0.0, float(coeff[-2]), 0.0))
    with path.open("w", encoding="ascii", newline="\n") as fh:
        fh.write("! KiPI AC fitted impedance, rail {}, probe {}\n".format(result.rail, probe))
        fh.write("Z 1\nR0: 50\n")
        if coeff[-1] != 0:
            fh.write(f"asymp: {coeff[-1]:.12e}\n")
        fh.write(f"{len(terms)}\n")
        for alpha, omega, a1, a2 in terms:
            fh.write(f"{alpha:.12e} {omega:.12e} {a1:.12e} {a2:.12e}\n")
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
