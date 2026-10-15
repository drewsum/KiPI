"""Read a live KiCad 9 or 10 board through the IPC API (kicad-python / kipy) into a
:class:`~kipi.core.model.board.BoardSnapshot`.

All kipy-specific code lives here so the rest of KiPI never touches protobufs.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Iterable, List, Optional

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Footprint, Net, Pad, Poly, Stackup,
    Track, Via, Zone,
)
from kipi.core.kicad_io.convert import arc_points, mm, poly, pt, shapely_to_polys
from kipi.core.kicad_io.copper_shapes import copper_shape_zones
from kipi.core.progress import NULL, ProgressReporter

DEFAULT_BOARD_THICKNESS_MM = 1.6
DEFAULT_OUTER_CU_MM = 0.035
DEFAULT_INNER_CU_MM = 0.0175


class ExtractionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(timeout_ms: int = 5000, retries: int = 3, progress: ProgressReporter = NULL):
    """Connect to the running KiCad and return ``(kicad, board)``.

    When launched as a KiCad plugin, ``KICAD_API_SOCKET``/``KICAD_API_TOKEN`` are set
    and kipy picks them up automatically. Run standalone, kipy falls back to the
    default socket, which works when only one KiCad instance is open.
    KiPI refuses to connect while more than one is open (:func:`require_single_kicad`).
    """
    require_single_kicad()
    from kipy import KiCad
    from kipy.errors import ApiError

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            kicad = KiCad(timeout_ms=timeout_ms)
            version = kicad.get_version()
            progress.info(f"Connected to KiCad {version} "
                          f"(kicad-python built for {kicad.get_api_version()})")
            _warn_old_kicad(version, progress)
            board = kicad.get_board()
            return kicad, board
        except ApiError as e:   # e.g. AS_BUSY or no board open
            last_err = e
        except Exception as e:  # transport errors (KiCad not running / API disabled)
            last_err = e
        progress.warn(f"Connection attempt {attempt} failed: {last_err}")
        time.sleep(0.5 * attempt)
    raise ExtractionError(
        "Could not connect to KiCad. Make sure the PCB editor is open with a board loaded "
        "and that Preferences > Plugins > 'Enable KiCad API' is checked.\n"
        f"Last error: {last_err}")


def require_single_kicad() -> None:
    """Raise :class:`ExtractionError` if this user has more than one KiCad open."""
    from kipi.core.kicad_io.instances import multiple_kicads_message
    message = multiple_kicads_message()
    if message:
        raise ExtractionError(message)


def _warn_old_kicad(version, progress: ProgressReporter) -> None:
    """Say what a KiCad too old for some of KiPI's requests means for the results."""
    try:
        v = (int(version.major), int(version.minor), int(version.patch))
    except (AttributeError, TypeError, ValueError):
        return
    if v < (9, 0, 3):
        progress.warn(f"KiCad {v[0]}.{v[1]}.{v[2]} cannot say which layers plated pads have "
                      "copper on, so pads are kept on every layer. Update to KiCad 9.0.3 "
                      "or later for exact pad copper.")


# ---------------------------------------------------------------------------
# Stackup
# ---------------------------------------------------------------------------

def _unsupported(e: Exception) -> bool:
    """True if KiCad does not know the request: it is older than the command.

    KiCad answers such a request with ``AS_UNHANDLED`` and the message "no handler
    available for request of type ..." (``KICAD_API_SERVER::handleApiEvent``). Either is
    taken as the sign, in case a release reports the status differently.
    """
    from kipy.errors import ApiError
    from kipy.proto.common.envelope_pb2 import ApiStatusCode
    return isinstance(e, ApiError) and (e.code == ApiStatusCode.AS_UNHANDLED
                                        or "no handler available" in str(e))


def _reports_enabled(stackup_layers) -> bool:
    """KiCad 10.0.1 leaves every layer's enabled flag unset (false); an unset flag on
    every layer means the server does not report it, not that no layer is enabled."""
    return any(sl.enabled for sl in stackup_layers)


def _stackup_copper(stackup_layers) -> Dict[int, str]:
    """Enabled copper layers in the board's stackup, with the names shown in KiCad."""
    from kipy.proto.board import board_pb2
    reports_enabled = _reports_enabled(stackup_layers)
    return {sl.layer: sl.user_name for sl in stackup_layers
            if (sl.enabled or not reports_enabled)
            and sl.type == board_pb2.BoardStackupLayerType.BSLT_COPPER}


