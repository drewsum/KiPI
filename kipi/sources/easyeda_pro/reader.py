"""Read an EasyEDA Pro ``.epro2`` project into a :class:`BoardSnapshot`."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.model.board import (
    BoardSnapshot, Footprint, Net, Pad, Poly, Stackup, Track, Via, Zone,
)
from kipi.core.model.layers import canonical_name, copper_id
from kipi.core.model.stackup import LayerSpec, build_stackup, coating_of
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.easyeda_pro.archive import EasyedaError, open_project
from kipi.sources.easyeda_pro.documents import Document, parse_stream, pick_pcb
from kipi.sources.easyeda_pro.primitives import (
    MAX_ERROR_MM, Units, _as_list, choose_units, flatten_path, hole_size,
    pad_rings_in_footprint, pad_shape_for_layer, path_rings, place_in_component,
    poly_from_rings, polys_from_parallel, to_point, to_ring,
)

_COPPER_TYPES = ("TOP", "BOTTOM", "SIGNAL", "PLANE")
_TECH_TYPES = ("TOP_MASK", "BOT_MASK", "TOP_SILK", "BOT_SILK", "TOP_PASTE",
               "BOT_PASTE", "OUTLINE", "MULTI")


# ---------------------------------------------------------------------------
# Small record helpers
# ---------------------------------------------------------------------------

def _array_id(record: dict, index: int = 1) -> str:
    value = record.get("id")
    if isinstance(value, str) and value[:1] == "[":
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list) and len(parsed) > index:
                return str(parsed[index])
        except (ValueError, RecursionError):
            pass
    return ""


def _record_id(record: dict) -> str:
    value = record.get("id")
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _layer_number(record: dict) -> Optional[int]:
    value = record.get("layerId")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    raw = record.get("id")
    if isinstance(raw, str) and raw[:1] == "[":
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) > 1:
                return int(parsed[1])
        except (ValueError, TypeError, RecursionError):
            return None
    return None


def _phys_layer_number(record: dict) -> Optional[int]:
    raw = record.get("id")
    if isinstance(raw, str) and raw[:1] == "[":
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) > 1:
                return int(parsed[1])
        except (ValueError, TypeError, RecursionError):
            return None
    value = record.get("layerId")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _net(record: dict) -> str:
    return str(record.get("netName") or "")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _note(assumed: List[str], text: str) -> None:
    if text not in assumed:
        assumed.append(text)


def _plural(count: int, word: str) -> str:
    return word if count == 1 else word + "s"


# ---------------------------------------------------------------------------
# Layers and stackup
# ---------------------------------------------------------------------------

class LayerTable:
    def __init__(self, pcb: Document, assumed: List[str]):
        self.info: Dict[int, dict] = {}
        for record in pcb.of_type("LAYER"):
            layer_id = _layer_number(record)
            if layer_id is not None:
                self.info[layer_id] = record

        phys = list(pcb.of_type("LAYER_PHYS"))
        phys.sort(key=lambda r: _number(r.get("zIndex"), 0.0))
        self.phys = [(pid, record) for pid, record in
                     ((_phys_layer_number(r), r) for r in phys) if pid is not None]

        declared = [lid for lid, rec in self.info.items()
                    if str(rec.get("layerType") or "").upper() in _COPPER_TYPES
                    and rec.get("use", True) is not False]
        if not declared:
            # Some exports omit LAYER rows; the physical stack still names the copper.
            declared = [lid for lid, _ in self.phys
                        if lid in (1, 2) or 15 <= lid <= 46]
        physical_copper = [lid for lid, _ in self.phys if lid in declared]
        if len(physical_copper) >= 2:
            ordered = physical_copper
        else:
            ordered = sorted(declared,
                             key=lambda lid: (0 if str(self.info[lid].get("layerType")) == "TOP"
                                              else 2 if str(self.info[lid].get("layerType")) == "BOTTOM"
                                              else 1, lid))
        # Remove duplicates while preserving order.
        seen = set()
        ordered = [lid for lid in ordered if not (lid in seen or seen.add(lid))]
        if not ordered:
            raise EasyedaError(
                "The EasyEDA Pro board has no copper layers, so there is no board to analyze.")
        if len(ordered) > 32:
            raise EasyedaError(
                f"The EasyEDA Pro board has {len(ordered)} copper layers; KiPI supports at "
                "most 30 inner layers.")
        self.cu_ids: Dict[int, int] = {
            layer_id: copper_id(index, len(ordered))
            for index, layer_id in enumerate(ordered)
        }
        self.multi_id = next((lid for lid, rec in self.info.items()
                              if str(rec.get("layerType") or "").upper() == "MULTI"), 12)
        self._assumed = assumed

    def kipi(self, easyeda_id: Optional[int]) -> Optional[int]:
        return self.cu_ids.get(easyeda_id) if easyeda_id is not None else None

    def all_copper(self) -> List[int]:
        return list(self.cu_ids.values())

    def name(self, easyeda_id: int) -> str:
        return str((self.info.get(easyeda_id) or {}).get("layerName") or easyeda_id)

    def build_stackup(self, progress: ProgressReporter, units: Units,
                      assumed: List[str]) -> Stackup:
        scale = self._thickness_scale(units, assumed)
        entries: List[LayerSpec] = []
        coatings = []
        for layer_id, record in self.phys:
            thickness = _number(record.get("thickness"), 0.0) * scale
            material = str(record.get("material") or "")
            er = _number(record.get("permittivity"), 0.0)
            if layer_id in self.cu_ids:
                entries.append(("cu", self.cu_ids[layer_id], thickness, "", 0.0))
                continue
            layer_type = str((self.info.get(layer_id) or {}).get("layerType") or "").upper()
            layer_name = self.name(layer_id)
            if layer_type in ("SUBSTRATE", "DIELECTRIC") or layer_id >= 361:
                entries.append(("diel", -1, thickness, material, er))
                continue
            coating = coating_of(layer_name, layer_type, thickness, material,
                                 str((self.info.get(layer_id) or {}).get("activeColor") or ""))
            if coating is not None:
                coatings.append(coating)
        if not entries:
            progress.warn("EasyEDA Pro stackup is missing; using KiPI's default stackup")
        if not any(entry[0] == "diel" for entry in entries):
            _note(assumed, "Stackup: the project has no dielectric layers; KiPI uses its "
                           "default stackup.")
            entries = []
        elif sum(entry[2] for entry in entries) <= 0:
            _note(assumed, "Stackup: the project's layer thicknesses are all zero; KiPI "
                           "uses its default stackup.")
            entries = []
        elif not any(entry[0] == "diel" and entry[4] for entry in entries):
            _note(assumed, "Stackup: the project gives no dielectric constants, so AC uses "
                           "its default εr.")
        cu_ids = [self.cu_ids[lid] for lid in self.cu_ids]
        names = {lid: canonical_name(lid) for lid in cu_ids}
        return build_stackup(entries, cu_ids, names, progress, assumed, coatings)

    def _thickness_scale(self, units: Units, assumed: List[str]) -> float:
        raw = [_number(r.get("thickness"), 0.0) for _, r in self.phys]
        total = sum(raw)
        if total <= 0:
            return units.thickness_mm
        mil_total = total * units.thickness_mm
        mm_total = total * 0.1
        plausible = lambda value: 0.2 <= value <= 10.0
        copper = [_number(r.get("thickness"), 0.0) * units.thickness_mm
                  for lid, r in self.phys if lid in self.cu_ids]
        if plausible(mil_total) and all(value <= 0.5 for value in copper):
            return units.thickness_mm
        if plausible(mm_total):
            _note(assumed, "Stackup: layer thicknesses are read as 0.1 mm units.")
            return 0.1
        return units.thickness_mm


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def read_board(path, progress: ProgressReporter = NULL) -> BoardSnapshot:
    """Read the EasyEDA Pro project at ``path`` into a :class:`BoardSnapshot`."""
    assumed: List[str] = []
    with progress.timed("Reading the EasyEDA Pro project"):
        files = open_project(path)
        progress.check_cancel()

    with progress.timed("Reading the EasyEDA Pro document stream"):
        documents, errors = parse_stream(files.stream_text)
        progress.check_cancel()
    if errors:
        word = _plural(len(errors), "line")
        progress.warn(f"{len(errors)} EasyEDA Pro {word} could not be read; the first is "
                      f"{errors[0]}")
        _note(assumed, f"EasyEDA Pro: {len(errors)} malformed {word} "
                       f"{'was' if len(errors) == 1 else 'were'} skipped.")

    pcb = pick_pcb(documents, progress, assumed)
    footprints = {doc.uuid: doc for doc in documents
                  if doc.doc_type == "FOOTPRINT" and not doc.deleted}
    units = choose_units(files.project, pcb, footprints.values())
    _note(assumed, units.note)
    max_error = MAX_ERROR_MM / max(units.mm, 1e-9)

    standards = LayerTable(pcb, assumed)
    with progress.timed("Reading EasyEDA Pro footprints and pads"):
        pads, footprints_out = _read_footprints(
            pcb, footprints, standards, units, max_error, progress, assumed)
        progress.check_cancel()

    with progress.timed("Reading EasyEDA Pro copper"):
        tracks, arcs = _read_tracks(pcb, standards, units, max_error, progress)
        vias = _read_vias(pcb, standards, units, assumed, progress)
        zones = _read_zones(pcb, standards, units, max_error, progress, assumed)
        free_pads = _read_free_pads(pcb, standards, units, max_error)
        pads.extend(free_pads)
        _assign_netless(zones, tracks, vias, pads, assumed, progress)
        progress.check_cancel()

    with progress.timed("Reading EasyEDA Pro stackup and outline"):
        stackup = standards.build_stackup(progress, units, assumed)
        outline = _read_outline(pcb, standards, units, max_error, progress, assumed)
        progress.check_cancel()

    nets = _build_nets(pcb, tracks, vias, pads, zones)
    return BoardSnapshot(
        board_name=Path(files.path).stem,
        project_dir=str(Path(files.path).parent),
        stackup=stackup,
        nets=nets,
        tracks=tracks + arcs,
        vias=vias,
        pads=pads,
        footprints=footprints_out,
        zones=zones,
        outline=outline,
        source="easyeda pro file",
        source_format="easyeda-epro2",
        assumed=assumed,
    )


# ---------------------------------------------------------------------------
# Nets, pads, footprints
# ---------------------------------------------------------------------------

def _pad_net_map(pcb: Document) -> Dict[Tuple[str, str], str]:
    result: Dict[Tuple[str, str], str] = {}
    for record in pcb.of_type("PAD_NET"):
        value = record.get("padNet")
        if value is None:
            continue
        raw = record.get("id")
        if not isinstance(raw, str) or raw[:1] != "[":
            continue
        try:
            parts = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(parts, list) or len(parts) < 3:
            continue
        component, pad_num = str(parts[1]), str(parts[2])
        pad_id = str(parts[3]) if len(parts) > 3 else ""
        key = str(value)
        if pad_id:
            result[(component, pad_id)] = key
        result.setdefault((component, pad_num), key)
    return result


def _component_attrs(pcb: Document) -> Dict[str, Dict[str, str]]:
    attrs: Dict[str, Dict[str, str]] = defaultdict(dict)
    for record in pcb.of_type("ATTR"):
        parent = str(record.get("parentId") or "")
        key = str(record.get("key") or "")
        if parent and key:
            attrs[parent][key] = str(record.get("value") or "")
    return attrs


def _footprint_name(doc: Optional[Document], component: dict) -> str:
    if doc is not None:
        for record in doc.of_type("ATTR"):
            if str(record.get("key") or "") == "Footprint" and not record.get("parentId"):
                value = str(record.get("value") or "")
                if value:
                    return value
    attrs = component.get("attrs") or {}
    return str(attrs.get("Origin Footprint") or "")


def _read_footprints(pcb: Document, docs: Dict[str, Document], layers: LayerTable,
                     units: Units, max_error: float, progress: ProgressReporter,
                     assumed: List[str]) -> Tuple[List[Pad], List[Footprint]]:
    attrs = _component_attrs(pcb)
    net_by_pad = _pad_net_map(pcb)
    pads: List[Pad] = []
    footprints: List[Footprint] = []

    for record in pcb.of_type("COMPONENT"):
        component_id = _record_id(record)
        values = attrs.get(component_id, {})
        reference = values.get("Designator") or values.get("Reference") or ""
        footprint_uuid = values.get("Footprint") or ""
        doc = docs.get(str(footprint_uuid))
        side = "bottom" if int(_number(record.get("layerId"), 1)) == 2 else "top"
        value = (values.get("Value") or values.get("Name")
                 or str((record.get("attrs") or {}).get("Name") or ""))
        position = to_point(_number(record.get("x", record.get("positionX"))),
                            _number(record.get("y", record.get("positionY"))), units)
        angle = _number(record.get("angle"), 0.0)
        lib_name = _footprint_name(doc, record)
        pad_ids: List[str] = []

        if doc is None:
            _note(assumed, f"EasyEDA Pro: footprint '{footprint_uuid}' was not found for "
                           f"{reference or component_id}; its pads are missing.")
            progress.warn(f"Footprint {footprint_uuid} for {reference or component_id} "
                          "is missing from the project")
        else:
            canvas = next(iter(doc.of_type("CANVAS")), {})
            origin = (_number(canvas.get("originX")), _number(canvas.get("originY")))
            for pad_record in doc.of_type("PAD"):
                pad = _read_footprint_pad(
                    pad_record, record, component_id, reference, side, layers, units,
                    max_error, net_by_pad, origin)
                if pad is not None:
                    pads.append(pad)
                    pad_ids.append(pad.id)

        footprints.append(Footprint(
            id=component_id, reference=reference, value=value, position=position,
            side=side, pad_ids=pad_ids, footprint=lib_name, angle_deg=angle))

    return pads, footprints


def _read_footprint_pad(pad: dict, component: dict, component_id: str, reference: str,
                        side: str, layers: LayerTable, units: Units, max_error: float,
                        net_by_pad: Dict[Tuple[str, str], str],
                        origin: Tuple[float, float] = (0.0, 0.0)) -> Optional[Pad]:
    raw_id = _record_id(pad)
    number = str(pad.get("num") or "")
    local_id = raw_id or number or "pad"
    net = (net_by_pad.get((component_id, local_id))
           or net_by_pad.get((component_id, number))
           or str(pad.get("netName") or ""))
    shape_layers: Dict[int, List[Poly]] = {}
    easyeda_layers = _pad_layers(pad, layers)
    if side == "bottom":
        # A footprint pad's local top/bottom is the board's bottom/top on a mirrored part.
        easyeda_layers = [{1: 2, 2: 1}.get(lid, lid) for lid in easyeda_layers]
    for easyeda_layer in easyeda_layers:
        kipi_layer = layers.kipi(easyeda_layer)
        if kipi_layer is None:
            continue
        rings, absolute = pad_shape_for_layer(pad, easyeda_layer, max_error)
        if not rings:
            continue
        rings = pad_rings_in_footprint(pad, rings, absolute)
        placed = place_in_component(rings, component, side, units, origin)
        # place_in_component() already returns KiPI millimetres; only reject degenerate
        # rings here (converting again would shrink the pad by the unit factor).
        polys = [Poly(outline=ring) for ring in placed if len(ring) >= 3]
        if polys:
            shape_layers[kipi_layer] = polys

    drill = _drill_mm(pad, units)
    pad_type = _pad_type(pad, drill)
    center = (pad.get("centerX"), pad.get("centerY"))
    if center[0] is None or center[1] is None:
        position = shape_layers and _first_point(shape_layers) or (0.0, 0.0)
    else:
        moved = place_in_component([[(float(center[0]), float(center[1]))]],
                                   component, side, units, origin)
        position = moved[0][0] if moved and moved[0] else (0.0, 0.0)
    return Pad(
        id=f"{component_id}{local_id}",
        net=net,
        footprint=reference,
        number=number,
        position=position,
        pad_type=pad_type,
        drill_mm=drill,
        shapes=shape_layers,
        angle_deg=None,
    )


def _pad_layers(pad: dict, layers: LayerTable) -> List[int]:
    layer_id = _layer_number(pad)
    if layer_id is None:
        return []
    if layer_id == layers.multi_id or layer_id == 12:
        return list(layers.cu_ids.keys())
    return [layer_id] if layer_id in layers.cu_ids else []


def _drill_mm(pad: dict, units: Units) -> Tuple[float, float]:
    width, height = hole_size(pad.get("hole"))
    if width <= 0 and height <= 0:
        return (0.0, 0.0)
    return (round(width * units.mm, 6), round(height * units.mm, 6))


def _pad_type(pad: dict, drill: Tuple[float, float]) -> str:
    if drill[0] <= 0 and drill[1] <= 0:
        return "smd"
    return "pth" if bool(pad.get("plated", True)) else "npth"


def _first_point(shapes: Dict[int, List[Poly]]) -> Tuple[float, float]:
    for polys in shapes.values():
        for poly in polys:
            if poly.outline:
                return poly.outline[0]
    return (0.0, 0.0)


def _read_free_pads(pcb: Document, layers: LayerTable, units: Units,
                    max_error: float) -> List[Pad]:
    pads: List[Pad] = []
    for record in pcb.of_type("PAD"):
        layer_ids = _pad_layers(record, layers)
        shapes: Dict[int, List[Poly]] = {}
        center = (_number(record.get("centerX")), _number(record.get("centerY")))
        for easyeda_layer in layer_ids:
            kipi_layer = layers.kipi(easyeda_layer)
            if kipi_layer is None:
                continue
            rings, absolute = pad_shape_for_layer(record, easyeda_layer, max_error)
            if not rings:
                continue
            rings = pad_rings_in_footprint(record, rings, absolute)
            converted = [Poly(outline=ring) for ring in
                         (to_ring(ring, units) for ring in rings) if len(ring) >= 3]
            if converted:
                shapes[kipi_layer] = converted
        drill = _drill_mm(record, units)
        pads.append(Pad(
            id=f"free:{_record_id(record)}", net=_net(record), footprint="",
            number=str(record.get("num") or ""),
            position=to_point(center[0], center[1], units),
            pad_type=_pad_type(record, drill), drill_mm=drill, shapes=shapes,
            angle_deg=None))
    return pads


# ---------------------------------------------------------------------------
# Tracks and vias
# ---------------------------------------------------------------------------

def _is_outline_poly(record: dict, layers: LayerTable) -> bool:
    if str(record.get("polyType") or "").upper() == "BOARD_OUTLINE":
        return True
    layer_id = _layer_number(record)
    return (layer_id is not None
            and str((layers.info.get(layer_id) or {}).get("layerType") or "").upper()
            == "OUTLINE")


def _read_tracks(pcb: Document, layers: LayerTable, units: Units, max_error: float,
                 progress: ProgressReporter) -> Tuple[List[Track], List[Track]]:
    tracks: List[Track] = []
    arcs: List[Track] = []
    for record in pcb.of_type("LINE"):
        easyeda_layer = _layer_number(record)
        kipi_layer = layers.kipi(easyeda_layer)
        if kipi_layer is None:
            continue
        if any(record.get(key) is None
               for key in ("startX", "startY", "endX", "endY")):
            continue
        start = to_point(_number(record.get("startX")), _number(record.get("startY")), units)
        end = to_point(_number(record.get("endX")), _number(record.get("endY")), units)
        tracks.append(Track(net=_net(record), layer=kipi_layer,
                            width_mm=_number(record.get("width")) * units.mm,
                            start=start, end=end, id=_record_id(record)))
    for record in list(pcb.of_type("ARC")) + list(pcb.of_type("CARC")):
        easyeda_layer = _layer_number(record)
        kipi_layer = layers.kipi(easyeda_layer)
        if kipi_layer is None:
            continue
        if any(record.get(key) is None
               for key in ("startX", "startY", "endX", "endY")):
            continue
        sx, sy = _number(record.get("startX")), _number(record.get("startY"))
        ex, ey = _number(record.get("endX")), _number(record.get("endY"))
        angle = _number(record.get("angle"))
        mid = _arc_mid(sx, sy, ex, ey, angle)
        start = to_point(sx, sy, units)
        end = to_point(ex, ey, units)
        arcs.append(Track(net=_net(record), layer=kipi_layer,
                          width_mm=_number(record.get("width")) * units.mm,
                          start=start, end=end,
                          mid=to_point(*mid, units) if mid else None,
                          id=_record_id(record)))
    # A POLY is a stroked centreline. An open path becomes track segments; closed paths,
    # circles and rectangles are copper areas and are read as zones.
    for record in pcb.of_type("POLY"):
        if _is_outline_poly(record, layers):
            continue
        kipi_layer = layers.kipi(_layer_number(record))
        if kipi_layer is None:
            continue
        path = record.get("path")
        if isinstance(path, str) or (isinstance(path, list) and path
                                     and isinstance(path[0], str)
                                     and path[0].upper() in ("R", "CIRCLE")):
            continue
        points = flatten_path(path, max_error)
        if len(points) < 2:
            continue
        closed = (len(points) > 2
                  and abs(points[0][0] - points[-1][0]) < 1e-6
                  and abs(points[0][1] - points[-1][1]) < 1e-6)
        if closed:
            continue
        width = _number(record.get("width")) * units.mm
        if width <= 0:
            continue
        net = _net(record)
        raw_id = _record_id(record)
        for index in range(len(points) - 1):
            tracks.append(Track(
                net=net, layer=kipi_layer, width_mm=width,
                start=to_point(*points[index], units),
                end=to_point(*points[index + 1], units),
                id=f"{raw_id}:{index}"))
    return tracks, arcs


def _arc_mid(sx: float, sy: float, ex: float, ey: float, angle: float):
    from kipi.sources.easyeda_pro.primitives import arc_mid
    return arc_mid(sx, sy, ex, ey, angle)


def _read_vias(pcb: Document, layers: LayerTable, units: Units, assumed: List[str],
               progress: ProgressReporter) -> List[Via]:
    vias: List[Via] = []
    blind = 0
    for record in pcb.of_type("VIA"):
        if record.get("centerX") is None or record.get("centerY") is None:
            continue
        via_type = str(record.get("viaType") or "NORMAL").upper()
        span = layers.all_copper()
        if via_type == "BLIND":
            blind += 1
        drill = _number(record.get("holeDiameter")) * units.mm
        diameter = _number(record.get("viaDiameter")) * units.mm
        hidden = {int(value) for value in record.get("unusedInnerLayers") or []
                  if isinstance(value, int) or str(value).isdigit()}
        pad_mm: Dict[int, float] = {}
        for easyeda_layer, kipi_layer in layers.cu_ids.items():
            if easyeda_layer not in hidden:
                pad_mm[kipi_layer] = diameter
        vias.append(Via(
            id=_record_id(record), net=_net(record),
            position=to_point(_number(record.get("centerX")),
                              _number(record.get("centerY")), units),
            drill_mm=drill, diameter_mm=diameter, layers=span,
            via_type="through", pad_mm=pad_mm))
    if blind:
        _note(assumed, f"EasyEDA Pro: {blind} blind/buried "
                       f"{_plural(blind, 'via')} "
                       f"{'is' if blind == 1 else 'are'} modeled as through vias; the rule "
                       "that gives their layer span is not read.")
    return vias


# ---------------------------------------------------------------------------
# Copper pours and graphics
# ---------------------------------------------------------------------------

def _read_zones(pcb: Document, layers: LayerTable, units: Units, max_error: float,
                progress: ProgressReporter, assumed: List[str]) -> List[Zone]:
    poured: Dict[str, List[List[Tuple[float, float]]]] = defaultdict(list)
    for record in pcb.of_type("POURED"):
        target = str(record.get("targetId") or _array_id(record, 1))
        for entry in _as_list(record.get("pourFill")) or []:
            if not isinstance(entry, dict):
                continue
            for ring in path_rings(entry.get("path"), max_error):
                poured[target].append(ring)

    zones: List[Zone] = []
    missing = 0
    for record in pcb.of_type("POUR"):
        easyeda_layer = _layer_number(record)
        kipi_layer = layers.kipi(easyeda_layer)
        if kipi_layer is None:
            continue
        net = _net(record)
        name = str(record.get("name") or "")
        target = _record_id(record)
        if poured.get(target):
            rings = poured[target]
            polys = polys_from_parallel(rings, Units(mm=units.poured_mm,
                                                     thickness_mm=units.thickness_mm))
            zones.append(Zone(id=target, net=net, name=name,
                              priority=int(_number(record.get("order"), 0)),
                              filled={kipi_layer: polys}))
        else:
            rings = path_rings(record.get("path"), max_error)
            polys = poly_from_rings(rings, units)
            missing += 1
            if polys:
                zones.append(Zone(id=target, net=net, name=name,
                                  priority=int(_number(record.get("order"), 0)),
                                  filled={kipi_layer: polys}))
    if missing:
        _note(assumed, f"EasyEDA Pro: {missing} copper "
                       f"{_plural(missing, 'pour')} "
                       f"{'has' if missing == 1 else 'have'} no POURED fill; "
                       "the outlined area is used.")

    for record in pcb.of_type("FILL"):
        easyeda_layer = _layer_number(record)
        kipi_layer = layers.kipi(easyeda_layer)
        if kipi_layer is None:
            continue
        rings = path_rings(record.get("path"), max_error)
        polys = polys_from_parallel(rings, units)
        if polys:
            zones.append(Zone(id=_record_id(record), net=_net(record), name="copper",
                              priority=0, filled={kipi_layer: polys}))

    # POLY copper areas: a rounded rectangle/circle or a closed path. Open polylines are
    # centreline strokes and were turned into tracks by _read_tracks().
    for record in pcb.of_type("POLY"):
        if _is_outline_poly(record, layers):
            continue
        kipi_layer = layers.kipi(_layer_number(record))
        if kipi_layer is None:
            continue
        path = record.get("path")
        if isinstance(path, str) or (isinstance(path, list) and path
                                     and isinstance(path[0], str)
                                     and path[0].upper() in ("R", "CIRCLE")):
            rings = path_rings(path, max_error)
            polys = poly_from_rings(rings, units)
            if polys:
                zones.append(Zone(id=_record_id(record), net=_net(record), name="copper",
                                  priority=0, filled={kipi_layer: polys}))
            continue
        points = flatten_path(path, max_error)
        if len(points) < 3:
            continue
        closed = (abs(points[0][0] - points[-1][0]) < 1e-6
                  and abs(points[0][1] - points[-1][1]) < 1e-6)
        if not closed:
            continue
        polys = poly_from_rings([points], units)
        if polys:
            zones.append(Zone(id=_record_id(record), net=_net(record), name="copper",
                              priority=0, filled={kipi_layer: polys}))
    return zones


def _read_outline(pcb: Document, layers: LayerTable, units: Units, max_error: float,
                  progress: ProgressReporter, assumed: List[str]) -> List[Poly]:
    outline: List[Poly] = []
    for record in pcb.of_type("POLY"):
        if not _is_outline_poly(record, layers):
            continue
        rings = path_rings(record.get("path"), max_error)
        polys = poly_from_rings(rings, units)
        outline.extend(polys)
    if not outline:
        _note(assumed, "EasyEDA Pro: the board has no board-outline polygon; KiPI uses "
                       "its copper-extent fallback.")
        progress.warn("No board outline found in the EasyEDA Pro project")
    return outline


# ---------------------------------------------------------------------------
# Net assignment and snapshot helpers
# ---------------------------------------------------------------------------

def _assign_netless(zones: List[Zone], tracks: List[Track], vias: List[Via],
                    pads: List[Pad], assumed: List[str],
                    progress: ProgressReporter) -> None:
    """Copper with an empty net takes the net of the copper it touches, if unique.

    EasyEDA Pro leaves the net out of many drawn tracks and pours. Pads, vias and zones
    that do carry a net seed the assignment; each pass gives touching pieces the one net
    they touch and makes them seeds for the next pass, so a track chain follows its pad.
    """
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree

    known: Dict[int, List[Tuple[Any, str]]] = defaultdict(list)
    for track in tracks:
        if track.net and track.start != track.end:
            known[track.layer].append(
                (LineString([track.start, track.end]).buffer(max(track.width_mm / 2, 1e-6)),
                 track.net))
    for via in vias:
        if via.net:
            for lid in via.layers:
                known[lid].append((Point(via.position).buffer(via.diameter_mm / 2), via.net))
    for pad in pads:
        if pad.net:
            for lid, polys in pad.shapes.items():
                known[lid] += [(poly_to_shapely(poly), pad.net) for poly in polys]
    for zone in zones:
        if zone.net:
            for lid, polys in zone.filled.items():
                known[lid] += [(poly_to_shapely(poly), zone.net) for poly in polys]

    loose_tracks = [track for track in tracks
                    if not track.net and track.start != track.end]
    loose_zones = [zone for zone in zones if not zone.net and zone.filled]
    guessed_tracks = guessed_zones = 0

    def nets_touching(geom, lid: int, trees) -> set:
        entry = trees.get(lid)
        if entry is None or geom is None:
            return set()
        tree, pieces = entry
        return {pieces[int(index)][1]
                for index in tree.query(geom, predicate="intersects")}

    for _ in range(6):
        # One spatial index per layer per pass; new pieces become seeds next pass.
        trees: Dict[int, Tuple[Any, List[Tuple[Any, str]]]] = {}
        for lid, pieces in known.items():
            valid = [(g, net) for g, net in pieces if g is not None and net]
            if valid:
                trees[lid] = (STRtree([g for g, _ in valid]), valid)

        changed = False
        remaining = []
        for track in loose_tracks:
            geom = LineString([track.start, track.end]).buffer(
                max(track.width_mm / 2, 1e-6))
            nets = nets_touching(geom, track.layer, trees)
            if len(nets) == 1:
                track.net = nets.pop()
                known[track.layer].append((geom, track.net))
                guessed_tracks += 1
                changed = True
            else:
                remaining.append(track)
        loose_tracks = remaining

        remaining_zones = []
        for zone in loose_zones:
            nets = set()
            geoms = []
            for lid, polys in zone.filled.items():
                for poly in polys:
                    geom = poly_to_shapely(poly)
                    if geom is None:
                        continue
                    geoms.append((geom, lid))
                    nets |= nets_touching(geom, lid, trees)
            if len(nets) == 1:
                zone.net = nets.pop()
                for geom, lid in geoms:
                    known[lid].append((geom, zone.net))
                guessed_zones += 1
                changed = True
            else:
                remaining_zones.append(zone)
        loose_zones = remaining_zones

        if not changed:
            break
        if not loose_tracks and not loose_zones:
            break

    if guessed_tracks or guessed_zones:
        parts = []
        if guessed_tracks:
            parts.append(f"{guessed_tracks} track segments")
        if guessed_zones:
            parts.append(f"{guessed_zones} copper pieces")
        _note(assumed, f"EasyEDA Pro: {' and '.join(parts)} have no net in the file and "
                       "take the net of the copper they touch.")
    if loose_tracks or loose_zones:
        progress.warn(f"{len(loose_tracks)} tracks and {len(loose_zones)} copper pieces "
                      "have no net and carry no current in the model")


def _build_nets(pcb: Document, tracks: Sequence[Track], vias: Sequence[Via],
                pads: Sequence[Pad], zones: Sequence[Zone]) -> List[Net]:
    names = {""}
    for record in pcb.of_type("NET"):
        name = _array_id(record, 1)
        if name:
            names.add(name)
    for item in list(tracks) + list(vias) + list(pads) + list(zones):
        if item.net:
            names.add(item.net)
    ordered = sorted(names)
    return [Net(code=index, name=name) for index, name in enumerate(ordered)]
