"""Where the DC setup's parts sit on the board, for the 3D view's "PI Models" markers (#84):
a point on each pad of a VRM, load or series link, with the part's name beside them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from kipi.core.model.board import BoardSnapshot, Pad

KINDS = ("vrm", "load", "link")
LIFT_MM = 0.05          # markers sit this far off the copper, so they are never buried


@dataclass
class PartMarker:
    """One setup part: its pads' points, (x, y_kicad, z) mm, by role."""
    kind: str                                   # "vrm", "load" or "link"
    name: str                                   # e.g. "VRM 3V3"
    power: np.ndarray                           # rail pins (N, 3): drawn larger
    ground: np.ndarray                          # ground pins and ground sense (M, 3)

    @property
    def anchor(self) -> np.ndarray:
        """Where the name goes: above the middle of the part's pins."""
        pts = np.vstack([self.power, self.ground])
        return np.array([*pts[:, :2].mean(axis=0), pts[:, 2].max()])


def _pad_z(snap: BoardSnapshot, pad: Pad, side: str) -> float:
    """The outer face of the pad's copper: top for a part on top, bottom for one under
    the board; an inner-layer-only pad sits on its first layer."""
    cu = {c.id: c for c in snap.stackup.copper}
    layers = [cu[i] for i in pad.layers if i in cu] or list(snap.stackup.copper[:1])
    if not layers:
        return 0.0
    if side == "bottom":
        return min(c.z_bottom_mm for c in layers) - LIFT_MM
    return max(c.z_top_mm for c in layers) + LIFT_MM


def _points(snap: BoardSnapshot, refs: Iterable, nets, sides: Dict[str, str]) -> np.ndarray:
    pts: List[Tuple[float, float, float]] = []
    seen = set()
    for r in refs:
        if r is None or not r.ref or r.net not in nets:
            continue
        wanted = set(r.pins) if r.pins else None
        for p in snap.items_on_net(r.net)[2]:
            if p.footprint != r.ref or (wanted is not None and p.number not in wanted):
                continue
            if p.id in seen:
                continue
            seen.add(p.id)
            pts.append((p.position[0], p.position[1],
                        _pad_z(snap, p, sides.get(r.ref, "top"))))
    return np.array(pts, dtype=float).reshape(-1, 3)


def pi_markers(snap: Optional[BoardSnapshot], setup, nets: Iterable[str]) -> List[PartMarker]:
    """Markers of the setup's VRMs, loads and series links, on the pads of ``nets`` only
    (so none float over copper that is not drawn)."""
    if snap is None or setup is None:
        return []
    nets = set(nets)
    sides = {f.reference: f.side for f in snap.footprints}
    parts = []
    for v in setup.vrms:
        phases = v.all_phases()
        parts.append(("vrm", f"VRM {v.name}",
                      [v.sense, *(r for ph in phases for r in (ph.output, ph.input))],
                      [v.ground_sense, *(ph.ground for ph in phases)]))
    for ld in setup.loads:
        parts.append(("load", f"Load {ld.name}", [ld.supply], [ld.ground]))
    for k in setup.links:
        parts.append(("link", f"Link {k.name}", [k.side_a, k.side_b], []))
    out = []
    for kind, name, power, ground in parts:
        m = PartMarker(kind, name, _points(snap, power, nets, sides),
                       _points(snap, ground, nets, sides))
        if len(m.power) or len(m.ground):
            out.append(m)
    return out
