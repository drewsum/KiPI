"""ODB++ layer features: pads, lines, arcs and surfaces, and the symbols pads are drawn
with.

Coordinates are turned into KiPI's board frame as they are read: millimeters, y down (ODB++
has y up). Angles in ODB++ turn clockwise; KiPI's turn counter-clockwise as KiCad shows
them, so a pad at ODB++ rotation r is at -r.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kipi.core.geometry.arcs import arc_points
from kipi.core.geometry.pad_shapes import DEFAULT_MAX_ERROR_MM, circle_points, oval, rounded_rect

MM_PER_INCH = 25.4
NM = 6                     # decimals of a millimeter in a nanometer

Point = Tuple[float, float]


class Units:
    """A file's units: coordinates in mm or inches; symbol sizes in microns or mils."""

    def __init__(self, unit: str = "MM"):
        self.inch = unit.strip().upper().startswith("INCH")
        self.coord = MM_PER_INCH if self.inch else 1.0
        self.symbol = MM_PER_INCH / 1000.0 if self.inch else 0.001

    @staticmethod
    def of(text: str, default: "Units") -> "Units":
        """The units a file declares (``UNITS=MM`` or ``U MM``), else ``default``."""
        for line in text.splitlines()[:40]:
            s = line.strip()
            if s.upper().startswith("UNITS="):
                return Units(s.split("=", 1)[1])
            if s.upper().startswith("U ") and len(s.split()) == 2:
                return Units(s.split()[1])
        return default


@dataclass
class Feature:
    kind: str                         # "P" pad, "L" line, "A" arc, "S" surface
    symbol: str = ""                  # pads, lines and arcs: the symbol drawn with
    positive: bool = True
    attrs: Dict[int, Optional[str]] = field(default_factory=dict)
    # P: the pad's place and turn
    at: Point = (0.0, 0.0)
    angle_deg: float = 0.0            # counter-clockwise, KiCad's sense
    mirror: bool = False
    # L, A: ends, and an arc's center and direction (clockwise as seen on the board)
    start: Point = (0.0, 0.0)
    end: Point = (0.0, 0.0)
    center: Point = (0.0, 0.0)
    clockwise: bool = False
    # S: (is_hole, points) per contour, in order: each island followed by its holes
    contours: List[Tuple[bool, List[Point]]] = field(default_factory=list)


@dataclass
class LayerFeatures:
    features: List[Feature]
    symbols: Dict[int, str]
    attr_names: Dict[int, str]
    attr_texts: Dict[int, str]
    units: Units

    def attr(self, f: Feature, name: str) -> Optional[str]:
        """The value of the attribute ``name`` on ``f`` ("" if set without one), else
        None."""
        for idx, val in f.attrs.items():
            if self.attr_names.get(idx) == name:
                return val if val is not None else ""
        return None


def _pt(units: Units, x: str, y: str) -> Point:
    return (round(float(x) * units.coord, NM), round(-float(y) * units.coord, NM))


def _attrs(text: str) -> Dict[int, Optional[str]]:
    out: Dict[int, Optional[str]] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, val = part.partition("=")
        try:
            out[int(key)] = val if val else None
        except ValueError:
            continue
    return out


def _orient(fields: List[str], i: int) -> Tuple[float, bool]:
    """``(angle, mirror)`` from a pad's orientation fields at ``i``: legacy 0..7 (90 degree
    steps, 4..7 mirrored) or 8/9 followed by any clockwise angle (9 mirrored).

    A mirrored pad is turned clockwise and then mirrored; on the board that is the
    symbol mirrored first and then turned the other way, counter-clockwise."""
    try:
        code = int(fields[i])
    except (IndexError, ValueError):
        return 0.0, False
    if code in (8, 9):
        rot = float(fields[i + 1]) if len(fields) > i + 1 else 0.0
        mirror = code == 9
    else:
        rot = 90.0 * (code % 4)
        mirror = code >= 4
    return (rot if mirror else -rot) % 360.0, mirror


def _arc_mid(start: Point, end: Point, center: Point, clockwise: bool) -> Point:
    """A point halfway along the arc from ``start`` to ``end`` (y down; ``clockwise`` as
    seen on the board)."""
    cx, cy = center
    r = math.dist(start, center)
    a1 = math.atan2(start[1] - cy, start[0] - cx)
    a2 = math.atan2(end[1] - cy, end[0] - cx)
    # With y down, clockwise on screen is increasing atan2 angle.
    sweep = (a2 - a1) % (2 * math.pi) if clockwise else -((a1 - a2) % (2 * math.pi))
    if abs(sweep) < 1e-12:
        sweep = 2 * math.pi if clockwise else -2 * math.pi       # a full circle
    am = a1 + sweep / 2
    return (cx + r * math.cos(am), cy + r * math.sin(am))


