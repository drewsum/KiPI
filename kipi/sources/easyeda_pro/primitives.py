"""Geometry and units for EasyEDA Pro PCB records.

EasyEDA Pro stores PCB coordinates in a flat unit (mil in the verified 4.1.99 projects,
0.01 inch in older ones) with Y up. KiPI uses millimetres with Y down. Shapes are built in
EasyEDA's own frame and converted only at the end, which keeps rotations and arcs honest.
"""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from kipi.core.geometry.pad_shapes import circle_points, oval, rounded_rect
from kipi.core.model.board import Poly
from kipi.sources.kicad_pcb.items import Frame

Point = Tuple[float, float]

#: MiCAD's default maximum error; converted to the file's units for tessellation.
MAX_ERROR_MM = 0.005
CIRCLE_POINTS = 64
ARC_STEPS = 16


@dataclass(frozen=True)
class Units:
    """One EasyEDA coordinate unit in millimetres (main geometry and stackup thickness)."""
    mm: float
    thickness_mm: float
    note: str = ""
    poured_mm: float = 0.254      # POURED is always 0.01 inch


def version_major(text: Any) -> int:
    match = re.match(r"\s*(\d+)", str(text or ""))
    return int(match.group(1)) if match else 0


def _in_range(value: float, low: float, high: float) -> float:
    return 1.0 if low <= value <= high else 0.0


def choose_units(project: Dict[str, Any], pcb, footprints: Iterable[Any]) -> Units:
    """Pick the file unit from its version and from the size of real geometry.

    The format notes and the sibling importer disagree about whether a 4.x project stores
    mil or 0.01 inch. A via drill or a pad is unambiguous: 12 units is 0.305 mm as mil but
    3.05 mm as 0.01 inch. Score both candidates and fall back to the version default.
    """
    version = str(project.get("editorVersion") or pcb.version or "")
    major = version_major(version)
    default = 0.0254 if major in (0, 4) or major > 4 else 0.254

    holes: List[float] = []
    pads: List[Tuple[float, float]] = []
    outlines: List[Tuple[float, float]] = []
    for record in pcb.of_type("VIA"):
        try:
            holes.append(float(record.get("holeDiameter") or 0))
        except (TypeError, ValueError):
            pass
    for record in pcb.of_type("POLY"):
        if str(record.get("polyType") or "").upper() != "BOARD_OUTLINE":
            continue
        path = record.get("path")
        if isinstance(path, list) and len(path) >= 5 and path[0] == "R":
            try:
                outlines.append((float(path[3]), float(path[4])))
            except (TypeError, ValueError):
                pass
    for doc in footprints:
        for record in doc.of_type("PAD"):
            shape = record.get("defaultPad")
            if isinstance(shape, dict):
                try:
                    pads.append((float(shape.get("width") or 0),
                                 float(shape.get("height") or 0)))
                except (TypeError, ValueError):
                    pass
            elif isinstance(shape, list) and len(shape) >= 3:
                try:
                    pads.append((float(shape[1]), float(shape[2])))
                except (TypeError, ValueError):
                    pass

    def score(unit: float) -> float:
        total = 0.0
        count = 0
        for value in holes:
            total += _in_range(value * unit, 0.1, 1.5)
            count += 1
        for width, height in pads:
            total += _in_range(width * unit, 0.1, 10.0)
            total += _in_range(height * unit, 0.1, 10.0)
            count += 2
        for width, height in outlines:
            total += _in_range(width * unit, 10.0, 1000.0)
            total += _in_range(height * unit, 10.0, 1000.0)
            count += 2
        return total / count if count else 0.5

    mil, inch = score(0.0254), score(0.254)
    if abs(mil - inch) > 0.15:
        chosen = 0.0254 if mil > inch else 0.254
    else:
        chosen = default
    note = ""
    if chosen == 0.0254:
        note = "Units: EasyEDA Pro coordinates are read as mil (0.0254 mm)."
    else:
        note = "Units: EasyEDA Pro coordinates are read as 0.01 inch (0.254 mm)."
    # LAYER_PHYS thickness follows the format's own convention: mil in 4.x, 0.1 mm in
    # older projects. It is independent of the main geometry unit.
    thickness = 0.0254 if major in (0, 4) or major > 4 else 0.1
    return Units(mm=chosen, thickness_mm=thickness, note=note)


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------

def to_point(x: float, y: float, units: Units) -> Point:
    """EasyEDA Y-up coordinate to KiPI mm / Y-down."""
    return (round(float(x) * units.mm, 6), round(-float(y) * units.mm, 6))


def to_ring(points: Sequence[Point], units: Units) -> List[Point]:
    ring = [to_point(x, y, units) for x, y in points]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    return ring


def to_ms(value: Any, units: Units) -> float:
    try:
        return round(float(value) * units.mm, 6)
    except (TypeError, ValueError):
        return 0.0


