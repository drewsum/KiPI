"""Read a KiCad board file (``.kicad_pcb``, KiCad 7 to 10) into a
:class:`~kipi.core.model.board.BoardSnapshot`, without KiCad.

The snapshot matches what the KiCad API gives for the same board: the same layer ids,
item ids (the file's uuids are the ids KiCad hands out), board name and project folder,
so a setup made in the KiCad plugin opens with the file and the other way round.

What the file cannot give is said in ``BoardSnapshot.assumed``. Zone fills are the main
one: KiPI does not fill zones itself, so a zone saved unfilled has no copper here.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kipi.core.geometry.copper import shapely_to_polys
from kipi.core.geometry.shapes import board_outline
from kipi.core.model.board import (
    BoardSnapshot, Footprint, Net, Pad, Poly, Stackup, Track, Via, Zone,
)
from kipi.core.model.layers import B_CU, F_CU, canonical_name, id_from_name
from kipi.core.model.stackup import (
    DEFAULT_INNER_CU_MM, DEFAULT_OUTER_CU_MM, build_stackup, combined_epsilon_r,
)
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.kicad_pcb import pads as padgeo, project as kproject
from kipi.sources.kicad_pcb.items import (
    BOARD, Frame, at, closed_shape, drawn_copper, drawn_items, kind, path_points, polyline,
    uuid,
)
from kipi.sources.kicad_pcb.sexpr import (
    Node, SexprError, atoms, child, children, flag, number, parse, value, xy,
)

#: The oldest file format read: KiCad 7.0's. Older files store arcs and footprints in
#: ways this reader does not handle.
OLDEST_FORMAT = 20221018

_COPPER_TYPES = ("signal", "power", "mixed", "jumper")


class KiCadFileError(ValueError):
    """The file is not a board KiPI can read; the message says why, for the user."""


# ---------------------------------------------------------------------------
# File and layers
# ---------------------------------------------------------------------------

def load_tree(path: Path) -> Node:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise KiCadFileError(f"{path.name} is not a KiCad board file (it is not text)") from e
    try:
        root = parse(text)
    except SexprError as e:
        raise KiCadFileError(f"{path.name} is not a KiCad board file ({e})") from e
    if not root or root[0] != "kicad_pcb":
        raise KiCadFileError(f"{path.name} is not a KiCad board file")
    return root


def file_version(root: Node) -> int:
    try:
        return int(value(root, "version", "0"))
    except ValueError:
        return 0


def check_version(root: Node, path: Path) -> int:
    version = file_version(root)
    if version < OLDEST_FORMAT:
        raise KiCadFileError(
            f"{path.name} was saved by KiCad 6 or older. Open it in KiCad 7 or later and "
            "save it, then open it here again.")
    return version


def copper_layers(root: Node) -> Dict[int, str]:
    """The board's copper layers, by id, with the names KiCad shows for them."""
    names: Dict[int, str] = {}
    layers = child(root, "layers")
    for entry in (layers[1:] if layers is not None else []):
        if type(entry) is not list or len(entry) < 3:
            continue
        canonical, ltype = entry[1], entry[2]
        lid = id_from_name(canonical)
        if lid is None or ltype not in _COPPER_TYPES:
            continue
        user = entry[3] if len(entry) > 3 and type(entry[3]) is str else ""
        names[lid] = user or canonical
    return names


def _sublayers(layer: Node) -> List[Node]:
    """A dielectric's sublayers: KiCad writes ``addsublayer`` between their settings."""
    groups: List[Node] = [["layer"]]
    for c in layer[2:]:
        if c == "addsublayer":
            groups.append(["layer"])
        elif type(c) is list:
            groups[-1].append(c)
    return groups


def read_stackup(root: Node, names: Dict[int, str], progress: ProgressReporter,
                 assumed: List[str]) -> Stackup:
    cu_ids = sorted(names, key=lambda lid: (lid == B_CU, lid))
    setup = child(root, "setup")
    stack = child(setup, "stackup") if setup is not None else None
    entries = []
    for layer in (children(stack, "layer") if stack is not None else []):
        if len(layer) < 2:
            continue
        name = layer[1]
        lid = id_from_name(name)
        if value(layer, "type") == "copper" and lid in names:
            default = DEFAULT_OUTER_CU_MM if lid in (F_CU, B_CU) else DEFAULT_INNER_CU_MM
            entries.append(("cu", lid, number(layer, "thickness", default), "", 0.0))
        elif name.startswith("dielectric"):
            subs = _sublayers(layer)
            t = sum(number(s, "thickness") for s in subs)
            er = combined_epsilon_r([(number(s, "thickness"), number(s, "epsilon_r"))
                                     for s in subs])
            entries.append(("diel", -1, t, value(subs[0], "material", "") or "", er))
    return build_stackup(entries, cu_ids, names, progress, assumed)


