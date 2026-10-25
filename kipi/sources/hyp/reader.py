"""Read a HyperLynx board file (``.hyp``) into a
:class:`~kipi.core.model.board.BoardSnapshot`.

A ``.hyp`` file (KiCad writes one with File > Export > Hyperlynx) gives the board as
HyperLynx simulates it: the stackup, the pad stacks, and each net's pins, vias, track
segments and copper polygons. It names each part and its layer, but not where the part
is or how it is turned, so a footprint is put in the middle of its pads.

The file is y up and KiPI's board y down: the file's (x, y) is (x, -y) on the board.
Its arcs (``ARC``, ``CURVE``, ``PERIMETER_ARC``) run counterclockwise, in the file's y-up
frame, from their first point to their second. Copper layers are numbered by their place
in the stack (:func:`~kipi.core.model.layers.copper_id`).

A ``.hyp`` file has no item ids. Pads are named by part and pin (``U1#3``, in file order),
vias by where they are, as the ODB++ reader names them.
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from kipi.core.geometry.copper import shapely_to_polys
from kipi.core.model.board import (
    BoardSnapshot, Footprint, Net, Pad, Poly, Track, Via, Zone,
)
from kipi.core.model.layers import B_CU, F_CU, canonical_name, copper_id
from kipi.core.model.stackup import (
    DEFAULT_INNER_CU_MM, DEFAULT_OUTER_CU_MM, build_stackup, coating_of,
)
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.hyp.parse import HypError, Record, parse

#: Copper weight: the thickness of 1 oz per square foot (as the ODB++ reader takes it).
MM_PER_OZ = 0.035
#: Copper's density, for a metric weight (kg/m²) as a thickness.
COPPER_KG_PER_M3 = 8960.0
# A HyperLynx pad stack's pad shapes.
OVAL, RECTANGLE, OBLONG = 0, 1, 2
# Segments in a quarter circle, for arcs and round pads (as kipi.core.geometry.copper).
QUAD_SEGS = 8
NM = 6                                   # decimals of a mm kept: 1 nm
# The pad stack layer that stands for every copper layer.
ALL_METAL = "MDEF"
# KiCad's export puts each pad with no net on a net of its own, EmptyNet0, EmptyNet1, ...
_KICAD_NO_NET = re.compile(r"EmptyNet\d+$")


@dataclass
class Units:
    mm: float = 25.4                     # mm per file unit: inches, or metres (METRIC)
    weight: bool = False                 # copper thickness given as a weight

    def xy(self, x: float, y: float) -> Tuple[float, float]:
        """A file point on the board: in mm, y down."""
        return (round(x * self.mm, NM), round(-y * self.mm, NM))


def _units(top: List[Record]) -> Units:
    rec = next((r for r in top if r.kind == "UNITS"), None)
    if rec is None:
        return Units()
    words = [rec.value.upper()] + [w.upper() for w in rec.words]
    return Units(mm=1000.0 if "METRIC" in words else 25.4, weight="WEIGHT" in words)


def _f(text: Optional[str], default: float = 0.0) -> float:
    try:
        return float(text) if text not in (None, "") else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Stackup
# ---------------------------------------------------------------------------

_COPPER_KINDS = ("SIGNAL", "PLANE")


def read_stackup(top: List[Record], units: Units, progress: ProgressReporter,
                 assumed: List[str]):
    """The stackup, and each copper layer's id by its name in the file."""
    rec = next((r for r in top if r.kind == "STACKUP"), None)
    layers = rec.items if rec is not None else []
    copper = [l for l in layers if l.kind in _COPPER_KINDS]
    if not copper:
        raise HypError("the file has no copper layers (no SIGNAL or PLANE in its STACKUP)")
    n = len(copper)
    ids: Dict[str, int] = {}
    names: Dict[int, str] = {}
    for i, l in enumerate(copper):
        lid = copper_id(i, n)
        name = l.get("L") or canonical_name(lid)
        ids[name] = lid
        canonical = canonical_name(lid)
        names[lid] = canonical if name.lower() == canonical.lower() else name

    entries, coatings = [], []
    missing_cu, missing_diel = [], []
    started = False
    k = 0
    for l in layers:
        if l.kind in _COPPER_KINDS:
            started = True
            lid = copper_id(k, n)
            k += 1
            t = _f(l.get("T"))
            t = (t * (MM_PER_OZ if units.mm != 1000.0 else 1000.0 / COPPER_KG_PER_M3)
                 if units.weight else t * units.mm)
            t += _f(l.get("P")) * units.mm               # plating, on the outer layers
            if t <= 0:
                missing_cu.append(names[lid])
                t = DEFAULT_OUTER_CU_MM if lid in (F_CU, B_CU) else DEFAULT_INNER_CU_MM
            entries.append(("cu", lid, t, "", 0.0))
        elif l.kind == "DIELECTRIC":
            t = _f(l.get("T")) * units.mm
            name, material = l.get("L"), l.get("M")
            if not started or k == n:
                # Outside the copper: a solder mask or silkscreen, or nothing of the board.
                text = f"{name} {material}".lower()
                if "mask" in text or "silk" in text:
                    coatings.append(coating_of("B." if started else "F.",
                                               "Solder Mask" if "mask" in text
                                               else "Silk Screen", t, material))
                continue
            if t <= 0:
                missing_diel.append(name or f"the dielectric under {names[copper_id(k - 1, n)]}")
            entries.append(("diel", -1, t, material, _f(l.get("C"))))
    if missing_diel:
        known = [e[2] for e in entries if e[0] == "diel" and e[2] > 0]
        fill = sum(known) / len(known) if known else 0.2
        entries = [(kd, lid, t if t > 0 else fill, m, er) for kd, lid, t, m, er in entries]
        assumed.append(f"Stackup: no thickness in the file for {', '.join(missing_diel)}, so "
                       f"{fill:.3f} mm is used for each (the mean of the other dielectrics)")
    if missing_cu:
        assumed.append(f"Stackup: no copper thickness in the file for {', '.join(missing_cu)}, "
                       "so 35 um outer / 17.5 um inner copper is used")
    if not any(e[0] == "diel" for e in entries) and n > 1:
        entries = []                    # no dielectrics at all: the default stackup
    return build_stackup(entries, [copper_id(i, n) for i in range(n)], names, progress,
                         assumed, [c for c in coatings if c is not None]), ids