def poly_from_rings(rings: Sequence[Sequence[Point]], units: Units) -> List[Poly]:
    """Rings of one POUR/POLY: the first is the outline and the rest are holes."""
    if not rings:
        return []
    converted = [to_ring(ring, units) for ring in rings]
    converted = [ring for ring in converted if len(ring) >= 3]
    if not converted:
        return []
    return [Poly(outline=converted[0], holes=converted[1:])]


def polys_from_parallel(rings: Sequence[Sequence[Point]], units: Units) -> List[Poly]:
    """Rings that are independent polygons (FILL / POURED), not outlines and holes."""
    return [Poly(outline=ring) for ring in
            (to_ring(points, units) for points in rings) if len(ring) >= 3]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> Any:
    """POURED payloads are sometimes stringified JSON or a Python literal."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except ValueError:
            try:
                return ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return []
    return value


def arc_mid(sx: float, sy: float, ex: float, ey: float,
            angle_deg: float) -> Optional[Point]:
    """The midpoint of an arc from start to end, turning ``angle_deg`` CCW."""
    dx, dy = ex - sx, ey - sy
    chord = math.hypot(dx, dy)
    a = math.radians(angle_deg)
    if chord < 1e-12 or abs(math.sin(a / 2.0)) < 1e-12 or abs(angle_deg) >= 360:
        return None
    radius = chord / (2.0 * math.sin(a / 2.0))
    mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
    px, py = -dy / chord, dx / chord
    h = radius * math.cos(a / 2.0)
    cx, cy = mx + px * h, my + py * h
    vx, vy = sx - cx, sy - cy
    half = a / 2.0
    cos_h, sin_h = math.cos(half), math.sin(half)
    return (cx + vx * cos_h - vy * sin_h, cy + vx * sin_h + vy * cos_h)


def _circle_center(start: Point, end: Point, angle_deg: float) -> Point:
    dx, dy = end[0] - start[0], end[1] - start[1]
    chord = math.hypot(dx, dy) or 1e-12
    a = math.radians(angle_deg)
    sin_half = math.sin(a / 2.0)
    radius = chord / (2.0 * sin_half) if abs(sin_half) > 1e-12 else 0.0
    px, py = -dy / chord, dx / chord
    h = radius * math.cos(a / 2.0)
    return ((start[0] + end[0]) / 2.0 + px * h,
            (start[1] + end[1]) / 2.0 + py * h)


def _quad_bezier(p0: Point, p1: Point, p2: Point, t: float) -> Point:
    u = 1.0 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


def _cubic_bezier(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def _arc_points(start: Point, end: Point, angle: float, steps: int) -> List[Point]:
    mid = arc_mid(start[0], start[1], end[0], end[1], angle)
    if mid is not None:
        return [_quad_bezier(start, mid, end, i / steps) for i in range(1, steps + 1)]
    cx, cy = _circle_center(start, end, angle)
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        out.append((cx + (start[0] - cx) * (1 - t) + (end[0] - cx) * t,
                    cy + (start[1] - cy) * (1 - t) + (end[1] - cy) * t))
    return out


def _rounded_rect_points(x: float, y: float, w: float, h: float,
                         radius: float, max_error: float) -> List[Point]:
    """A rectangle anchored at min-x/max-y, extending toward -y, corners rounded."""
    radius = max(0.0, min(radius, w / 2.0, h / 2.0))
    if radius <= 0:
        return [(x, y), (x + w, y), (x + w, y - h), (x, y - h)]
    # rounded_rect() is centred on the origin; this rectangle's centre is (x+w/2, y-h/2).
    local = rounded_rect(w, h, radius, 0.0, (), max_error)
    cx, cy = x + w / 2.0, y - h / 2.0
    return [(cx + px, cy + py) for px, py in local]


def _flatten_shape(spec: Sequence[Any], max_error: float) -> List[Point]:
    kind = str(spec[0]).upper()
    if kind == "CIRCLE" and len(spec) >= 4:
        cx, cy, r = float(spec[1]), float(spec[2]), float(spec[3])
        return [(cx + r * math.cos(2 * math.pi * i / CIRCLE_POINTS),
                 cy + r * math.sin(2 * math.pi * i / CIRCLE_POINTS))
                for i in range(CIRCLE_POINTS)]
    if kind == "R" and len(spec) >= 5:
        x, y, w, h = (float(spec[1]), float(spec[2]), float(spec[3]), float(spec[4]))
        rot = float(spec[5]) if len(spec) > 5 else 0.0
        # V4: 7 elements, the last is the corner radius. V3: 8, radius is element 7.
        if len(spec) >= 8:
            radius = float(spec[7])
        elif len(spec) == 7:
            radius = float(spec[6])
        else:
            radius = 0.0
        pts = _rounded_rect_points(x, y, w, h, radius, max_error)
        if rot:
            a = math.radians(rot)
            c, s = math.cos(a), math.sin(a)
            pts = [(x + (px - x) * c - (py - y) * s,
                    y + (px - x) * s + (py - y) * c) for px, py in pts]
        return pts
    return []


def flatten_path(path: Any, max_error: float = MAX_ERROR_MM) -> List[Point]:
    """Flatten one EasyEDA single-polygon path (``L``, ``ARC``, ``CARC``, ``C``, ``R``,
    ``CIRCLE``) into raw EasyEDA points."""
    path = _as_list(path)
    if not isinstance(path, list) or not path:
        return []
    if isinstance(path[0], str):
        return _flatten_shape(path, max_error)
    if isinstance(path[0], list):           # nested: the caller picks a sub-path
        return []

    points: List[Point] = []
    index = 0
    length = len(path)

    def number() -> Optional[float]:
        nonlocal index
        if index >= length:
            return None
        value = path[index]
        index += 1
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def pair() -> Optional[Point]:
        x = number()
        y = number()
        return None if x is None or y is None else (x, y)

    first = pair()
    if first is None:
        return []
    points.append(first)
    current = first

    while index < length:
        token = path[index]
        if isinstance(token, str):
            index += 1
            op = token.upper()
            if op == "L":
                while index < length and not isinstance(path[index], str):
                    nxt = pair()
                    if nxt is None:
                        break
                    points.append(nxt)
                    current = nxt
            elif op in ("ARC", "CARC"):
                angle = number()
                end = pair()
                if angle is None or end is None:
                    break
                points.extend(_arc_points(current, end, angle, ARC_STEPS))
                current = end
            elif op == "C":
                c1, c2, end = pair(), pair(), pair()
                if c1 is None or c2 is None or end is None:
                    break
                points.extend(_cubic_bezier(current, c1, c2, end, i / ARC_STEPS)
                              for i in range(1, ARC_STEPS + 1))
                current = end
        else:
            nxt = pair()
            if nxt is None:
                break
            points.append(nxt)
            current = nxt
    return points


def path_rings(path: Any, max_error: float = MAX_ERROR_MM) -> List[List[Point]]:
    """The rings of a path: a POUR/POLY may be one ring or an outer ring plus holes."""
    path = _as_list(path)
    if not isinstance(path, list) or not path:
        return []
    if isinstance(path[0], list):
        return [ring for ring in (flatten_path(part, max_error) for part in path) if ring]
    ring = flatten_path(path, max_error)
    return [ring] if ring else []


# ---------------------------------------------------------------------------
# Pads
# ---------------------------------------------------------------------------

def _pad_definition(value: Any) -> Optional[dict]:
    """Normalize a pad shape to a dict; accept the list form used by older data."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value:
        kind = str(value[0]).upper()
        if kind in ("RECT", "ROUND", "ELLIPSE", "OVAL") and len(value) >= 3:
            return {"padType": "ROUND" if kind == "ROUND" else kind,
                    "width": value[1], "height": value[2],
                    "radius": value[3] if len(value) > 3 else 0}
        if kind in ("POLYGON", "POLY") and len(value) >= 2:
            return {"padType": "POLYGON", "path": value[1]}
        if kind == "NGON" and len(value) >= 2:
            return {"padType": "NGON", "width": value[1], "height": value[1]}
    return None