# ---------------------------------------------------------------------------
# Nets
# ---------------------------------------------------------------------------

class Nets:
    """Net references: KiCad 9 and older number nets in a table and refer to them by
    number; KiCad 10 refers to them by name."""

    def __init__(self, root: Node):
        self.table: Dict[str, str] = {}
        self.codes: Dict[str, int] = {}
        for n in children(root, "net"):
            if len(n) >= 3:
                self.table[n[1]] = n[2]
                try:
                    self.codes[n[2]] = int(n[1])
                except ValueError:
                    pass
        self.seen = set(self.table.values())

    def of(self, node: Node) -> str:
        n = child(node, "net")
        name = ""
        if n is not None and len(n) >= 2:
            if not self.table:
                name = n[1]
            elif len(n) >= 3:
                name = n[2]
            else:
                name = self.table.get(n[1], "")
        else:
            name = value(node, "net_name", "") or ""
        self.seen.add(name)
        return name

    def all(self, project: kproject.Project) -> List[Net]:
        names = sorted(self.seen | {""})
        codes = dict(self.codes)
        for name in names:                     # KiCad 10 files carry no numbers
            if name not in codes:
                codes[name] = 0 if name == "" else max(codes.values(), default=0) + 1
        return [Net(code=codes[n], name=n, netclass=project.netclass(n)) for n in names]


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _layer_id(node: Node) -> Optional[int]:
    name = value(node, "layer")
    return id_from_name(name) if name is not None else None


def read_tracks(root: Node, cu: Dict[int, str], nets: Nets) -> List[Track]:
    tracks = []
    for node in root:
        if type(node) is not list or not node or node[0] not in ("segment", "arc"):
            continue
        lid = _layer_id(node)
        if lid not in cu:
            continue
        mid = child(node, "mid") if node[0] == "arc" else None
        tracks.append(Track(net=nets.of(node), layer=lid, width_mm=number(node, "width"),
                            start=xy(child(node, "start")), end=xy(child(node, "end")),
                            mid=xy(mid) if mid is not None else None))
    return tracks


def _via_type(node: Node) -> str:
    words = set(atoms(node)) | {value(node, "type", "")}
    if "micro" in words:
        return "micro"
    if "buried" in words:
        return "buried"
    if "blind" in words:
        return "blind"
    return "through"


def _via_filled(node: Node) -> Optional[bool]:
    """``(filling yes|no)`` (KiCad 9+); None when the file leaves it to the design rules."""
    v = value(node, "filling", None)
    return None if v is None else v not in ("no", "false")


def _via_pads(node: Node, span: List[int], size: float) -> Dict[int, float]:
    """The via's pad diameter on each spanned layer, from its padstack (KiCad 9+): one
    size on every layer, front / inner / back, or each layer its own."""
    stack = child(node, "padstack")
    mode = value(stack, "mode", "normal") if stack is not None else "normal"
    sized = {}
    if stack is not None:
        for layer in children(stack, "layer"):
            if len(layer) > 1:
                sized[layer[1]] = number(layer, "size", size)
    out = {}
    for lid in span:
        name = canonical_name(lid)
        if mode == "custom":
            out[lid] = sized.get(name, size)
        elif mode == "front_inner_back" and lid != F_CU:
            out[lid] = sized.get("B.Cu", size) if lid == B_CU else sized.get("Inner", size)
        else:
            out[lid] = size
    return out


def read_vias(root: Node, stackup: Stackup, nets: Nets) -> Tuple[List[Via], List[Via]]:
    """The vias, and those set to remove their pads on unconnected layers."""
    all_cu = [c.id for c in stackup.copper]
    vias, trim = [], []
    for node in children(root, "via"):
        layers = [id_from_name(n) for n in atoms(child(node, "layers") or ["layers"])]
        layers = [lid for lid in layers if lid is not None]
        span = stackup.layers_between(layers[0], layers[-1]) if len(layers) >= 2 else []
        span = span or list(all_cu)
        drill = number(node, "drill")
        size = number(node, "size", drill * 2)
        pads = _via_pads(node, span, size)
        via = Via(id=uuid(node), net=nets.of(node), position=xy(child(node, "at")),
                  drill_mm=drill, diameter_mm=max(pads.values(), default=size), layers=span,
                  via_type=_via_type(node), filled=_via_filled(node), pad_mm=pads)
        vias.append(via)
        if flag(node, "remove_unused_layers"):
            trim.append((via, flag(node, "keep_end_layers")))
    return vias, trim