# ---------------------------------------------------------------------------
# Pad stacks and their shapes
# ---------------------------------------------------------------------------

@dataclass
class PadShape:
    shape: int
    sx: float                            # mm
    sy: float
    angle: float                         # degrees clockwise, as KiCad's export writes it


@dataclass
class PadStack:
    name: str
    drill_mm: float
    pads: Dict[str, PadShape] = field(default_factory=dict)   # by layer name or MDEF

    def on(self, layer_ids: Dict[str, int]) -> Dict[int, PadShape]:
        """The pad on each copper layer: a layer's own, else the all-layer one."""
        out: Dict[int, PadShape] = {}
        if ALL_METAL in self.pads:
            out = {lid: self.pads[ALL_METAL] for lid in layer_ids.values()}
        for name, p in self.pads.items():
            lid = _layer(layer_ids, name)
            if lid is not None:
                out[lid] = p
        return out


def _layer(layer_ids: Dict[str, int], name: str) -> Optional[int]:
    if name in layer_ids:
        return layer_ids[name]
    low = name.lower()
    return next((lid for n, lid in layer_ids.items() if n.lower() == low), None)


def read_padstacks(top: List[Record], units: Units) -> Dict[str, PadStack]:
    """Each pad stack by name. A row is (layer, shape, sx, sy, angle[, kind]): the metal
    pads (kind M, or none given) are copper, anti-pads (A) and thermals are not."""
    out: Dict[str, PadStack] = {}
    for rec in top:
        if rec.kind != "PADSTACK":
            continue
        ps = PadStack(rec.value, _f(rec.words[0] if rec.words else None) * units.mm)
        for row in rec.items:
            w = row.words
            if len(w) < 5 or (len(w) > 5 and w[5].upper() != "M"):
                continue
            ps.pads[w[0]] = PadShape(int(_f(w[1])), _f(w[2]) * units.mm,
                                     _f(w[3]) * units.mm, _f(w[4]))
        out[ps.name] = ps
    return out


