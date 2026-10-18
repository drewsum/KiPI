"""Vias from the KiCad API: type, drill, fill, the layers the barrel spans, and the pad
on each of those layers.

A KiCad 10 padstack can give a via a different pad per copper layer (front / inner /
back, or every layer its own), and can remove the pad on layers where nothing connects
to it. The barrel still runs through such a layer; only the pad copper is missing.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from kipi.core.model.board import Stackup, Via
from kipi.core.progress import ProgressReporter
from kipi.sources.kicad_ipc.convert import mm, pt


def via_type_names() -> Dict[int, str]:
    from kipy.proto.board import board_types_pb2 as b
    return {b.VT_THROUGH: "through", b.VT_BLIND_BURIED: "blind_buried", b.VT_MICRO: "micro",
            b.VT_BLIND: "blind", b.VT_BURIED: "buried"}


def _filled(via) -> Optional[bool]:
    """KiCad's filling of the via's drill; None when unknown or left to design rules."""
    from kipy.proto.board import board_types_pb2 as b
    return {b.VDFM_FILLED: True, b.VDFM_UNFILLED: False}.get(via.padstack.drill.proto.filled)


def _pad_sizes(via, layers: Iterable[int]) -> Dict[int, float]:
    """The via's pad diameter on each spanned layer, from its padstack."""
    from kipy.proto.board import board_types_pb2 as b
    stack = via.padstack
    by_layer = {cl.layer: mm(cl.size.x) for cl in stack.copper_layers}
    if not by_layer:
        return {}
    front = by_layer.get(b.BL_F_Cu, next(iter(by_layer.values())))
    inner = by_layer.get(b.BL_In1_Cu, front)
    out = {}
    for lid in layers:
        if stack.type == b.PST_CUSTOM and lid in by_layer:
            out[lid] = by_layer[lid]
        elif stack.type == b.PST_FRONT_INNER_BACK:
            out[lid] = by_layer.get(lid, inner) if lid in (b.BL_F_Cu, b.BL_B_Cu) else inner
        else:
            out[lid] = by_layer.get(lid, front)
    return out


def _via_layers(via, stackup: Stackup) -> List[int]:
    drill = via.padstack.drill
    span = stackup.layers_between(drill.start_layer, drill.end_layer)
    return span or [c.id for c in stackup.copper]


def read_vias(board, stackup: Stackup, progress: ProgressReporter) -> List[Via]:
    kvias = list(board.get_vias())
    cu_ids = [c.id for c in stackup.copper]
    presence = {}
    try:
        pres = board.check_padstack_presence_on_layers(kvias, cu_ids) if kvias else {}
        presence = {item.id.value: layers for item, layers in pres.items()}
    except Exception as e:  # noqa: BLE001 - older KiCad: pads on every spanned layer
        progress.warn(f"Via padstack presence query failed ({e}); a pad is assumed on every "
                      "layer each via spans")
    types = via_type_names()
    vias: List[Via] = []
    for v in kvias:
        layers = _via_layers(v, stackup)
        pads = _pad_sizes(v, layers)
        here = presence.get(v.id.value)
        if here is not None:
            pads = {lid: d for lid, d in pads.items() if here.get(lid, False)}
        dia = max(pads.values()) if pads else 0.0
        vias.append(Via(id=v.id.value, net=v.net.name, position=pt(v.position),
                        drill_mm=mm(v.drill_diameter), diameter_mm=dia, layers=layers,
                        via_type=types.get(v.type, "through"), filled=_filled(v),
                        pad_mm=pads))
    padless = sum(1 for v in vias if not v.pad_mm)
    if padless:
        progress.warn(f"{padless} via(s) with no pad in their padstack: barrel only")
    unknown = [v for v in kvias if v.type not in types]
    if unknown:
        progress.warn(f"{len(unknown)} via(s) of a type KiPI does not know; solved with the "
                      "through-via rule")
    filled = sum(1 for v in vias if v.filled)
    progress.info(f"{len(vias)} vias ({filled} filled)")
    return vias
