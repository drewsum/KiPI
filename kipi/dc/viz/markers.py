"""Where the setup's parts sit on the board, for the 3D view's "PI Models" markers (#84):
a point on each pad of a VRM, load or series link, with the part's name beside them.

This is the DC mode's set. AC mode marks the parts an impedance sweep uses
(:func:`kipi.ac.viz.markers.ac_markers`), built from the same pieces: the pin refs of a
part become :func:`pad_points`, and :func:`markers_from` turns a mode's list of parts into
markers. Modelled capacitors (:func:`cap_markers`) are AC mode's only: a DC solve has no
use for them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from kipi.core.model.board import BoardSnapshot, Pad
from kipi.core.model.nets import is_ground_name
from kipi.dc.pi.diagram import _ground_nets

KINDS = ("vrm", "load", "link", "cap", "probe", "victim")
LIFT_MM = 0.05          # markers sit this far off the copper, so they are never buried

# One part to mark: (kind, the setup item's name, the name shown, the pin refs on the rail
# side, the pin refs on the ground side). Each mode builds its own list of these.
Part = Tuple[str, str, str, List, List]


@dataclass
class PartMarker:
    """One setup part: its pads' points, (x, y_kicad, z) mm, by role."""
    kind: str                                   # one of KINDS
    name: str                                   # e.g. "VRM 3V3", or "C12" for a capacitor
    power: np.ndarray                           # rail pins (N, 3): drawn larger
    ground: np.ndarray                          # ground pins and ground sense (M, 3)
    item: str = ""                              # the setup item's name (#96), e.g. "3V3"
    models: Tuple[str, ...] = ()                # a capacitor's: the models naming it

    def is_item(self, kind: str, name: str) -> bool:
        """Whether this marks the setup item ``name`` of marker kind ``kind``: a capacitor
        model names several parts, each marked on its own."""
        if kind != self.kind:
            return False
        return name in self.models if self.kind == "cap" else name == self.item

    @property
    def setup_item(self) -> str:
        """The setup item to select for this part: a capacitor's (first) model."""
        return self.models[0] if self.kind == "cap" and self.models else self.item

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


def part_sides(snap: BoardSnapshot) -> Dict[str, str]:
    """Which side of the board each part sits on, for :func:`pad_points`."""
    return {f.reference: f.side for f in snap.footprints}


def pad_points(snap: BoardSnapshot, refs: Iterable, nets,
               sides: Dict[str, str]) -> np.ndarray:
    """The points of the pads the pin refs ``refs`` name, (x, y_kicad, z) mm: only their
    pins on ``nets``, each pad once."""
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


def cap_points(snap: BoardSnapshot, ref: str, nets, grounds,
               sides: Dict[str, str]) -> Tuple[np.ndarray, np.ndarray]:
    """A capacitor's pads on ``nets``, (rail side, ground side): a capacitor model names
    parts, not pins, so its pads are split by net, ground being a ground in the setup or a
    net named like one."""
    power: List[Tuple[float, float, float]] = []
    ground: List[Tuple[float, float, float]] = []
    for p in snap.pads:
        if p.footprint != ref or p.net not in nets:
            continue
        (ground if p.net in grounds or is_ground_name(p.net) else power).append(
            (p.position[0], p.position[1], _pad_z(snap, p, sides.get(ref, "top"))))
    return (np.array(power, dtype=float).reshape(-1, 3),
            np.array(ground, dtype=float).reshape(-1, 3))


def vrm_part(vrm) -> Part:
    """A VRM's marker spec: every phase's output and input pins and the sense pin on the
    rail side, the phases' and the sense's ground pins (and an isolated converter's input
    ground pins) on the other."""
    phases, points = vrm.all_phases(), vrm.all_sense_points()
    return ("vrm", vrm.name, f"VRM {vrm.name}",
            [*(sp.sense for sp in points), *(r for ph in phases for r in (ph.output, ph.input))],
            [*(sp.ground_sense for sp in points), *(ph.ground for ph in phases),
             vrm.input_ground])


def link_part(link) -> Part:
    """A series link's marker spec: all of its sides are rail pins."""
    return ("link", link.name, f"Link {link.name}", list(link.sides), [])


def cap_markers(snap: BoardSnapshot, setup, nets, sides: Dict[str, str]) -> List[PartMarker]:
    """Markers of the capacitors that have a model, which an impedance sweep solves."""
    grounds = _ground_nets(setup)
    out = []
    models: Dict[str, List[str]] = {}
    for c in setup.caps:
        for r in c.refs:
            if r and c.name not in models.setdefault(r, []):
                models[r].append(c.name)
    for ref in sorted(models):
        m = PartMarker("cap", ref, *cap_points(snap, ref, nets, grounds, sides), ref,
                       tuple(models[ref]))
        if len(m.power) or len(m.ground):
            out.append(m)
    return out


def markers_from(snap: BoardSnapshot, setup, nets: Iterable[str],
                 parts: Iterable[Part], caps: bool = False) -> List[PartMarker]:
    """One mode's ``parts`` as markers, on the pads of ``nets`` only (so none float over
    copper that is not drawn), with the modelled capacitors after them if ``caps``. A part
    with no pad on those nets is left out."""
    nets = set(nets)
    sides = part_sides(snap)
    out = []
    for kind, item, name, power, ground in parts:
        m = PartMarker(kind, name, pad_points(snap, power, nets, sides),
                       pad_points(snap, ground, nets, sides), item)
        if len(m.power) or len(m.ground):
            out.append(m)
    return out + (cap_markers(snap, setup, nets, sides) if caps else [])


def pi_markers(snap: Optional[BoardSnapshot], setup, nets: Iterable[str]) -> List[PartMarker]:
    """DC mode's markers: the setup's VRMs, loads and series links."""
    if snap is None or setup is None:
        return []
    parts = [vrm_part(v) for v in setup.vrms]
    parts += [("load", ld.name, f"Load {ld.name}", [ld.supply], [ld.ground])
              for ld in setup.loads]
    parts += [link_part(k) for k in setup.links]
    return markers_from(snap, setup, nets, parts)