def pad_shape_rings(shape: Any, max_error: float) -> Tuple[List[List[Point]], bool]:
    """Rings of a pad shape in its own frame; ``True`` when they are already absolute."""
    definition = _pad_definition(shape)
    if definition is None:
        return [], False
    kind = str(definition.get("padType") or definition.get("shape") or "").upper()
    try:
        width = float(definition.get("width") or 0)
        height = float(definition.get("height") or 0)
        radius = float(definition.get("radius") or 0)
    except (TypeError, ValueError):
        width = height = radius = 0.0
    if kind in ("ROUND", "CIRCLE"):
        kind = "ELLIPSE"
        width = height = width or height
    if kind == "RECT":
        if width <= 0 or height <= 0:
            return [], False
        return [rounded_rect(width, height, radius, 0.0, (), max_error)], False
    if kind == "ELLIPSE":
        if width <= 0 or height <= 0:
            return [], False
        if abs(width - height) < 1e-9:
            return [circle_points(0.0, 0.0, width / 2.0, max_error)], False
        return [[(width / 2.0 * math.cos(2 * math.pi * i / CIRCLE_POINTS),
                  height / 2.0 * math.sin(2 * math.pi * i / CIRCLE_POINTS))
                 for i in range(CIRCLE_POINTS)]], False
    if kind == "OVAL":
        if width <= 0 or height <= 0:
            return [], False
        return [oval(width, height, max_error)], False
    if kind == "POLYGON":
        rings = path_rings(definition.get("path") or [], max_error)
        return rings, True
    if kind == "NGON":
        if width <= 0:
            return [], False
        sides = int(definition.get("sides") or 6)
        return [[(width / 2.0 * math.cos(2 * math.pi * i / sides),
                  width / 2.0 * math.sin(2 * math.pi * i / sides))
                 for i in range(sides)]], False
    if width > 0 and height > 0:
        return [rounded_rect(width, height, radius, 0.0, (), max_error)], False
    return [], False


