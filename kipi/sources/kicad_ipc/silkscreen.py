"""The silkscreens' printing from the running KiCad (#82): the board's and footprints'
graphics on F.SilkS and B.SilkS, and their text, which KiCad turns into shapes itself
(``GetTextAsShapes``) so it is drawn in KiCad's own font."""

from __future__ import annotations

from typing import Dict, Iterable, List

from kipi.core.geometry import silkscreen as silk
from kipi.core.model.board import Poly
from kipi.core.progress import ProgressReporter
from kipi.sources.kicad_ipc.copper_shapes import shape_geometry


def _side(layer) -> str:
    from kipy.proto.board.board_types_pb2 import BoardLayer
    return silk.silk_side(BoardLayer.Name(layer)) if layer else None


def _shown(item) -> bool:
    """Whether a text item is drawn: a footprint field when it is visible (KiCad 9.0.1
    says so on the field, earlier on its text)."""
    from kipy.board_types import Field
    if isinstance(item, Field):
        return item.visible or item.text.attributes.visible
    return True


def _text_shapes(board, texts: List) -> List:
    """KiCad's own drawing of ``texts`` (free text, fields, text boxes), as shapely
    geometries."""
    from kipy.board_types import BoardTextBox, Field
    from kipy.common_types import CompoundShape
    from kipy.proto.common.commands import base_commands_pb2
    cmd = base_commands_pb2.GetTextAsShapes()
    for t in texts:
        inner = cmd.text.add()
        if isinstance(t, BoardTextBox):
            inner.textbox.CopyFrom(t.as_textbox().proto)
        else:
            inner.text.CopyFrom((t.text if isinstance(t, Field) else t).as_text().proto)
    reply = board.client.send(cmd, base_commands_pb2.GetTextAsShapesResponse)
    return [shape_geometry(s) for entry in reply.text_with_shapes
            for s in CompoundShape(entry.shapes).shapes]


def read_silkscreen(board, shapes: Iterable, footprints: Iterable,
                    progress: ProgressReporter, assumed: List[str]) -> Dict[str, List[Poly]]:
    """What each side's silkscreen prints: ``shapes`` (the board's and footprints'
    graphic shapes) and the text of the board and of ``footprints``."""
    parts: Dict[str, list] = {"top": [], "bottom": []}
    for s in shapes:
        side = _side(s.layer)
        if side:
            parts[side].append(shape_geometry(s))
    texts: Dict[str, list] = {"top": [], "bottom": []}
    for t in list(board.get_text()) + [t for fp in footprints for t in fp.texts_and_fields]:
        side = _side(t.layer)
        if side and _shown(t):
            texts[side].append(t)
    for side, items in texts.items():
        if not items:
            continue
        try:
            parts[side].extend(_text_shapes(board, items))
        except Exception as e:  # noqa: BLE001 - KiCad before 9 cannot draw text for us
            progress.warn(f"KiCad could not draw the silkscreen text ({e}); left out")
            assumed.append("Silkscreen: KiCad did not give its text as shapes, so only the "
                           "silkscreen's graphics are printed, not its text")
            break
    out = silk.merge(parts)
    for side, polys in out.items():
        progress.debug(f"  {side} silkscreen: {len(polys)} polygons")
    return out