def _copper_layer_ids(board, stackup_layers=(), progress: ProgressReporter = NULL) -> List[int]:
    from kipy.util.board_layer import is_copper_layer
    from kipy.proto.board.board_types_pb2 import BoardLayer
    try:
        ids = [l for l in board.get_enabled_layers() if is_copper_layer(l)]
    except Exception as e:  # noqa: BLE001
        if not _unsupported(e):
            raise
        # KiCad before 9.0.5 cannot list the enabled layers: the stackup has the copper.
        ids = [l for l in _stackup_copper(stackup_layers) if is_copper_layer(l)]
        if not ids:
            progress.warn("This KiCad cannot list the board's layers; assuming 2 copper layers")
            ids = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
    # Stack order: F.Cu, In1..In30, B.Cu. Enum values are already in that order.
    return sorted(ids, key=lambda l: (l == BoardLayer.BL_B_Cu, l))


def _layer_names(board, cu_ids, stackup_layers) -> Dict[int, str]:
    """Each copper layer's name as KiCad shows it ("F.Cu", or the user's own name)."""
    from kipy.util.board_layer import CANONICAL_LAYER_NAMES
    from_stackup = _stackup_copper(stackup_layers)
    names = {}
    for l in cu_ids:
        try:
            name = board.get_layer_name(l)
        except Exception as e:  # noqa: BLE001
            if not _unsupported(e):
                raise
            name = ""            # KiCad before 9.0.8 has no GetBoardLayerName
        names[l] = name or from_stackup.get(l) or CANONICAL_LAYER_NAMES.get(l, str(l))
    return names


def extract_stackup(board, progress: ProgressReporter = NULL) -> Stackup:
    from kipy.proto.board import board_pb2

    try:
        stackup_layers = list(board.get_stackup().layers)
    except Exception as e:  # noqa: BLE001 - stackup is optional; fall back below
        progress.warn(f"Could not read board stackup ({e}); using defaults")
        stackup_layers = []
    cu_ids = _copper_layer_ids(board, stackup_layers, progress)
    names = _layer_names(board, cu_ids, stackup_layers)

    entries = []  # (kind, layer_id, thickness_mm, material, epsilon_r), top to bottom
    try:
        reports_enabled = _reports_enabled(stackup_layers)
        for sl in stackup_layers:
            if reports_enabled and not sl.enabled:
                continue
            if sl.type == board_pb2.BoardStackupLayerType.BSLT_COPPER and sl.layer in names:
                entries.append(("cu", sl.layer, mm(sl.thickness), "", 0.0))
            elif sl.type == board_pb2.BoardStackupLayerType.BSLT_DIELECTRIC:
                diel = sl.dielectric
                subs = diel.layers if diel is not None else []
                t = sum(mm(s.thickness) for s in subs) or mm(sl.thickness)
                mat = subs[0].material_name if subs else sl.material_name
                er = subs[0].epsilon_r if subs else 0.0
                entries.append(("diel", -1, t, mat, er))
    except Exception as e:  # noqa: BLE001 - stackup is optional; fall back below
        progress.warn(f"Could not read board stackup layers ({e}); using defaults")
        entries = []

    from_board = sum(1 for e in entries if e[0] == "cu") == len(cu_ids) and len(cu_ids) > 0
    if not from_board:
        progress.warn("Stackup incomplete; synthesizing a default "
                      f"{DEFAULT_BOARD_THICKNESS_MM} mm stackup")
        entries = []
        n = len(cu_ids)
        cu_t = [DEFAULT_OUTER_CU_MM if i in (0, n - 1) else DEFAULT_INNER_CU_MM for i in range(n)]
        diel_t = (DEFAULT_BOARD_THICKNESS_MM - sum(cu_t)) / max(1, n - 1)
        for i, lid in enumerate(cu_ids):
            entries.append(("cu", lid, cu_t[i], "", 0.0))
            if i < n - 1:
                entries.append(("diel", -1, diel_t, "FR4", 4.5))

    total = sum(e[2] for e in entries)
    z = total
    copper: List[CopperLayer] = []
    dielectrics: List[DielectricLayer] = []
    last_cu = -1
    pending_diel: List[DielectricLayer] = []
    for kind, lid, t, mat, er in entries:
        z -= t
        if kind == "cu":
            copper.append(CopperLayer(id=lid, name=names.get(lid, str(lid)), index=len(copper),
                                      thickness_mm=t, z_bottom_mm=z))
            for d in pending_diel:
                d.below_layer = lid
            pending_diel = []
            last_cu = lid
        else:
            d = DielectricLayer(thickness_mm=t, material=mat, epsilon_r=er, above_layer=last_cu)
            dielectrics.append(d)
            pending_diel.append(d)

    progress.info(f"Stackup: {len(copper)} copper layers, {total:.3f} mm total" +
                  ("" if from_board else " (default)"))
    for c in copper:
        progress.info(f"  {c.name:<8} t={c.thickness_mm * 1000:.1f} um  z={c.z_mid_mm:.3f} mm")
    return Stackup(copper=copper, dielectrics=dielectrics, board_thickness_mm=total,
                   from_board=from_board)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