def arc_polyline(start: Point, end: Point, center: Point, clockwise: bool) -> List[Point]:
    """The points along an arc, ``start`` included."""
    if start == end:                                    # a full circle
        r = math.dist(start, center)
        a0 = math.atan2(start[1] - center[1], start[0] - center[0])
        n = 64
        sign = 1 if clockwise else -1
        return [(center[0] + r * math.cos(a0 + sign * 2 * math.pi * i / n),
                 center[1] + r * math.sin(a0 + sign * 2 * math.pi * i / n)) for i in range(n)]
    mid = _arc_mid(start, end, center, clockwise)
    return [start] + arc_points(start, mid, end)


def parse(text: str, default_units: Units) -> LayerFeatures:
    units = Units.of(text, default_units)
    symbols: Dict[int, str] = {}
    attr_names: Dict[int, str] = {}
    attr_texts: Dict[int, str] = {}
    features: List[Feature] = []
    surface: Optional[Feature] = None
    contour: Optional[List[Point]] = None
    hole = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] == "#":
            continue
        head = line[0]
        if surface is not None:
            fields = line.split()
            tag = fields[0]
            if tag == "OB":
                contour, hole = [_pt(units, fields[1], fields[2])], fields[3:4] == ["H"]
            elif tag == "OS" and contour is not None:
                contour.append(_pt(units, fields[1], fields[2]))
            elif tag == "OC" and contour is not None:
                end = _pt(units, fields[1], fields[2])
                center = _pt(units, fields[3], fields[4])
                cw = fields[5].upper() == "Y" if len(fields) > 5 else True
                contour.extend(arc_polyline(contour[-1], end, center, cw)[1:])
            elif tag == "OE" and contour is not None:
                if len(contour) > 1 and contour[0] == contour[-1]:
                    contour.pop()
                if len(contour) >= 3:
                    surface.contours.append((hole, contour))
                contour = None
            elif tag == "SE":
                features.append(surface)
                surface = None
            continue
        if head == "$":
            num, _, rest = line[1:].partition(" ")
            name = rest.split()[0] if rest.split() else ""
            try:
                symbols[int(num)] = name
            except ValueError:
                pass
            continue
        if head in "@&":
            num, _, rest = line[1:].partition(" ")
            try:
                (attr_names if head == "@" else attr_texts)[int(num)] = rest.strip()
            except ValueError:
                pass
            continue
        body, _, attr_text = line.partition(";")
        fields = body.split()
        tag = fields[0]
        try:
            if tag == "P":
                angle, mirror = _orient(fields, 6)
                features.append(Feature(
                    "P", symbols.get(int(fields[3]), ""), fields[4] != "N", _attrs(attr_text),
                    at=_pt(units, fields[1], fields[2]), angle_deg=angle, mirror=mirror))
            elif tag == "L":
                features.append(Feature(
                    "L", symbols.get(int(fields[5]), ""), fields[6] != "N", _attrs(attr_text),
                    start=_pt(units, fields[1], fields[2]), end=_pt(units, fields[3], fields[4])))
            elif tag == "A":
                features.append(Feature(
                    "A", symbols.get(int(fields[7]), ""), fields[8] != "N", _attrs(attr_text),
                    start=_pt(units, fields[1], fields[2]), end=_pt(units, fields[3], fields[4]),
                    center=_pt(units, fields[5], fields[6]),
                    # The board's picture is the same either way up, so an arc clockwise
                    # in ODB++ is clockwise on the board.
                    clockwise=len(fields) > 10 and fields[10].upper() == "Y"))
            elif tag == "S":
                surface = Feature("S", positive=fields[1] != "N", attrs=_attrs(attr_text))
            elif tag in ("T", "B"):
                features.append(Feature(tag))          # text, barcodes: kept for numbering
        except (IndexError, ValueError):
            features.append(Feature("?"))              # numbering must not shift
    return LayerFeatures(features, symbols, attr_names, attr_texts, units)


# --- symbols ------------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d*)?|\.\d+)"
_RECT = re.compile(rf"rect{_NUM}x{_NUM}(?:x(r|c){_NUM}(?:x(\d+))?)?$")
_TWO = re.compile(rf"(oval|di|s|r|rect){_NUM}(?:x{_NUM})?$")
_DONUT = re.compile(rf"donut_r{_NUM}x{_NUM}$")
_OCT = re.compile(rf"oct{_NUM}x{_NUM}x{_NUM}$")
# ODB++ numbers rectangle corners counter-clockwise from the top right; KiPI's builders
# name them as seen on the board.
_CORNERS = {"1": "top_right", "2": "top_left", "3": "bottom_left", "4": "bottom_right"}


