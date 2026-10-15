"""Save / load :class:`BoardSnapshot` as (optionally gzipped) JSON.

Snapshots let the GUI, solver and tests run without a live KiCad.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
from pathlib import Path
from typing import Any, Dict

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Footprint, Net, Pad, Poly, Stackup,
    Track, Via, Zone,
)

FORMAT_VERSION = 1


def _round(obj: Any) -> Any:
    """Round floats to 1 nm so files stay small and diffs stay stable."""
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, (list, tuple)):
        return [_round(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    return obj


def to_dict(snap: BoardSnapshot) -> Dict[str, Any]:
    d = _round(dataclasses.asdict(snap))
    # JSON object keys must be strings; layer-keyed dicts are converted back on load.
    for pad in d["pads"]:
        pad["shapes"] = {str(k): v for k, v in pad["shapes"].items()}
    for zone in d["zones"]:
        zone["filled"] = {str(k): v for k, v in zone["filled"].items()}
    d["format_version"] = FORMAT_VERSION
    return d


def _poly(d) -> Poly:
    return Poly(outline=[tuple(p) for p in d["outline"]],
                holes=[[tuple(p) for p in h] for h in d["holes"]])


def _layer_polys(d) -> Dict[int, list]:
    return {int(k): [_poly(p) for p in v] for k, v in d.items()}


def from_dict(d: Dict[str, Any]) -> BoardSnapshot:
    version = d.get("format_version", 1)
    if version > FORMAT_VERSION:
        raise ValueError(f"Snapshot format {version} is newer than supported ({FORMAT_VERSION})")
    st = d["stackup"]
    stackup = Stackup(
        copper=[CopperLayer(**c) for c in st["copper"]],
        dielectrics=[DielectricLayer(**x) for x in st["dielectrics"]],
        board_thickness_mm=st["board_thickness_mm"],
        from_board=st.get("from_board", True))
    return BoardSnapshot(
        board_name=d["board_name"],
        project_dir=d.get("project_dir", ""),
        stackup=stackup,
        nets=[Net(**n) for n in d["nets"]],
        tracks=[Track(net=t["net"], layer=t["layer"], width_mm=t["width_mm"],
                      start=tuple(t["start"]), end=tuple(t["end"]),
                      mid=tuple(t["mid"]) if t.get("mid") else None) for t in d["tracks"]],
        vias=[Via(id=v["id"], net=v["net"], position=tuple(v["position"]),
                  drill_mm=v["drill_mm"], diameter_mm=v["diameter_mm"],
                  layers=list(v["layers"]), via_type=v.get("via_type", "through"))
              for v in d["vias"]],
        pads=[Pad(id=p["id"], net=p["net"], footprint=p["footprint"], number=p["number"],
                  position=tuple(p["position"]), pad_type=p["pad_type"],
                  drill_mm=tuple(p["drill_mm"]), shapes=_layer_polys(p["shapes"]),
                  angle_deg=p.get("angle_deg"))
              for p in d["pads"]],
        footprints=[Footprint(id=f["id"], reference=f["reference"], value=f["value"],
                              position=tuple(f["position"]), side=f["side"],
                              pad_ids=list(f["pad_ids"]), footprint=f.get("footprint", ""))
                    for f in d["footprints"]],
        zones=[Zone(id=z["id"], net=z["net"], name=z["name"], priority=z["priority"],
                    filled=_layer_polys(z["filled"])) for z in d["zones"]],
        outline=[_poly(p) for p in d["outline"]],
        source=d.get("source", "snapshot"),
        kicad_version=d.get("kicad_version", ""),
    )


def save(snap: BoardSnapshot, path) -> Path:
    path = Path(path)
    data = json.dumps(to_dict(snap), separators=(",", ":"))
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def load(path) -> BoardSnapshot:
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
    else:
        d = json.loads(path.read_text(encoding="utf-8"))
    snap = from_dict(d)
    snap.source = str(path)
    return snap