def pad_polygon(p: PadShape, at: Tuple[float, float]):
    """A pad's copper on the board (shapely), centred on the board point ``at``.

    An oval is round-ended along its longer side (a circle when square), a rectangle
    square-cornered, an oblong a rectangle with corners rounded by a quarter of its shorter
    side (KiCad's rounded rectangle, which its export writes as an oblong)."""
    from shapely import affinity
    from shapely.geometry import Point as SPoint, box
    sx, sy = p.sx, p.sy
    if sx <= 0 or sy <= 0:
        return None
    if p.shape == RECTANGLE:
        g = box(-sx / 2, -sy / 2, sx / 2, sy / 2)
    else:
        r = min(sx, sy) / 2 * (1.0 if p.shape == OVAL else 0.5)
        if r >= min(sx, sy) / 2 and abs(sx - sy) < 1e-9:
            g = SPoint(0, 0).buffer(r, quad_segs=QUAD_SEGS)
        else:
            g = box(-sx / 2 + r, -sy / 2 + r, sx / 2 - r, sy / 2 - r).buffer(
                r, quad_segs=QUAD_SEGS)
    # Turned clockwise as seen, which is counterclockwise on KiPI's y-down board. KiCad
    # writes a pad's angle as 180 degrees less its orientation (counterclockwise as seen).
    g = affinity.rotate(g, p.angle, origin=(0, 0))
    return affinity.translate(g, at[0], at[1])


# ---------------------------------------------------------------------------
# Arcs and outlines
# ---------------------------------------------------------------------------

def arc_points(p1, p2, c, n_min: int = 2) -> List[Tuple[float, float]]:
    """The points of the arc from ``p1`` to ``p2`` about ``c``, counterclockwise in the
    file's y-up frame (file coordinates in, file coordinates out). The same two points
    are a full circle."""
    r = math.dist(p1, c)
    a1 = math.atan2(p1[1] - c[1], p1[0] - c[0])
    a2 = math.atan2(p2[1] - c[1], p2[0] - c[0])
    sweep = (a2 - a1) % (2 * math.pi)
    if sweep < 1e-12:
        sweep = 2 * math.pi
    n = max(n_min, int(math.ceil(sweep / (math.pi / 2) * QUAD_SEGS)))
    return [(c[0] + r * math.cos(a1 + sweep * i / n), c[1] + r * math.sin(a1 + sweep * i / n))
            for i in range(n + 1)]


def _ring(rec: Record) -> List[Tuple[float, float]]:
    """A POLYGON's or POLYVOID's outline in file coordinates: from its X, Y, a straight
    line to each LINE's point and an arc along each CURVE. A CURVE, counterclockwise from
    its first point to its second, is followed backwards when the outline reaches its
    second point first (a clockwise turn of the outline)."""
    pts = [(_f(rec.get("X")), _f(rec.get("Y")))] if "X" in rec.fields else []
    for item in rec.items:
        if item.kind == "LINE":
            pts.append((_f(item.get("X")), _f(item.get("Y"))))
        elif item.kind == "CURVE":
            p1 = (_f(item.get("X1")), _f(item.get("Y1")))
            p2 = (_f(item.get("X2")), _f(item.get("Y2")))
            c = (_f(item.get("XC")), _f(item.get("YC")))
            arc = arc_points(p1, p2, c)
            if pts and math.dist(pts[-1], p2) < math.dist(pts[-1], p1):
                arc.reverse()
            if not pts or math.dist(pts[-1], arc[0]) > 1e-9:
                pts.append(arc[0])
            pts.extend(arc[1:])
    return pts