def symbol_outline(name: str, units: Units,
                   max_error: float = DEFAULT_MAX_ERROR_MM) -> Optional[List[Point]]:
    """The outline of a standard symbol centered on the origin, y down, in mm; None for a
    symbol KiPI does not know (a user symbol, a thermal)."""
    s = units.symbol
    m = _RECT.match(name)
    if m:
        w, h = float(m.group(1)) * s, float(m.group(2)) * s
        corners = [_CORNERS[c] for c in (m.group(5) or "1234") if c in _CORNERS]
        if m.group(3) == "r":
            return rounded_rect(w, h, float(m.group(4)) * s, 0.0, [], max_error)
        if m.group(3) == "c":
            return rounded_rect(w, h, 0.0, float(m.group(4)) * s, corners, max_error)
        return rounded_rect(w, h, 0.0, 0.0, [], max_error)
    m = _TWO.match(name)
    if m:
        kind, a = m.group(1), float(m.group(2)) * s
        b = float(m.group(3)) * s if m.group(3) else a
        if kind == "r":
            return circle_points(0.0, 0.0, a / 2.0, max_error)
        if kind in ("s", "rect"):
            return rounded_rect(a, b, 0.0, 0.0, [], max_error)
        if kind == "oval":
            return oval(a, b, max_error)
        if kind == "di":
            return [(a / 2, 0.0), (0.0, b / 2), (-a / 2, 0.0), (0.0, -b / 2)]
    m = _OCT.match(name)
    if m:
        w, h, c = (float(m.group(i)) * s for i in (1, 2, 3))
        return rounded_rect(w, h, 0.0, c, list(_CORNERS.values()), max_error)
    return None


def symbol_size(name: str, units: Units) -> Optional[Tuple[float, float]]:
    """A symbol's width and height in mm (a round one's diameter twice)."""
    m = _RECT.match(name) or _TWO.match(name) or _OCT.match(name)
    if m:
        a = round(float(m.group(2) if m.re is _TWO else m.group(1)) * units.symbol, NM)
        b_text = m.group(3) if m.re is _TWO else m.group(2)
        b = round(float(b_text) * units.symbol, NM) if b_text else a
        return (a, b)
    m = _DONUT.match(name)
    if m:
        d = round(float(m.group(1)) * units.symbol, NM)
        return (d, d)
    return None


def round_width(name: str, units: Units) -> Optional[float]:
    """The diameter of a round symbol (``r150``), the width a track is drawn with."""
    m = re.match(rf"r{_NUM}$", name)
    return round(float(m.group(1)) * units.symbol, NM) if m else None


def pad_points(f: Feature, units: Units, max_error: float = DEFAULT_MAX_ERROR_MM,
               mirror: bool = True):
    """A pad feature's outline on the board, or None for an unknown symbol. ``mirror``
    False leaves mirrored symbols unmirrored: KiCad writes a bottom pad's symbol as it
    lies on the board, already mirrored."""
    pts = symbol_outline(f.symbol, units, max_error)
    if pts is None:
        return None
    a = math.radians(f.angle_deg)
    c, s = math.cos(a), math.sin(a)
    out = []
    for x, y in pts:
        if f.mirror and mirror:
            x = -x
        out.append((round(f.at[0] + x * c + y * s, NM), round(f.at[1] - x * s + y * c, NM)))
    return out


def geometry(f: Feature, units: Units, max_error: float = DEFAULT_MAX_ERROR_MM,
             mirror: bool = True):
    """The copper of a feature as a shapely geometry, or None (negative features, text,
    unknown symbols)."""
    from shapely.geometry import LineString, Point as SPoint, Polygon as SPolygon
    from shapely.ops import unary_union
    if not f.positive:
        return None
    if f.kind == "P":
        pts = pad_points(f, units, max_error, mirror)
        if pts is None:
            size = symbol_size(f.symbol, units)
            return SPoint(f.at).buffer(size[0] / 2.0, quad_segs=8) if size else None
        g = SPolygon(pts)
        return g if g.is_valid else g.buffer(0)
    if f.kind in ("L", "A"):
        path = [f.start, f.end] if f.kind == "L" else \
            arc_polyline(f.start, f.end, f.center, f.clockwise)
        width = round_width(f.symbol, units)
        if width is not None:
            if len(path) < 2 or path[0] == path[-1] and len(path) == 2:
                return SPoint(path[0]).buffer(width / 2.0, quad_segs=8)
            return LineString(path).buffer(width / 2.0, quad_segs=8)
        size = symbol_size(f.symbol, units)
        if size is None:
            return None
        return LineString(path).buffer(size[0] / 2.0, cap_style="square")
    if f.kind == "S":
        # Each island and the holes that follow it. The holes are cut out together, in one
        # difference: one at a time, each cut reworks the whole plane again, which made a
        # plane with thousands of antipads take minutes (Kria carrier: 188 s of 208 s).
        islands = []
        for is_hole, pts in f.contours:
            if not is_hole:
                shell = SPolygon(pts)
                islands.append((shell if shell.is_valid else shell.buffer(0), []))
            elif islands:
                islands[-1][1].append(SPolygon(pts).buffer(0))
        parts = [shell.difference(unary_union(holes)) if holes else shell
                 for shell, holes in islands]
        return unary_union(parts) if parts else None
    return None