def trim_unused_via_pads(trim, tracks: List[Track], pads: List[Pad], zones: List[Zone],
                         outer: Tuple[int, int]) -> int:
    """Drop the pad of a "remove unused layers" via on each layer where nothing of its net
    meets it (no track end in it, no pad or zone fill of the net touching it); the barrel
    still runs through. Returns how many via pads were dropped."""
    if not trim:
        return 0
    from shapely.geometry import Point as SPoint
    from kipi.core.geometry.copper import poly_to_shapely
    ends = defaultdict(list)          # (net, layer) -> track end points and widths
    for t in tracks:
        ends[(t.net, t.layer)] += [(t.start, t.width_mm), (t.end, t.width_mm)]
    copper = defaultdict(list)        # (net, layer) -> pad copper and zone fills
    for p in pads:
        for lid, polys in p.shapes.items():
            copper[(p.net, lid)] += [g for g in map(poly_to_shapely, polys) if g is not None]
    for z in zones:
        for lid, polys in z.filled.items():
            copper[(z.net, lid)] += [g for g in map(poly_to_shapely, polys) if g is not None]
    dropped = 0
    for via, keep_ends in trim:
        for lid, d in list(via.pad_mm.items()):
            if (keep_ends and lid in outer) or not via.net:
                continue
            key = (via.net, lid)
            disc = SPoint(via.position).buffer(d / 2.0)
            x, y = via.position
            connected = (any((e[0] - x) ** 2 + (e[1] - y) ** 2 <= (d / 2.0 + w / 2.0) ** 2
                             for e, w in ends.get(key, ()))
                         or any(g.intersects(disc) for g in copper.get(key, ())))
            if not connected:
                del via.pad_mm[lid]
                dropped += 1
        if not via.pad_mm:
            via.diameter_mm = 0.0
    return dropped


def _is_rule_area(zone: Node) -> bool:
    return child(zone, "keepout") is not None or child(zone, "rule_area") is not None


def read_zones(root: Node, cu: Dict[int, str], nets: Nets,
               progress: ProgressReporter, assumed: List[str]) -> List[Zone]:
    zones = []
    unfilled = 0
    for node in children(root, "zone"):
        if _is_rule_area(node):
            continue
        filled: Dict[int, List[Poly]] = {}
        for fp in children(node, "filled_polygon"):
            lid = _layer_id(fp)
            if lid not in cu:
                continue
            # KiCad saves fills "fractured": each hole joined to the outline by a cut,
            # the same polygons its API returns.
            pts = polyline(child(fp, "pts"))
            if len(pts) >= 3:
                filled.setdefault(lid, []).append(Poly(outline=pts))
        if not filled:
            unfilled += 1
        zones.append(Zone(id=uuid(node), net=nets.of(node), name=value(node, "name", "") or "",
                          priority=int(number(node, "priority")), filled=filled))
    n_polys = sum(len(v) for z in zones for v in z.filled.values())
    progress.info(f"{len(zones)} zones, {n_polys} filled polygons")
    if unfilled:
        progress.warn(f"{unfilled} zones have no fill in the file; fill zones in KiCad (B) "
                      "and save the board for accurate results")
        assumed.append(f"Zones: {unfilled} zones were saved unfilled, so they have no copper "
                       "here. Fill the zones in KiCad (B), save, and reload the board")
    return zones


def _copper_shape_zones(items: List[Tuple[Node, Frame]], cu: Dict[int, str], nets: Nets,
                        progress: ProgressReporter) -> List[Zone]:
    """One zone per drawn shape on a copper layer that carries a net, as the KiCad
    source makes them."""
    zones = []
    no_net = 0
    for node, frame in items:
        lid = _layer_id(node)
        if lid not in cu:
            continue
        net = nets.of(node)
        if not net:
            no_net += 1
            continue
        polys = shapely_to_polys(drawn_copper(node, frame))
        if polys:
            zones.append(Zone(id=uuid(node), net=net, name="graphic shape", priority=0,
                              filled={lid: polys}))
    if zones:
        progress.info(f"{len(zones)} copper graphic shapes with a net")
    if no_net:
        progress.warn(f"{no_net} copper graphic shapes have no net and carry no current "
                      "in the model")
    return zones