def read_outline(top: List[Record], units: Units) -> List[Poly]:
    """The board's outline from its PERIMETER_SEGMENT and PERIMETER_ARC records: each
    closed loop is a face, and a loop inside another a cutout."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import polygonize, unary_union
    board = next((r for r in top if r.kind == "BOARD"), None)
    if board is None:
        return []
    lines = []
    for item in board.items:
        p1 = (_f(item.get("X1")), _f(item.get("Y1")))
        p2 = (_f(item.get("X2")), _f(item.get("Y2")))
        if item.kind == "PERIMETER_SEGMENT":
            pts = [p1, p2]
        elif item.kind == "PERIMETER_ARC":
            pts = arc_points(p1, p2, (_f(item.get("XC")), _f(item.get("YC"))))
        else:
            continue
        lines.append(LineString([units.xy(*p) for p in pts]))
    if not lines:
        return []
    faces = list(polygonize(unary_union(lines)))
    shape = None
    for f in faces:                      # even-odd: a loop inside another cuts it out
        loop = Polygon(f.exterior)
        shape = loop if shape is None else shape.symmetric_difference(loop)
    return shapely_to_polys(shape)


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

def read_board(path, progress: ProgressReporter = NULL) -> BoardSnapshot:
    """Read the ``.hyp`` file at ``path``. Raises :class:`HypError` for a file KiPI cannot
    read."""
    path = Path(path).resolve()
    t0 = time.perf_counter()
    with progress.timed("Reading the file"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-1")
        top = parse(text)
    if not any(r.kind in ("STACKUP", "NET", "BOARD") for r in top):
        raise HypError(f"{path.name} is not a HyperLynx board file (it has no BOARD, "
                       "STACKUP or NET records)")
    version = next((r.value for r in top if r.kind == "VERSION"), "?")
    board_rec = next((r for r in top if r.kind == "BOARD"), None)
    kicad = bool(board_rec and any(w.lower().endswith(".kicad_pcb") for w in board_rec.words))
    progress.info(f"HyperLynx {version}" + (" from KiCad" if kicad else ""))
    progress.check_cancel()

    units = _units(top)
    assumed: List[str] = []
    with progress.timed("Reading stackup"):
        stackup, cu_ids = read_stackup(top, units, progress, assumed)
    padstacks = read_padstacks(top, units)
    all_cu = [c.id for c in stackup.copper]

    devices: Dict[str, str] = {}             # ref -> side
    values: Dict[str, Tuple[str, str]] = {}  # ref -> (value, package)
    dev = next((r for r in top if r.kind == "DEVICES"), None)
    for d in (dev.items if dev else []):
        ref = d.get("REF")
        if not ref:
            continue
        lid = _layer(cu_ids, d.get("L"))
        devices[ref] = "bottom" if lid is not None and lid == all_cu[-1] else "top"
        values[ref] = (d.get("VAL"), d.get("PKG"))
    refs = sorted(devices, key=len, reverse=True)

    pads: List[Pad] = []
    vias: List[Via] = []
    tracks: List[Track] = []
    zones: List[Zone] = []
    net_names = {""}
    missing_stacks = set()
    pins_by_ref: Dict[str, List[Pad]] = defaultdict(list)
    with progress.timed("Reading nets"):
        for k, net in enumerate(r for r in top if r.kind == "NET"):
            name = "" if kicad and _KICAD_NO_NET.match(net.value) else net.value
            net_names.add(name)
            by_kind: Dict[str, Dict[int, list]] = defaultdict(lambda: defaultdict(list))
            # ID -> [layer, type, outline, holes]
            polys: Dict[str, list] = {}
            for item in net.items:
                kind = item.kind
                if kind == "PIN":
                    pad = _pad(item, name, padstacks, cu_ids, units, refs, missing_stacks,
                               kicad)
                    if pad is not None:
                        pins_by_ref[pad.footprint].append(pad)
                elif kind == "VIA":
                    via = _via(item, name, padstacks, cu_ids, stackup, units, missing_stacks)
                    if via is not None:
                        vias.append(via)
                elif kind in ("SEG", "ARC"):
                    track = _track(item, name, cu_ids, units)
                    if track is not None:
                        tracks.append(track)
                elif kind in ("POLYGON", "POLYVOID", "POLYLINE"):
                    _polygon(item, cu_ids, units, polys, by_kind)
            for lid, ptype, ring, holes, width in polys.values():
                g = _area(ring, holes, width)
                if g is not None and not g.is_empty:
                    by_kind[ptype][lid].append(g)
            for ptype, by_layer in by_kind.items():
                zones.append(_zone(f"{ptype.lower()}{k}", name, by_layer,
                                   "copper" if ptype == "COPPER" else ""))
        progress.check_cancel()

    footprints: List[Footprint] = []
    for ref in sorted(pins_by_ref, key=lambda r: (r == "", r)):
        # Numbered in pin order, so a pad keeps its id when its net is renamed.
        fp_pads = sorted(pins_by_ref[ref], key=lambda p: (_natural(p.number), p.position))
        for i, p in enumerate(fp_pads):
            p.id = f"{ref}#{i}"
        pads.extend(fp_pads)
        if not ref:
            continue
        xs = [p.position[0] for p in fp_pads]
        ys = [p.position[1] for p in fp_pads]
        value, package = values.get(ref, ("", ""))
        footprints.append(Footprint(
            id=ref, reference=ref, value=value,
            position=(round((min(xs) + max(xs)) / 2, NM), round((min(ys) + max(ys)) / 2, NM)),
            side=devices.get(ref, "top"), pad_ids=[p.id for p in fp_pads], footprint=package))
    if missing_stacks:
        progress.warn(f"{len(missing_stacks)} pad stacks are used but not in the file: "
                      f"{', '.join(sorted(missing_stacks)[:5])}; their pads have no copper")
    progress.info(f"{len(footprints)} footprints, {len(pads)} pads")
    progress.info(f"{len(vias)} vias")
    n_arcs = sum(t.is_arc for t in tracks)
    progress.info(f"{len(tracks)} track segments ({n_arcs} arcs), {len(zones)} copper areas")
    assumed.append("Footprints: a HyperLynx file does not say where a part is or how it is "
                   "turned, so each is put in the middle of its pads, unturned")

    with progress.timed("Reading board outline"):
        outline = read_outline(top, units)

    nets = [Net(code=i, name=n) for i, n in enumerate(sorted(net_names))]
    snap = BoardSnapshot(
        board_name=path.stem, project_dir=str(path.parent), stackup=stackup,
        nets=nets, tracks=tracks, vias=vias, pads=pads, footprints=footprints, zones=zones,
        outline=outline, source="hyperlynx", source_format="hyp", assumed=assumed)
    progress.info(f"{len(nets)} nets")
    progress.info(f"Read in {time.perf_counter() - t0:.2f} s")
    return snap


def _natural(text: str):
    """A pin number's sort key: 2 before 10, A2 before A10."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", text)]