def pad_shape_for_layer(pad: dict, layer_id: int, max_error: float) -> Tuple[List[List[Point]], bool]:
    """The shape used on ``layer_id``: a specialPad override wins over defaultPad."""
    chosen = pad.get("defaultPad")
    for override in pad.get("specialPad") or []:
        if not override:
            continue
        if isinstance(override, dict):
            start, end = override.get("startLayer"), override.get("endLayer")
            shape = override.get("pad")
        elif isinstance(override, list) and len(override) >= 3:
            start, end, shape = override[0], override[1], override[2]
        else:
            continue
        try:
            if int(start) <= layer_id <= int(end):
                chosen = shape
        except (TypeError, ValueError):
            continue
    return pad_shape_rings(chosen, max_error)


def pad_rings_in_footprint(pad: dict, rings: Sequence[Sequence[Point]],
                           absolute: bool) -> List[List[Point]]:
    """Place a shape in the footprint frame (EasyEDA Y-up, mil)."""
    try:
        cx = float(pad.get("centerX") or 0)
        cy = float(pad.get("centerY") or 0)
        angle = float(pad.get("padAngle") or 0)
    except (TypeError, ValueError):
        cx = cy = angle = 0.0
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    out: List[List[Point]] = []
    for ring in rings:
        placed = []
        for x, y in ring:
            if absolute:
                # POLYGON coordinates are already in the footprint frame; only a pad
                # angle turns them, about the pad's own origin.
                px, py = x - cx, y - cy
                placed.append((cx + px * c - py * s, cy + px * s + py * c))
            else:
                placed.append((cx + x * c - y * s, cy + x * s + y * c))
        out.append(placed)
    return out


def place_in_component(rings: Sequence[Sequence[Point]], component: dict,
                       side: str, units: Units,
                       origin: Point = (0.0, 0.0)) -> List[List[Point]]:
    """EasyEDA local rings -> KiPI board rings (mm, Y-down), using KiPI's Frame."""
    try:
        cx = float(component.get("x", component.get("positionX")) or 0)
        cy = float(component.get("y", component.get("positionY")) or 0)
        angle = float(component.get("angle") or 0)
    except (TypeError, ValueError):
        cx = cy = angle = 0.0
    # Frame's origin is already in KiPI's mm/Y-down frame; the local point is converted
    # from EasyEDA's mil/Y-up frame before it is rotated.
    frame = Frame((cx * units.mm, -cy * units.mm), angle)
    out: List[List[Point]] = []
    for ring in rings:
        placed = []
        for x, y in ring:
            # Footprint geometry is stored relative to the canvas coordinates;
            # the component anchor is its declared footprint origin.
            x, y = x - origin[0], y - origin[1]
            # Pro flips the footprint's local Y on the bottom, before rotation.
            # Mirroring X instead exchanges pin positions (even on 2-pad parts).
            local_y = -y if side == "bottom" else y
            placed.append(frame((x * units.mm, -local_y * units.mm)))
        out.append(placed)
    return out


def hole_size(hole: Any) -> Tuple[float, float]:
    """The drill in the pad's own frame, in EasyEDA units; (0, 0) for no hole."""
    if isinstance(hole, dict):
        kind = str(hole.get("holeType") or "ROUND").upper()
        try:
            width = float(hole.get("width") or 0)
            height = float(hole.get("height") or 0)
        except (TypeError, ValueError):
            return (0.0, 0.0)
    elif isinstance(hole, list) and len(hole) >= 3:
        kind = str(hole[0]).upper()
        try:
            width, height = float(hole[1]), float(hole[2])
        except (TypeError, ValueError):
            return (0.0, 0.0)
    else:
        return (0.0, 0.0)
    if width <= 0 and height <= 0:
        return (0.0, 0.0)
    if kind == "ROUND" or abs(width - height) < 1e-9:
        size = max(width, height)
        return (size, size)
    return (width, height)


def hole_is_slot(hole: Any) -> bool:
    if isinstance(hole, dict):
        return str(hole.get("holeType") or "").upper() in ("SLOT", "RECT", "ROUND_RECT")
    if isinstance(hole, list) and hole:
        return str(hole[0]).upper() in ("SLOT", "RECT", "ROUND_RECT")
    return False
