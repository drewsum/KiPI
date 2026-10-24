"""What the AC board map shows (#67): the board outline, the solver's mesh of the rail and its
ground, the probes, and the capacitors in the model (by model) and those left out.

:func:`build_map` gathers it from the board while the solve runs, so the map needs no board
to draw and is saved with the rail's results. The GUI's Board map tab and the HTML report
draw the same things.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from kipi.ac.model import caps_dict
from kipi.core.model.board import BoardSnapshot


def si(x: float, unit: str) -> str:
    """``x`` with an SI prefix, 4 significant digits: 1e-7, "F" -> "100 nF"."""
    if not x or not math.isfinite(x):
        return ""
    e = max(-15, min(9, 3 * math.floor(math.log10(abs(x)) / 3)))
    prefix = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[e]
    return f"{x / 10 ** e:.4g} {prefix}{unit}"


@dataclass
class MapMesh:
    """The AC solve's mesh, flattened to 2D: each triangle's layer, and whether it is
    ground copper (else the rail's)."""
    points: np.ndarray                 # (N, 2) mm, KiCad coordinates (y down)
    tris: np.ndarray                   # (M, 3) node ids
    tri_layer: np.ndarray              # (M,) copper layer id
    tri_ground: np.ndarray             # (M,) bool

    @classmethod
    def from_mesh(cls, mesh, node_ground: np.ndarray) -> "MapMesh":
        tris = np.asarray(mesh.tris, dtype=np.int32)
        return cls(points=np.asarray(mesh.points, dtype=np.float32), tris=tris,
                   tri_layer=np.asarray(mesh.tri_layer, dtype=np.int16),
                   tri_ground=np.asarray(node_ground, dtype=bool)[tris[:, 0]])


@dataclass
class MapPart:
    ref: str
    x: float
    y: float
    text: str                          # what hovering shows


@dataclass
class ACMap:
    rail: str
    rail_nets: List[str]
    ground: str
    outline: List[np.ndarray] = field(default_factory=list)   # closed rings, mm
    layers: List[Tuple[int, str]] = field(default_factory=list)  # copper (id, name), top first
    models: Dict[str, List[MapPart]] = field(default_factory=dict)  # model name -> its caps
    missing: List[MapPart] = field(default_factory=list)      # caps with no model, left out
    probes: List[MapPart] = field(default_factory=list)
    mesh: Optional[MapMesh] = None     # None until the solve has meshed

    def layer_name(self, layer_id: int) -> str:
        return dict(self.layers).get(layer_id, str(layer_id))

    def mesh_layers(self) -> List[Tuple[int, str]]:
        """The copper layers the mesh has triangles on, top first."""
        if self.mesh is None:
            return []
        used = set(np.unique(self.mesh.tri_layer).tolist())
        return [(i, n) for i, n in self.layers if i in used]

    def extent(self) -> Optional[Tuple[float, float, float, float]]:
        """(x0, x1, y0, y1) around the outline and every part, or None if there is none."""
        pts = [r for r in self.outline if len(r)]
        parts = [p for refs in self.models.values() for p in refs] + self.missing + self.probes
        if parts:
            pts.append(np.array([(p.x, p.y) for p in parts]))
        if self.mesh is not None and len(self.mesh.points):
            pts.append(self.mesh.points)
        if not pts:
            return None
        allp = np.vstack(pts)
        (x0, y0), (x1, y1) = allp.min(axis=0), allp.max(axis=0)
        return float(x0), float(x1), float(y0), float(y1)


def build_map(snap: BoardSnapshot, setup, rail: str, rail_nets, ground: str,
              caps: List[str], missing: List[str], probe_names: List[str]) -> ACMap:
    """The map of a rail's solve, before its mesh: ``caps`` are the references in the model,
    ``missing`` those on the rail with no model, ``probe_names`` the probes solved."""
    fp = {f.reference: f for f in snap.footprints}
    outline = [np.asarray(list(p.outline) + list(p.outline[:1]), dtype=float)
               for p in (snap.outline or []) if p.outline]
    model_of = caps_dict(setup.caps)
    models: Dict[str, List[MapPart]] = {}
    for ref in caps:
        f = fp.get(ref)
        m = model_of.get(ref)
        if f is None:
            continue
        name = m.name if m else "?"
        desc = (f"{si(m.capacitance_f, 'F')}, {si(m.esl_h, 'H')}, {si(m.esr_ohm, 'Ω')}"
                if m else "")
        models.setdefault(name, []).append(MapPart(
            ref, f.position[0], f.position[1],
            f"{ref} · {f.value} · {f.side}\nmodel {name}: {desc}"))
    left_out = [MapPart(r, fp[r].position[0], fp[r].position[1],
                        f"{r} · {fp[r].value}: no model, left out")
                for r in missing if r in fp]
    names = set(probe_names)
    probes = [MapPart(p.name, fp[p.rail.ref].position[0], fp[p.rail.ref].position[1],
                      f"probe {p.name} on the die of {p.rail.ref}" if p.on_die
                      else f"probe {p.name} at {p.rail.ref}")
              for p in setup.probes if p.name in names and p.rail.ref in fp]
    return ACMap(rail=rail, rail_nets=sorted(rail_nets), ground=ground, outline=outline,
                 layers=[(cu.id, cu.name) for cu in snap.stackup.copper],
                 models=dict(sorted(models.items())), missing=left_out, probes=probes)