def _split_ref(text: str, refs: Sequence[str]) -> Tuple[str, str]:
    """A pin's part and pin from ``R="U1.3"``: the longest known part name it starts with,
    else what comes before its first dot."""
    for ref in refs:
        if text.startswith(ref + "."):
            return ref, text[len(ref) + 1:]
    ref, _, pin = text.partition(".")
    return ref, pin


def _pad(item: Record, net: str, padstacks, cu_ids, units: Units, refs,
         missing: set, kicad: bool) -> Optional[Pad]:
    at = units.xy(_f(item.get("X")), _f(item.get("Y")))
    ref, number = _split_ref(item.get("R"), refs)
    ps = padstacks.get(item.get("P"))
    shapes: Dict[int, List[Poly]] = {}
    angle = None
    drill = 0.0
    if ps is None:
        missing.add(item.get("P"))
    else:
        drill = ps.drill_mm
        for lid, shape in ps.on(cu_ids).items():
            polys = shapely_to_polys(pad_polygon(shape, at))
            polys = [Poly([_round(p) for p in q.outline],
                          [[_round(p) for p in h] for h in q.holes]) for q in polys]
            if polys:
                shapes[lid] = polys
            if angle is None:                # as KiCad shows it: counterclockwise
                angle = ((180.0 if kicad else 0.0) - shape.angle) % 360.0
    if drill > 0:
        ptype = "pth" if shapes else "npth"
    else:
        ptype = "smd"
    return Pad(id="", net=net, footprint=ref, number=number, position=at, pad_type=ptype,
               drill_mm=(round(drill, NM), round(drill, NM)), shapes=shapes,
               angle_deg=angle)


def _round(p) -> Tuple[float, float]:
    return (round(p[0], NM), round(p[1], NM))


