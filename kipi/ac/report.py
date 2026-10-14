"""AC results to files."""

from __future__ import annotations

import csv
from pathlib import Path


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