def read_outline(root: Node, progress: ProgressReporter) -> List[Poly]:
    """The board outline from the board's own Edge.Cuts drawings."""
    from shapely.geometry import LineString
    lines, closed = [], []
    for node in drawn_items(root, "gr_"):
        if value(node, "layer") != "Edge.Cuts":
            continue
        if kind(node) in ("curve", "bezier"):
            continue                       # as the KiCad source: beziers do not close
        pts = path_points(node)
        if pts is not None:
            lines.append(LineString(pts))
            continue
        shape = closed_shape(node, quad_segs=32)
        if shape is not None and not shape.is_empty:
            closed.append(shape)
    return board_outline(lines, closed, progress)


# ---------------------------------------------------------------------------
# Footprints and pads
# ---------------------------------------------------------------------------

def _field(fp: Node, name: str) -> str:
    """A footprint field: ``(property "Reference" ..)`` since KiCad 8, ``(fp_text
    reference ..)`` before."""
    for p in children(fp, "property"):
        if len(p) > 2 and p[1] == name:
            return p[2]
    for t in children(fp, "fp_text"):
        if len(t) > 2 and t[1] == name.lower():
            return t[2]
    return ""


def _pad_layer_names(node: Node, cu: Dict[int, str]) -> List[str]:
    """The copper layers a pad is on, by canonical name."""
    out: List[str] = []
    all_cu = [canonical_name(lid) for lid in sorted(cu, key=lambda i: (i == B_CU, i))]
    for name in atoms(child(node, "layers") or ["layers"]):
        if name == "*.Cu":
            out.extend(all_cu)
        elif name == "F&B.Cu":
            out.extend(["F.Cu", "B.Cu"])
        elif id_from_name(name) in cu:
            out.append(name)
    return list(dict.fromkeys(n for n in out if n in all_cu))


class _PadToTrim:
    """A plated pad that drops its copper on layers nothing connects to on."""

    def __init__(self, pad: Pad, keep_ends: bool):
        self.pad = pad
        self.keep_ends = keep_ends


def read_footprints(root: Node, cu: Dict[int, str], nets: Nets, max_error: float,
                    progress: ProgressReporter):
    footprints: List[Footprint] = []
    pads: List[Pad] = []
    drawn: List[Tuple[Node, Frame]] = []
    trim: List[_PadToTrim] = []
    for fp in children(root, "footprint"):
        pos, angle = at(fp)
        fp_frame = Frame(pos, angle)
        ref = _field(fp, "Reference")
        fp_pads = []
        for node in children(fp, "pad"):
            local, pad_angle = at(node)
            position = fp_frame(local)
            frame = Frame(position, pad_angle)
            ptype = padgeo.pad_type(node)
            size, _, _ = padgeo.drill(node)
            shapes = padgeo.PadShapes(node)
            copper: Dict[int, List[Poly]] = {}
            for name in _pad_layer_names(node, cu):
                if ptype == "npth" and not padgeo.npth_has_copper(shapes, name, node):
                    continue
                polys = padgeo.copper(shapes, name, frame, max_error)
                if polys:
                    copper[id_from_name(name)] = polys
            pad = Pad(id=uuid(node), net=nets.of(node), footprint=ref,
                      number=node[1] if len(node) > 1 else "", position=position,
                      pad_type=ptype, drill_mm=size, shapes=copper, angle_deg=pad_angle)
            remove, keep_ends = padgeo.removes_unused_layers(node)
            if remove and ptype == "pth":
                trim.append(_PadToTrim(pad, keep_ends))
            fp_pads.append(pad)
        pads.extend(fp_pads)
        footprints.append(Footprint(
            id=uuid(fp), reference=ref, value=_field(fp, "Value"), position=pos,
            side="bottom" if value(fp, "layer") == "B.Cu" else "top",
            pad_ids=[p.id for p in fp_pads],
            footprint=fp[1] if len(fp) > 1 and type(fp[1]) is str else ""))
        drawn.extend((node, fp_frame) for node in drawn_items(fp, "fp_"))
    progress.info(f"{len(footprints)} footprints, {len(pads)} pads")
    return footprints, pads, drawn, trim