def _via(item: Record, net: str, padstacks, cu_ids, stackup, units: Units,
         missing: set) -> Optional[Via]:
    at = units.xy(_f(item.get("X")), _f(item.get("Y")))
    all_cu = [c.id for c in stackup.copper]
    ps = padstacks.get(item.get("P")) if "P" in item.fields else None
    pad_mm: Dict[int, float] = {}
    if ps is not None:
        drill = ps.drill_mm
        on = ps.on(cu_ids)
        pad_mm = {lid: round(max(s.sx, s.sy), NM) for lid, s in on.items()}
        listed = [lid for lid in all_cu if lid in on]
        span = all_cu[all_cu.index(listed[0]):all_cu.index(listed[-1]) + 1] if listed else all_cu
    elif "D" in item.fields:            # the version 1 form: its drill and layer span
        drill = _f(item.get("D")) * units.mm
        a, b = _layer(cu_ids, item.get("L1")), _layer(cu_ids, item.get("L2"))
        span = stackup.layers_between(a, b) if a is not None and b is not None else all_cu
        span = span or all_cu
    else:
        missing.add(item.get("P"))
        return None
    if len(span) < 2:                   # a pad stack on one layer: not a via
        return None
    outer = {all_cu[0], all_cu[-1]}
    if span == all_cu:
        vtype = "through"
    elif span[0] in outer or span[-1] in outer:
        vtype = "blind"
    else:
        vtype = "buried"
    diameter = max(pad_mm.values(), default=0.0)
    return Via(id=f"via@{at[0]:.4f},{at[1]:.4f}", net=net, position=at,
               drill_mm=round(drill, NM), diameter_mm=diameter or round(drill * 2, NM),
               layers=span, via_type=vtype, pad_mm=pad_mm)


def _track(item: Record, net: str, cu_ids, units: Units) -> Optional[Track]:
    lid = _layer(cu_ids, item.get("L"))
    width = _f(item.get("W")) * units.mm
    if lid is None or width <= 0:
        return None
    f1 = (_f(item.get("X1")), _f(item.get("Y1")))
    f2 = (_f(item.get("X2")), _f(item.get("Y2")))
    mid = None
    if item.kind == "ARC":
        pts = arc_points(f1, f2, (_f(item.get("XC")), _f(item.get("YC"))), n_min=2)
        mid = units.xy(*pts[len(pts) // 2])
    return Track(net=net, layer=lid, width_mm=round(width, NM), start=units.xy(*f1),
                 end=units.xy(*f2), mid=mid)


def _polygon(item: Record, cu_ids, units: Units, polys, by_kind) -> None:
    """A POLYGON (copper), POLYVOID (a hole in the POLYGON of its ID) or POLYLINE (an
    open path drawn with a width) of a net."""
    from shapely.geometry import LineString
    ring = [units.xy(*p) for p in _ring(item)]
    pid = item.get("ID")
    if item.kind == "POLYVOID":
        if pid in polys and len(ring) >= 3:
            polys[pid][3].append(ring)
        return
    lid = _layer(cu_ids, item.get("L"))
    if lid is None:
        return
    width = _f(item.get("W")) * units.mm
    if item.kind == "POLYLINE":
        if len(ring) >= 2 and width > 0:
            g = LineString(ring).buffer(width / 2, quad_segs=QUAD_SEGS)
            by_kind["COPPER"][lid].append(g)
        return
    if len(ring) < 3:
        return
    ptype = (item.get("T") or "POUR").upper()
    key = pid if pid else f"#{len(polys)}"
    if key in polys:                     # a second polygon of the same ID
        polys[f"{key}#{len(polys)}"] = polys[key]
    polys[key] = [lid, ptype, ring, [], width]


def _area(ring, holes, width: float):
    """A polygon's copper: its outline less its voids, and the outline's own width."""
    from shapely.geometry import LineString, Polygon
    g = Polygon(ring, holes)
    if not g.is_valid:                   # voids that cross the outline or each other
        import shapely
        g = shapely.make_valid(Polygon(ring))
        if holes:
            g = g.difference(shapely.union_all([_valid(Polygon(h)) for h in holes]))
    if width > 0:                        # its outline is drawn with that width too
        g = g.union(LineString(ring + ring[:1]).buffer(width / 2, quad_segs=QUAD_SEGS))
    return g


def _valid(g):
    import shapely
    return g if g.is_valid else shapely.make_valid(g)


def _zone(zid: str, net: str, by_layer: Dict[int, list], name: str) -> Zone:
    from shapely.ops import unary_union
    filled = {lid: shapely_to_polys(unary_union(gs)) for lid, gs in by_layer.items()}
    return Zone(id=zid, net=net, name=name, priority=0,
                filled={lid: polys for lid, polys in filled.items() if polys})