_VIA_TYPES = {1: "through", 2: "blind_buried", 3: "micro", 4: "blind", 5: "buried"}
_PAD_TYPES = {1: "pth", 2: "smd", 3: "edge", 4: "npth"}


def _netclass_map(board, nets) -> Dict[str, str]:
    try:
        classes = board.get_netclass_for_nets(nets)
        return {name: nc.name for name, nc in classes.items()}
    except Exception:  # noqa: BLE001 - net classes are a nicety
        return {}


def _net_code(net, index: int) -> int:
    """KiCad's net code, which KiPI only records. kipy deprecates it (KiCad is dropping
    net codes from the API), so read the message itself and fall back to the list order."""
    try:
        return int(net.proto.code.value)
    except Exception:  # noqa: BLE001
        return index


def _field_text(field) -> str:
    try:
        return field.text.value
    except Exception:  # noqa: BLE001
        try:
            return field.text.as_text().value
        except Exception:  # noqa: BLE001
            return ""


def _pad_shapes(board, pads, layer_ids: Iterable[int], progress) -> Dict[str, Dict[int, List[Poly]]]:
    """Pad copper polygon per layer, keyed by pad id.

    Calls the GetPadShapeAsPolygon command directly: kipy's list helper drops pads
    with no shape, which misaligns its result with the input list.
    """
    from kipy.proto.board import board_commands_pb2

    out: Dict[str, Dict[int, List[Poly]]] = {}
    if not pads:
        return out
    presence = {}
    try:
        pres = board.check_padstack_presence_on_layers(pads, list(layer_ids))
        presence = {item.id.value: layers for item, layers in pres.items()}
    except Exception as e:  # noqa: BLE001 - older KiCad: assume present everywhere it has shape
        progress.warn(f"Padstack presence query failed ({e}); using raw pad shapes")

    for lid in layer_ids:
        cmd = board_commands_pb2.GetPadShapeAsPolygon()
        cmd.board.CopyFrom(board.document)
        cmd.layer = lid
        cmd.pads.extend([p.id for p in pads])
        resp = board.client.send(cmd, board_commands_pb2.PadShapeAsPolygonResponse)
        from kipy.geometry import PolygonWithHoles
        for pid, kpoly in zip(resp.pads, resp.polygons):
            if presence and not presence.get(pid.value, {}).get(lid, False):
                continue
            p = poly(PolygonWithHoles(kpoly))
            if len(p.outline) >= 3:
                out.setdefault(pid.value, {}).setdefault(lid, []).append(p)
    return out


def _pad_angle(pad) -> Optional[float]:
    """The pad's orientation on the board in degrees. KiCad keeps the padstack's angle
    absolute (the footprint's rotation is included); the drill size is unrotated."""
    try:
        return float(pad.padstack.angle.degrees)
    except Exception:  # noqa: BLE001 - older API: slots fall back to the pad's shape
        return None


def _via_layers(via, stackup: Stackup) -> List[int]:
    drill = via.padstack.drill
    a, b = drill.start_layer, drill.end_layer
    span = stackup.layers_between(a, b)
    if span:
        return span
    return [c.id for c in stackup.copper]


