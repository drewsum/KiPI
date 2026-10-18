"""Read an ODB++ job (a folder, ``.zip`` or ``.tgz``) into a
:class:`~kipi.core.model.board.BoardSnapshot`.

ODB++ gives the copper as it will be made: pads, traces and filled planes per layer,
which ``eda/data`` ties to nets and component pins. The stackup is read from the layer
matrix and each layer's attributes. Copper layers are numbered by their place in the
stack (:func:`~kipi.core.model.layers.copper_id`), as ODB++ names them freely.

ODB++ has no item ids. Pads are named by part and pin (``U1.3``), vias by where they are,
so ids stay the same when the board is exported again, and setups (which name parts, pins
and nets) carry over from KiCad.
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kipi.core.geometry.copper import shapely_to_polys
from kipi.core.model.board import (
    BoardSnapshot, Footprint, Net, Pad, Poly, Stackup, Track, Via, Zone,
)
from kipi.core.model.layers import B_CU, F_CU, canonical_name, copper_id
from kipi.core.model.stackup import DEFAULT_INNER_CU_MM, DEFAULT_OUTER_CU_MM, build_stackup
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.odbpp import eda as edamod, features as feat
from kipi.sources.odbpp.archive import Job, OdbError, job_name

#: KiCad's ODB++ export, and most others, give copper weight in oz per square foot.
MM_PER_OZ = 0.035
_COPPER_TYPES = ("SIGNAL", "POWER_GROUND", "MIXED")
# Where a pad and the hole drilled through it are taken to be the same place.
SAME_PLACE_MM = 0.002
# KiCad names the pins of pads with no number PAD0, PAD1, ... (NPTH0, ... for holes).
_KICAD_UNNUMBERED = re.compile(r"(PAD|NPTH)\d+$")


# ---------------------------------------------------------------------------
# Matrix, attributes and stackup
# ---------------------------------------------------------------------------

def parse_blocks(text: str, kind: str) -> List[Dict[str, str]]:
    """The ``KIND { KEY=VALUE ... }`` blocks of a matrix-style file."""
    out: List[Dict[str, str]] = []
    block: Optional[Dict[str, str]] = None
    for raw in text.splitlines():
        line = raw.strip()
        if block is None:
            if line.upper().startswith(kind) and line.endswith("{"):
                block = {}
        elif line == "}":
            out.append(block)
            block = None
        elif "=" in line:
            k, _, v = line.partition("=")
            block[k.strip().upper()] = v.strip()
    return out


def parse_attrs(text: Optional[str]) -> Dict[str, str]:
    """A ``.name=value`` attribute list (layer ``attrlist``, ``misc/info``)."""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip().lower()] = v.strip()
    return out


def _decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


@dataclass
class Layer:
    name: str               # as the matrix names it
    row: int
    type: str
    context: str
    start: str = ""         # drill layers: the copper layers the holes span
    end: str = ""
    dielectric_type: str = ""

    @property
    def folder(self) -> str:
        return self.name.lower()


def read_matrix(job: Job, step: str) -> List[Layer]:
    text = job.read("matrix/matrix")
    if text is None:
        raise OdbError("the job has no matrix/matrix file")
    layers = []
    for b in parse_blocks(text, "LAYER"):
        try:
            row = int(b.get("ROW", "0"))
        except ValueError:
            continue
        layers.append(Layer(name=b.get("NAME", ""), row=row, type=b.get("TYPE", "").upper(),
                            context=b.get("CONTEXT", "").upper(),
                            start=b.get("START_NAME", ""), end=b.get("END_NAME", ""),
                            dielectric_type=b.get("DIELECTRIC_TYPE", "")))
    layers.sort(key=lambda l: l.row)
    return layers


def is_panel(job: Job, step: str) -> bool:
    """A step that places other steps (a manufacturing panel), not a board of its own."""
    return "STEP-REPEAT" in (job.read(f"steps/{step}/stephdr") or "").upper()


def panel_placements(job: Job) -> Dict[str, int]:
    """How many times the job's panels place each step: its stephdr's STEP-REPEAT
    blocks, one ``NAME=`` each (SMPC5's panel: PCB1 three times, PCB2 once)."""
    counts: Dict[str, int] = {}
    for s in job.list("steps"):
        hdr = job.read(f"steps/{s}/stephdr") or ""
        if "STEP-REPEAT" not in hdr.upper():
            continue
        for line in hdr.splitlines():
            key, _, val = line.strip().partition("=")
            if key.upper() == "NAME" and val.strip():
                name = val.strip().lower()
                counts[name] = counts.get(name, 0) + 1
    return counts


def board_steps(job: Job) -> List[str]:
    """The steps that are boards, best first: not panels, and with a netlist where any has
    one. The step a panel places most comes first (the board the panel is made of); a
    step whose name says panel (Valor's "pcb+panel": the panel flattened into one step,
    with no step-and-repeat to show it) comes after the plain board steps."""
    steps = [s for s in job.list("steps") if not is_panel(job, s)]
    with_eda = [s for s in steps if job.exists(f"steps/{s}/eda/data")] or steps
    placed = panel_placements(job)
    return sorted(with_eda, key=lambda s: ("panel" in s, -placed.get(s, 0)))


def pick_step(job: Job, progress: ProgressReporter, wanted: Optional[str] = None,
              assumed: Optional[List[str]] = None) -> str:
    """The step holding the board: ``wanted`` if given, else the only board step, else
    one called pcb or board, else the first board step (and the others are said). A panel
    step, which places boards rather than being one, is never read for a board."""
    steps = job.list("steps")
    if not steps:
        raise OdbError("the job has no steps (no board in it)")
    if wanted:
        match = [s for s in steps if s == wanted.lower()]
        if not match:
            raise OdbError(f"the job has no step {wanted!r}; it has {', '.join(steps)}")
        return match[0]
    boards = board_steps(job) or steps
    named = [s for s in boards if s in ("pcb", "board")]
    step = (named or boards)[0]
    if len(steps) > 1:
        progress.info(f"The job has steps {', '.join(steps)}; reading {step}")
    others = [s for s in boards if s != step]
    if others and assumed is not None:
        placed = panel_placements(job)
        layout = ", ".join(f"{s} x{n}" for s, n in placed.items())
        assumed.append(f"Board: the job holds {len(boards)} board steps ({', '.join(boards)})"
                       + (f"; its panel places {layout}" if layout else "")
                       + f"; {step} is read. Open another by its step name")
    return step


def _copper_thickness(attrs: Dict[str, str]) -> Optional[float]:
    """A copper layer's thickness: its own if written precisely enough, else its weight."""
    t_text = attrs.get(".layer_dielectric") or attrs.get(".copper_thickness") or ""
    weight = attrs.get(".copper_weight")
    try:
        t = float(t_text) if t_text else None
    except ValueError:
        t = None
    # At the default precision (2 decimals) 35 um is written 0.03: the weight says more.
    if weight and (t is None or _decimals(t_text) <= 2):
        try:
            return float(weight) * MM_PER_OZ
        except ValueError:
            pass
    return t


def read_stackup(job: Job, step: str, layers: List[Layer], units: feat.Units,
                 progress: ProgressReporter, assumed: List[str]):
    """The stackup, and each copper layer's id by its folder name."""
    board = [l for l in layers if l.context == "BOARD"]
    copper = [l for l in board if l.type in _COPPER_TYPES]
    if not copper:
        raise OdbError("the job has no copper layers")
    n = len(copper)
    ids: Dict[str, int] = {}
    names: Dict[int, str] = {}
    for i, l in enumerate(copper):
        lid = copper_id(i, n)
        ids[l.folder] = lid
        canonical = canonical_name(lid)
        names[lid] = canonical if l.name.lower() == canonical.lower() else l.name

    entries = []
    missing_cu, missing_diel = [], []
    started = False
    for l in board:
        if l.type in _COPPER_TYPES:
            started = True
            attrs = parse_attrs(job.read(f"steps/{step}/layers/{l.folder}/attrlist"))
            lid = ids[l.folder]
            outer = lid in (F_CU, B_CU)
            t = _copper_thickness(attrs)
            if t is None or t <= 0:
                missing_cu.append(l.name)
                t = DEFAULT_OUTER_CU_MM if outer else DEFAULT_INNER_CU_MM
            else:
                t *= units.coord
            entries.append(("cu", lid, t, "", 0.0))
        elif l.type == "DIELECTRIC" and started:
            attrs = _dielectric_attrs(job, step, l)
            try:
                t = float(attrs.get(".layer_dielectric", "")) * units.coord
            except ValueError:
                t = None
            try:
                er = float(attrs.get(".dielectric_constant", "0"))
            except ValueError:
                er = 0.0
            if not t:
                missing_diel.append(l.name)
            entries.append(("diel", -1, t, attrs.get(".material", ""), er))
    # Dielectrics after the last copper layer (masks are not dielectrics here) go.
    while entries and entries[-1][0] == "diel":
        entries.pop()
    if missing_diel:
        known = [e[2] for e in entries if e[0] == "diel" and e[2]]
        fill = sum(known) / len(known) if known else 0.2
        entries = [(k, lid, t if t else fill, m, er) for k, lid, t, m, er in entries]
        assumed.append(f"Stackup: no thickness in the file for {', '.join(missing_diel)}, so "
                       f"{fill:.3f} mm is used for each (the mean of the other dielectrics)")
    if missing_cu:
        assumed.append(f"Stackup: no copper thickness in the file for {', '.join(missing_cu)}, "
                       "so 35 um outer / 17.5 um inner copper is used")
    has_diel = any(e[0] == "diel" for e in entries)
    if not has_diel and n > 1:
        entries = []                    # no dielectrics at all: the default stackup
    return build_stackup(entries, [copper_id(i, n) for i in range(n)], names, progress,
                         assumed), ids


def _dielectric_attrs(job: Job, step: str, layer: Layer) -> Dict[str, str]:
    """A dielectric's attributes. KiCad 10 names a dielectric's sublayers ``NAME_1``,
    ``NAME_2`` in the matrix but writes one folder, ``name``, holding the first sublayer's
    attributes; that folder stands for ``NAME_1``, and the later sublayers have none."""
    attrs = parse_attrs(job.read(f"steps/{step}/layers/{layer.folder}/attrlist"))
    if not attrs:
        base, _, sub = layer.folder.rpartition("_")
        if base and sub == "1":
            attrs = parse_attrs(job.read(f"steps/{step}/layers/{base}/attrlist"))
    return attrs


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

class _Board:
    """Everything read so far, while the snapshot is put together."""

    def __init__(self, job: Job, step: str, units: feat.Units, progress: ProgressReporter):
        self.job, self.step, self.units, self.progress = job, step, units, progress
        self.layers: Dict[str, feat.LayerFeatures] = {}
        self.mirror = True                  # mirror the symbols of mirrored pads
        self.kicad = False                  # written by KiCad, with its quirks

    def geometry(self, f: feat.Feature, folder: str):
        return feat.geometry(f, self.layers[folder].units, mirror=self.mirror)

    def features(self, folder: str) -> Optional[feat.LayerFeatures]:
        if folder not in self.layers:
            text = self.job.read(f"steps/{self.step}/layers/{folder}/features")
            self.layers[folder] = feat.parse(text, self.units) if text is not None else None
        return self.layers[folder]

    def feature(self, ref: Tuple[str, int]) -> Optional[feat.Feature]:
        lf = self.features(ref[0])
        if lf is None or not 0 <= ref[1] < len(lf.features):
            return None
        return lf.features[ref[1]]


def _hole_size(f: feat.Feature, lf: feat.LayerFeatures, pad_angle: float) -> Tuple[float, float]:
    """A drill's size in the pad's own frame (KiPI's ``Pad.drill_mm``)."""
    if f.kind in ("L", "A"):                       # a slot routed as a line
        d = feat.round_width(f.symbol, lf.units) or 0.0
        length = math.dist(f.start, f.end) + d
        along = math.degrees(math.atan2(-(f.end[1] - f.start[1]), f.end[0] - f.start[0]))
        turned = abs(((along - pad_angle) + 90) % 180 - 90) > 45
        return (d, length) if turned else (length, d)
    size = feat.symbol_size(f.symbol, lf.units) or (0.0, 0.0)
    turned = abs(((f.angle_deg - pad_angle) + 90) % 180 - 90) > 45
    return (size[1], size[0]) if turned else size


def read_board(path, progress: ProgressReporter = NULL,
               step: Optional[str] = None) -> BoardSnapshot:
    """Read the ODB++ job at ``path`` (its board ``step``, else the one picked by
    :func:`pick_step`). Raises :class:`OdbError` for a job KiPI cannot read."""
    path = Path(path).resolve()
    t0 = time.perf_counter()
    with Job(path) as job:
        return _read(job, path, progress, t0, step)


def _read(job: Job, path: Path, progress: ProgressReporter, t0: float,
          wanted_step: Optional[str] = None) -> BoardSnapshot:
    info = parse_attrs(job.read("misc/info"))
    units = feat.Units(info.get("units", "INCH"))
    app = info.get("save_app", "") or info.get("odb_source", "")
    progress.info(f"ODB++ {info.get('odb_version_major', '?')}.{info.get('odb_version_minor', '?')}"
                  + (f" from {app}" if app else ""))
    assumed: List[str] = []
    step = pick_step(job, progress, wanted_step, assumed)
    with progress.timed("Reading the job"):
        job.focus(step)
    layers = read_matrix(job, step)
    board = _Board(job, step, units, progress)
    board.kicad = "kicad" in app.lower()
    board.mirror = not board.kicad

    with progress.timed("Reading stackup"):
        stackup, cu_ids = read_stackup(job, step, layers, units, progress, assumed)
    progress.check_cancel()

    eda_text = job.read(f"steps/{step}/eda/data")
    if eda_text is None:
        raise OdbError("the job has no netlist (steps/<step>/eda/data), so its copper "
                       "cannot be tied to nets; export it with its netlist")
    eda = edamod.parse_eda(eda_text)

    with progress.timed("Reading copper layers"):
        for folder in cu_ids:
            board.features(folder)
    progress.check_cancel()

    used: Set[Tuple[str, int]] = set()      # copper features some subnet claims
    drills = {l.folder: l for l in layers if l.type == "DRILL"}

    # --- components and pads ------------------------------------------------------------
    with progress.timed("Reading footprints and pads"):
        comps: Dict[str, List[edamod.Component]] = {}
        for side, folder in (("T", "comp_+_top"), ("B", "comp_+_bot")):
            text = job.read(f"steps/{step}/layers/{folder}/components")
            comps[side] = edamod.parse_components(
                text, "top" if side == "T" else "bottom", units) if text else []
        pin_subnets: Dict[Tuple[str, int, int], edamod.Subnet] = {
            (s.side, s.component, s.toeprint): s for s in eda.subnets if s.kind == "TOP"}
        footprints: List[Footprint] = []
        pads: List[Pad] = []
        npth_holes = _holes(board, drills, plated=False)
        for side in ("T", "B"):
            for ci, comp in enumerate(comps[side]):
                fp_pads = []
                for ti, pin in enumerate(comp.pins):
                    sn = pin_subnets.get((side, ci, ti))
                    pad = _pad(board, comp, pin, ti, sn, cu_ids, drills, npth_holes, eda, used)
                    fp_pads.append(pad)
                pads.extend(fp_pads)
                footprints.append(Footprint(id=f"{comp.ref}", reference=comp.ref,
                                            value=comp.value, position=comp.at, side=comp.side,
                                            pad_ids=[p.id for p in fp_pads],
                                            footprint=comp.part))
        progress.info(f"{len(footprints)} footprints, {len(pads)} pads")
    progress.check_cancel()

    # --- vias, traces, planes -----------------------------------------------------------
    with progress.timed("Reading vias"):
        vias = _vias(board, eda, stackup, cu_ids, drills, used)
        progress.info(f"{len(vias)} vias")

    with progress.timed("Reading tracks and planes"):
        tracks: List[Track] = []
        zones: List[Zone] = []
        for k, sn in enumerate(eda.subnets):
            if sn.kind not in ("TRC", "PLN"):
                continue
            extra: Dict[int, list] = defaultdict(list)
            for ref in sn.copper:
                lid = cu_ids.get(ref[0])
                f = board.feature(ref)
                if lid is None or f is None:
                    continue
                used.add(ref)
                width = feat.round_width(f.symbol, board.layers[ref[0]].units) \
                    if f.kind in ("L", "A") else None
                if width is not None and f.positive:
                    mid = feat._arc_mid(f.start, f.end, f.center, f.clockwise) \
                        if f.kind == "A" else None
                    tracks.append(Track(net=sn.net, layer=lid, width_mm=width, start=f.start,
                                        end=f.end, mid=_round(mid) if mid else None))
                    continue
                g = board.geometry(f, ref[0])
                if g is not None and not g.is_empty:
                    extra[lid].append(g)
            if extra:
                zones.append(_zone(f"{sn.kind.lower()}{k}", sn.net, extra,
                                   "" if sn.kind == "PLN" else "copper"))
        n_arcs = sum(t.is_arc for t in tracks)
        progress.info(f"{len(tracks)} track segments ({n_arcs} arcs), {len(zones)} planes "
                      "and copper areas")

    with progress.timed("Reading copper with no net"):
        zones.extend(_loose_copper(board, cu_ids, used, tracks, vias, pads, zones, assumed))

    with progress.timed("Reading board outline"):
        outline = _outline(job, step, units)

    names = sorted(set(eda.nets) | {""})
    nets = [Net(code=i, name=n) for i, n in enumerate(names)]
    snap = BoardSnapshot(
        board_name=job_name(path), project_dir=str(path.parent), stackup=stackup,
        nets=nets, tracks=tracks, vias=vias, pads=pads, footprints=footprints, zones=zones,
        outline=outline, source="odb++", source_format="odbpp", assumed=assumed)
    progress.info(f"{len(nets)} nets")
    progress.info(f"Read in {time.perf_counter() - t0:.2f} s")
    return snap


def _round(p) -> tuple:
    return (round(p[0], feat.NM), round(p[1], feat.NM))


def _zone(zid: str, net: str, by_layer: Dict[int, list], name: str) -> Zone:
    from shapely.ops import unary_union
    filled = {lid: shapely_to_polys(unary_union(gs)) for lid, gs in by_layer.items()}
    return Zone(id=zid, net=net, name=name, priority=0,
                filled={lid: polys for lid, polys in filled.items() if polys})


def _holes(board: _Board, drills: Dict[str, Layer], plated: bool):
    """Every hole of the plated (or non-plated) drill layers: (position, feature, layer)."""
    out = []
    for folder in drills:
        lf = board.features(folder)
        if lf is None:
            continue
        for f in lf.features:
            kind = lf.attr(f, ".drill")
            is_plated = (kind or "").upper() not in ("NON_PLATED", "1")
            if kind is None:
                is_plated = "non" not in folder
            if is_plated == plated and f.kind in ("P", "L", "A"):
                at = f.at if f.kind == "P" else ((f.start[0] + f.end[0]) / 2,
                                                 (f.start[1] + f.end[1]) / 2)
                out.append((at, f, lf))
    return out


def _pad(board: _Board, comp, pin, index: int, sn: Optional[edamod.Subnet],
         cu_ids: Dict[str, int], drills: Dict[str, Layer], npth_holes, eda, used) -> Pad:
    shapes: Dict[int, List[Poly]] = {}
    hole: Optional[Tuple[feat.Feature, feat.LayerFeatures]] = None
    plated = False
    if sn is not None:
        for ref in sn.copper:
            lid = cu_ids.get(ref[0])
            f = board.feature(ref)
            if lid is None or f is None:
                continue
            used.add(ref)
            polys = shapely_to_polys(board.geometry(f, ref[0]))
            polys = [Poly([_round(p) for p in q.outline], [[_round(p) for p in h] for h in q.holes])
                     for q in polys]
            if polys:
                shapes.setdefault(lid, []).extend(polys)
        for ref in sn.holes:
            f = board.feature(ref)
            if f is not None:
                hole, plated = (f, board.layers[ref[0]]), True
                break
    if hole is None:
        for at, f, lf in npth_holes:
            if math.dist(at, pin.at) <= SAME_PLACE_MM:
                hole = (f, lf)
                break
    if hole is not None:
        ptype = "pth" if plated else "npth"
        drill = _hole_size(hole[0], hole[1], pin.angle_deg)
    else:
        ptype, drill = "smd", (0.0, 0.0)
    net = eda.nets[pin.net_index] if 0 <= pin.net_index < len(eda.nets) else ""
    if sn is not None and sn.net:
        net = sn.net
    number = pin.name
    if board.kicad and _KICAD_UNNUMBERED.match(number):
        number = ""
    return Pad(id=f"{comp.ref}#{index}", net=net, footprint=comp.ref, number=number,
               position=pin.at, pad_type=ptype, drill_mm=(round(drill[0], feat.NM),
                                                          round(drill[1], feat.NM)),
               shapes=shapes, angle_deg=pin.angle_deg)


def _vias(board: _Board, eda, stackup: Stackup, cu_ids: Dict[str, int],
          drills: Dict[str, Layer], used) -> List[Via]:
    all_cu = [c.id for c in stackup.copper]
    outer = {all_cu[0], all_cu[-1]}
    by_folder = {f: lid for f, lid in cu_ids.items()}
    vias: List[Via] = []
    seen: Set[Tuple[str, int]] = set()
    for sn in eda.subnets:
        if sn.kind != "VIA":
            continue
        for ref in sn.copper:
            used.add(ref)
        for href in sn.holes:
            if href in seen or href[0] not in drills:
                continue
            seen.add(href)
            f = board.feature(href)
            if f is None or f.kind != "P":
                continue
            lf = board.layers[href[0]]
            drill = (feat.symbol_size(f.symbol, lf.units) or (0.0, 0.0))[0]
            layer = drills[href[0]]
            a = by_folder.get(layer.start.lower())
            b = by_folder.get(layer.end.lower())
            span = stackup.layers_between(a, b) if a is not None and b is not None else []
            span = span or list(all_cu)
            # The via's pad on each copper layer: a layer with none has its pad removed.
            pad_mm: Dict[int, float] = {}
            for ref in sn.copper:
                cf = board.feature(ref)
                lid = cu_ids.get(ref[0])
                if (cf is not None and lid in span and cf.kind == "P"
                        and math.dist(cf.at, f.at) <= SAME_PLACE_MM):
                    size = feat.symbol_size(cf.symbol, board.layers[ref[0]].units)
                    if size:
                        pad_mm[lid] = max(pad_mm.get(lid, 0.0), size[0])
            diameter = max(pad_mm.values(), default=0.0)
            if span == all_cu:
                vtype = "through"
            elif span[0] in outer or span[-1] in outer:
                vtype = "blind"
            else:
                vtype = "buried"
            vias.append(Via(id=f"via@{f.at[0]:.4f},{f.at[1]:.4f}/{layer.folder}", net=sn.net,
                            position=f.at, drill_mm=drill, diameter_mm=diameter or drill * 2,
                            layers=span, via_type=vtype, pad_mm=pad_mm))
    return vias


def _loose_copper(board: _Board, cu_ids, used, tracks, vias, pads, zones,
                  assumed: List[str]) -> List[Zone]:
    """Copper no subnet claims (drawn copper; many exporters leave its net out). Each
    piece takes the net of the copper it touches, if exactly one net's; otherwise it
    carries no current, as a copper drawing with no net in KiCad."""
    from shapely.geometry import LineString, Point as SPoint
    from shapely.strtree import STRtree
    loose: Dict[int, List[Tuple[str, object]]] = defaultdict(list)
    for folder, lid in cu_ids.items():
        lf = board.layers.get(folder)
        if lf is None:
            continue
        for i, f in enumerate(lf.features):
            if (folder, i) in used or f.kind not in ("P", "L", "A", "S"):
                continue
            g = board.geometry(f, folder)
            if g is not None and not g.is_empty:
                loose[lid].append((f"{folder}#{i}", g))
    if not loose:
        return []

    # Copper with a known net, per layer.
    known: Dict[int, List[Tuple[object, str]]] = defaultdict(list)
    for t in tracks:
        known[t.layer].append((LineString([t.start, t.end]).buffer(t.width_mm / 2), t.net))
    for v in vias:
        for lid in v.layers:
            known[lid].append((SPoint(v.position).buffer(v.diameter_mm / 2), v.net))
    from kipi.core.geometry.copper import poly_to_shapely
    for p in pads:
        for lid, polys in p.shapes.items():
            known[lid] += [(poly_to_shapely(q), p.net) for q in polys]
    for z in zones:
        for lid, polys in z.filled.items():
            known[lid] += [(poly_to_shapely(q), z.net) for q in polys]

    out: List[Zone] = []
    guessed = no_net = 0
    for lid, items in loose.items():
        pieces = [(g, n) for g, n in known.get(lid, []) if g is not None and n]
        tree = STRtree([g for g, _ in pieces]) if pieces else None
        for zid, g in items:
            nets = set()
            if tree is not None:
                for i in tree.query(g, predicate="intersects"):
                    nets.add(pieces[int(i)][1])
            if len(nets) == 1:
                guessed += 1
                out.append(_zone(zid, nets.pop(), {lid: [g]}, "graphic shape"))
            else:
                no_net += 1
    if guessed:
        assumed.append(f"Copper drawings: {guessed} pieces of copper have no net in the file "
                       "and take the net of the copper they touch")
    if no_net:
        board.progress.warn(f"{no_net} pieces of copper have no net and carry no current in "
                            "the model")
    return out


def _outline(job: Job, step: str, units: feat.Units) -> List[Poly]:
    """The board outline from the step's profile."""
    text = job.read(f"steps/{step}/profile")
    if text is None:
        return []
    lf = feat.parse(text, units)
    from shapely.ops import unary_union
    parts = [feat.geometry(f, lf.units) for f in lf.features if f.kind == "S"]
    parts = [g for g in parts if g is not None and not g.is_empty]
    return shapely_to_polys(unary_union(parts)) if parts else []