def trim_unused_layers(trim: List[_PadToTrim], tracks: List[Track], vias: List[Via],
                       zones: List[Zone], outer: Tuple[int, int]) -> int:
    """Drop the copper of "remove unused layers" pads on the layers where nothing of their
    net meets them: no track ends on the pad, no via of the net is in it, and no zone of
    the net touches it. KiCad's own connectivity decides this in the editor; this is the
    same test on the saved items. Returns how many pad layers were dropped."""
    if not trim:
        return 0
    from shapely.geometry import Point as SPoint
    from shapely.prepared import prep
    from kipi.core.geometry.copper import poly_to_shapely
    ends = defaultdict(list)          # (net, layer) -> track end points
    for t in tracks:
        ends[(t.net, t.layer)] += [t.start, t.end]
    via_at = defaultdict(list)        # (net, layer) -> via centers
    for v in vias:
        for lid in v.layers:
            via_at[(v.net, lid)].append(v.position)
    fills = defaultdict(list)         # (net, layer) -> zone fills
    for z in zones:
        for lid, polys in z.filled.items():
            fills[(z.net, lid)] += [g for g in map(poly_to_shapely, polys) if g is not None]
    dropped = 0
    for item in trim:
        pad = item.pad
        for lid in list(pad.shapes):
            if item.keep_ends and lid in outer:
                continue
            key = (pad.net, lid)
            geoms = [g for g in map(poly_to_shapely, pad.shapes[lid]) if g is not None]
            if not geoms or not pad.net:
                continue
            shape = geoms[0] if len(geoms) == 1 else geoms[0].union(geoms[1:])
            p = prep(shape)
            connected = (any(p.covers(SPoint(e)) for e in ends.get(key, ()))
                         or any(p.covers(SPoint(c)) for c in via_at.get(key, ()))
                         or any(p.intersects(f) for f in fills.get(key, ())))
            if not connected:
                del pad.shapes[lid]
                dropped += 1
    return dropped


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

def read_board(path, progress: ProgressReporter = NULL) -> BoardSnapshot:
    """Read the board file ``path``. Raises :class:`KiCadFileError` for a file KiPI cannot
    read, OSError if it cannot be opened."""
    path = Path(path).resolve()   # KiCad names the project folder in full
    t0 = time.perf_counter()
    with progress.timed(f"Reading {path.name}"):
        root = load_tree(path)
    version = check_version(root, path)
    proj = kproject.read(path)
    gen = value(root, "generator_version", "") or ""
    progress.info(f"KiCad board file, format {version}" + (f" (KiCad {gen})" if gen else ""))
    assumed: List[str] = []
    if proj.path is None:
        assumed.append(f"Net classes: no project file ({path.with_suffix('.kicad_pro').name}) "
                       "beside the board, so every net is in the Default class")
    progress.check_cancel()

    with progress.timed("Reading stackup"):
        cu = copper_layers(root)
        if not cu:
            raise KiCadFileError(f"{path.name} has no copper layers")
        stackup = read_stackup(root, cu, progress, assumed)
    nets = Nets(root)

    with progress.timed("Reading tracks"):
        tracks = read_tracks(root, cu, nets)
        progress.info(f"{len(tracks)} track segments ({sum(t.is_arc for t in tracks)} arcs)")
    progress.check_cancel()

    with progress.timed("Reading vias"):
        vias, via_trim = read_vias(root, stackup, nets)
        progress.info(f"{len(vias)} vias ({sum(1 for v in vias if v.filled)} filled)")
    progress.check_cancel()

    with progress.timed("Reading footprints and pads"):
        footprints, pads, fp_drawn, trim = read_footprints(root, cu, nets, proj.max_error_mm,
                                                           progress)
    progress.check_cancel()

    with progress.timed("Reading zone fills"):
        zones = read_zones(root, cu, nets, progress, assumed)

    with progress.timed("Reading copper graphic shapes"):
        board_drawn = [(node, BOARD) for node in drawn_items(root, "gr_")]
        zones.extend(_copper_shape_zones(board_drawn + fp_drawn, cu, nets, progress))

    if trim:
        with progress.timed("Removing unused pad layers"):
            n = trim_unused_layers(trim, tracks, vias, zones, (F_CU, B_CU))
            progress.info(f"{n} unconnected pad layers removed ({len(trim)} pads set to "
                          "remove unused layers)")

    if via_trim:
        with progress.timed("Removing unused via pads"):
            n = trim_unused_via_pads(via_trim, tracks, pads, zones, (F_CU, B_CU))
            progress.info(f"{n} unconnected via pads removed ({len(via_trim)} vias set to "
                          "remove unused layers)")

    with progress.timed("Reading board outline"):
        outline = read_outline(root, progress)

    snap = BoardSnapshot(
        board_name=path.name, project_dir=str(path.parent), stackup=stackup,
        nets=nets.all(proj), tracks=tracks, vias=vias, pads=pads, footprints=footprints,
        zones=zones, outline=outline, source="kicad file", kicad_version=gen,
        source_format="kicad-pcb", assumed=assumed)
    progress.info(f"{len(snap.nets)} nets")
    progress.info(f"Read in {time.perf_counter() - t0:.2f} s")
    return snap