def _outline(board_shapes, progress) -> List[Poly]:
    """Board outline polygons from Edge.Cuts graphics (best effort)."""
    from kipy.board_types import BoardArc, BoardCircle, BoardPolygon, BoardRectangle, BoardSegment
    from kipy.proto.board.board_types_pb2 import BoardLayer
    try:
        from shapely.geometry import LineString, Point as SPoint, Polygon as SPolygon
        from shapely.ops import polygonize, unary_union
    except ImportError:
        return []

    lines, closed = [], []
    for s in board_shapes:
        if s.layer != BoardLayer.BL_Edge_Cuts:
            continue
        if isinstance(s, BoardSegment):
            lines.append(LineString([pt(s.start), pt(s.end)]))
        elif isinstance(s, BoardArc):
            st, md, en = pt(s.start), pt(s.mid), pt(s.end)
            lines.append(LineString([st] + arc_points(st, md, en)))
        elif isinstance(s, BoardCircle):
            c = pt(s.center)
            closed.append(SPoint(c).buffer(mm(s.radius()), quad_segs=32))
        elif isinstance(s, BoardRectangle):
            (x1, y1), (x2, y2) = pt(s.top_left), pt(s.bottom_right)
            closed.append(SPolygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)]))
        elif isinstance(s, BoardPolygon):
            for pwh in s.polygons:
                p = poly(pwh)
                closed.append(SPolygon(p.outline, p.holes))
    polys = list(polygonize(unary_union(lines))) if lines else []
    polys += closed
    if not polys:
        progress.warn("No closed board outline found on Edge.Cuts")
        return []
    # Even-odd: the largest polygon is the board, the others inside it are cutouts.
    polys.sort(key=lambda p: p.area, reverse=True)
    board_poly = polys[0]
    for p in polys[1:]:
        board_poly = board_poly.symmetric_difference(p)
    return shapely_to_polys(board_poly)


def _kicad_busy(board) -> bool:
    """True if the PCB editor cannot serve board requests right now.

    ``Ping`` is answered even while a zone fill runs, so it cannot tell when the fill
    is done. A cheap ``GetItems`` goes through the editor's busy check: it returns
    ``AS_BUSY`` while the fill runs, or times out if the fill blocks the event loop.

    Groups are asked for because a board has few of them. KiCad checks for busy before it
    looks at the type, so a KiCad that rejects groups (KiCad 9) is asked for text instead,
    and then for tracks, which every version serves.
    """
    from kipy.errors import ApiError
    from kipy.proto.common.envelope_pb2 import ApiStatusCode
    from kipy.proto.common.types import KiCadObjectType

    probes = (KiCadObjectType.KOT_PCB_GROUP, KiCadObjectType.KOT_PCB_TEXT,
              KiCadObjectType.KOT_PCB_TRACE)
    for i, kind in enumerate(probes):
        try:
            board.get_items(kind)
        except ApiError as e:
            if e.code == ApiStatusCode.AS_BUSY:
                return True
            if (e.code in (ApiStatusCode.AS_BAD_REQUEST, ApiStatusCode.AS_UNHANDLED)
                    and i < len(probes) - 1):
                continue
            raise
        except OSError:     # transport timeout (kipy raises ConnectionError)
            return True
        return False
    return False


def refill_and_wait(board, progress: ProgressReporter = NULL, timeout_s: float = 600.0,
                    grace_s: float = 3.0, poll_s: float = 0.25) -> None:
    """Refill all zones in KiCad and return once the fill has finished.

    KiCad only queues the refill when it acknowledges the request, so the editor is
    briefly idle before the fill starts. kipy's own ``refill_zones(block=True)`` polls
    with ``Ping``, which succeeds in that window (and during the fill), so it returned
    before the fill and the following reads hit ``AS_BUSY`` or timed out. Here we wait
    until we have seen the editor busy and then idle again, or, for a fill too quick
    to catch, until it has stayed idle for ``grace_s``.
    """
    board.refill_zones(block=False)
    t0 = time.perf_counter()
    seen_busy = False
    while True:
        progress.check_cancel()
        time.sleep(poll_s)
        elapsed = time.perf_counter() - t0
        if _kicad_busy(board):
            seen_busy = True
        elif seen_busy or elapsed >= grace_s:
            return
        if elapsed > timeout_s:
            raise ExtractionError(f"KiCad was still filling zones after {timeout_s:.0f} s")


