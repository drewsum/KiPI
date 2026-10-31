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


def _shown(item, field_flags: bool) -> bool:
    """Whether a text item is drawn: a footprint field when it is visible. KiCad 9.0.1
    on says so on the field and leaves its text's flag set (``field_flags``); 9.0.0
    says so only on the text."""
    from kipy.board_types import Field
    if isinstance(item, Field):
        return item.visible if field_flags else item.text.attributes.visible
    return True


#: Most text strokes KiPI draws on one side: merging them takes 3 to 5 s (some 2,000
#: designators are 60,000 strokes and 2 s). Past it, the largest texts are left out.
TEXT_STROKES = 100_000


def _text_shapes(board, texts: List) -> List:
    """KiCad's own drawing of ``texts`` (free text, fields, text boxes): each one's
    shapes, as KiCad's ``CompoundShape`` messages."""
    from kipy.board_types import BoardTextBox, Field
    from kipy.proto.common.commands import base_commands_pb2
    cmd = base_commands_pb2.GetTextAsShapes()
    for t in texts:
        inner = cmd.text.add()
        if isinstance(t, BoardTextBox):
            inner.textbox.CopyFrom(t.as_textbox().proto)
        else:
            inner.text.CopyFrom((t.text if isinstance(t, Field) else t).as_text().proto)
    reply = board.client.send(cmd, base_commands_pb2.GetTextAsShapesResponse)
    return [entry.shapes for entry in reply.text_with_shapes]


def _within_budget(texts: List, drawn: List, budget: int):
    """``drawn`` without its largest texts until it has at most ``budget`` shapes, and
    the texts left out."""
    sizes = [len(d.shapes) for d in drawn]
    total = sum(sizes)
    out = set()
    for i in sorted(range(len(drawn)), key=sizes.__getitem__, reverse=True):
        if total <= budget:
            break
        out.add(i)
        total -= sizes[i]
    return ([d for i, d in enumerate(drawn) if i not in out],
            [t for i, t in enumerate(texts) if i in out])


def _ink(drawn: List) -> List:
    """The ink of KiCad's text shapes as shapely geometries. Its stroke font draws in
    segments, read straight from the messages and stroked a glyph's stroke at a time."""
    from kipy.common_types import GraphicShape, to_concrete_shape
    from kipi.core.geometry.shapes import stroke_segments
    segments: Dict[int, list] = {}
    out = []
    for compound in drawn:
        for s in compound.shapes:
            if s.WhichOneof("geometry") == "segment":
                a, b = s.segment.start, s.segment.end
                segments.setdefault(s.attributes.stroke.width.value_nm, []).append(
                    ((a.x_nm / 1e6, a.y_nm / 1e6), (b.x_nm / 1e6, b.y_nm / 1e6)))
            else:
                shape = to_concrete_shape(GraphicShape(s))
                if shape is not None:
                    out.append(shape_geometry(shape))
    for width, segs in segments.items():
        out.extend(stroke_segments(segs, width / 1e6))
    return out


def _named(t) -> str:
    from kipy.board_types import Field
    text = (t.text if isinstance(t, Field) else t).value
    text = text if len(text) <= 24 else text[:21] + "..."
    return f"field {t.name} ({text!r})" if isinstance(t, Field) else repr(text)


def read_silkscreen(board, shapes: Iterable, footprints: Iterable,
                    progress: ProgressReporter, assumed: List[str]) -> Dict[str, List[Poly]]:
    """What each side's silkscreen prints: ``shapes`` (the board's and footprints'
    graphic shapes) and the text of the board and of ``footprints``."""
    parts: Dict[str, list] = {"top": [], "bottom": []}
    for s in shapes:
        side = _side(s.layer)
        if side:
            parts[side].append(shape_geometry(s))
    from kipy.board_types import Field
    items = list(board.get_text()) + [t for fp in footprints for t in fp.texts_and_fields]
    # KiCad 9.0.0 reports every field hidden on the field itself; later versions show
    # the references, so a board where any field is visible there has the field flags.
    field_flags = any(isinstance(t, Field) and t.visible for t in items)
    texts: Dict[str, list] = {"top": [], "bottom": []}
    for t in items:
        side = _side(t.layer)
        if side and _shown(t, field_flags):
            texts[side].append(t)
    for side, items in texts.items():
        if not items:
            continue
        try:
            drawn = _text_shapes(board, items)
        except Exception as e:  # noqa: BLE001 - KiCad before 9 cannot draw text for us
            progress.warn(f"KiCad could not draw the silkscreen text ({e}); left out")
            assumed.append("Silkscreen: KiCad did not give its text as shapes, so only the "
                           "silkscreen's graphics are printed, not its text")
            break
        drawn, left_out = _within_budget(items, drawn, TEXT_STROKES)
        if left_out:
            names = ", ".join(_named(t) for t in left_out[:3])
            more = f" and {len(left_out) - 3} more" if len(left_out) > 3 else ""
            progress.warn(f"The {side} silkscreen has more text than KiPI draws in good time; "
                          f"left out its {len(left_out)} largest: {names}{more}")
            assumed.append(f"Silkscreen: the {side} side's {len(left_out)} largest texts are "
                           "left out, as drawing them all would take minutes")
        parts[side].extend(_ink(drawn))
    out = silk.merge(parts)
    for side, polys in out.items():
        progress.debug(f"  {side} silkscreen: {len(polys)} polygons")
    return out