def extract_board(board, refill_zones: bool = False,
                  progress: ProgressReporter = NULL) -> BoardSnapshot:
    """Pull everything KiPI needs from an open kipy ``Board``."""
    t0 = time.perf_counter()

    if refill_zones:
        with progress.timed("Refilling zones"):
            refill_and_wait(board, progress)

    with progress.timed("Reading stackup"):
        stackup = extract_stackup(board, progress)
    cu_ids = [c.id for c in stackup.copper]
    progress.check_cancel()

    with progress.timed("Reading nets"):
        knets = board.get_nets()
        nc = _netclass_map(board, knets)
        nets = [Net(code=_net_code(n, i), name=n.name, netclass=nc.get(n.name, ""))
                for i, n in enumerate(knets)]
        progress.info(f"{len(nets)} nets")
    progress.check_cancel()

    with progress.timed("Reading tracks"):
        tracks: List[Track] = []
        from kipy.board_types import ArcTrack
        for t in board.get_tracks():
            if t.layer not in cu_ids:
                continue
            tracks.append(Track(
                net=t.net.name, layer=t.layer, width_mm=mm(t.width),
                start=pt(t.start), end=pt(t.end),
                mid=pt(t.mid) if isinstance(t, ArcTrack) else None))
        n_arcs = sum(1 for t in tracks if t.is_arc)
        progress.info(f"{len(tracks)} track segments ({n_arcs} arcs)")
    progress.check_cancel()

    with progress.timed("Reading vias"):
        vias = []
        for v in board.get_vias():
            try:
                dia = mm(v.diameter)
            except ValueError:
                dia = mm(v.drill_diameter) * 2
            vias.append(Via(id=v.id.value, net=v.net.name, position=pt(v.position),
                            drill_mm=mm(v.drill_diameter), diameter_mm=dia,
                            layers=_via_layers(v, stackup),
                            via_type=_VIA_TYPES.get(v.type, "through")))
        progress.info(f"{len(vias)} vias")
    progress.check_cancel()

    with progress.timed("Reading footprints and pads"):
        footprints: List[Footprint] = []
        kpads = []
        fp_shapes = []
        pad_ref: Dict[str, str] = {}
        for fp in board.get_footprints():
            ref = _field_text(fp.reference_field)
            fpads = list(fp.definition.pads)
            from kipy.proto.board.board_types_pb2 import BoardLayer
            footprints.append(Footprint(
                id=fp.id.value, reference=ref, value=_field_text(fp.value_field),
                position=pt(fp.position),
                side="bottom" if fp.layer == BoardLayer.BL_B_Cu else "top",
                pad_ids=[p.id.value for p in fpads]))
            for p in fpads:
                pad_ref[p.id.value] = ref
            kpads.extend(fpads)
            fp_shapes.extend(fp.definition.shapes)
        progress.info(f"{len(footprints)} footprints, {len(kpads)} pads")
        progress.info("Querying pad copper shapes per layer...")
        shapes = _pad_shapes(board, kpads, cu_ids, progress)
        pads: List[Pad] = []
        for p in kpads:
            d = p.padstack.drill.diameter
            pads.append(Pad(
                id=p.id.value, net=p.net.name, footprint=pad_ref.get(p.id.value, ""),
                number=p.number, position=pt(p.position),
                pad_type=_PAD_TYPES.get(p.pad_type, "unknown"),
                drill_mm=(mm(d.x), mm(d.y)),
                shapes=shapes.get(p.id.value, {}),
                angle_deg=_pad_angle(p)))
    progress.check_cancel()

    with progress.timed("Reading zone fills"):
        zones: List[Zone] = []
        unfilled = 0
        for z in board.get_zones():
            if z.is_rule_area():
                continue
            net = z.net.name if z.net is not None else ""
            filled = {lid: [poly(p) for p in polys]
                      for lid, polys in z.filled_polygons.items() if lid in cu_ids}
            if not filled:
                unfilled += 1
            zones.append(Zone(id=z.id.value, net=net, name=z.name, priority=z.priority,
                              filled=filled))
        n_polys = sum(len(v) for z in zones for v in z.filled.values())
        progress.info(f"{len(zones)} zones, {n_polys} filled polygons")
        if unfilled:
            progress.warn(f"{unfilled} zones have no fill; refill zones (B) for accurate results")

    board_shapes = list(board.get_shapes())
    with progress.timed("Reading copper graphic shapes"):
        zones.extend(copper_shape_zones(board_shapes + fp_shapes, cu_ids, progress))

    with progress.timed("Reading board outline"):
        outline = _outline(board_shapes, progress)

    project_dir = ""
    try:
        project_dir = board.document.project.path
    except Exception:  # noqa: BLE001
        pass

    snap = BoardSnapshot(
        board_name=board.name, project_dir=project_dir, stackup=stackup, nets=nets,
        tracks=tracks, vias=vias, pads=pads, footprints=footprints, zones=zones,
        outline=outline, source="kicad")
    progress.info(f"Extraction complete in {time.perf_counter() - t0:.2f} s")
    return snap


def default_project_dir(snapshot: BoardSnapshot) -> str:
    return snapshot.project_dir or os.getcwd()
