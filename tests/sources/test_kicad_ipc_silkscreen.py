"""The silkscreen text the running KiCad draws: which footprint fields are printed, how
much text is drawn, and its ink."""

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("kipy")

import shapely  # noqa: E402
from kipy.board_types import Field  # noqa: E402
from kipy.common_types import CompoundShape  # noqa: E402
from kipy.proto.board import board_types_pb2 as b  # noqa: E402
from kipy.proto.common.types import base_types_pb2  # noqa: E402

from kipi.core.progress import NULL  # noqa: E402
from kipi.sources.kicad_ipc import silkscreen  # noqa: E402
from kipi.sources.kicad_ipc.copper_shapes import shape_geometry  # noqa: E402


def _field(value, field_visible, text_visible=True):
    f = Field()
    f.layer = b.BL_F_SilkS
    f.text.value = value
    f.visible = field_visible
    f.text.attributes.visible = text_visible
    return f


def _glyphs(n, x0=0.0, width_nm=150_000):
    """A text KiCad drew in ``n`` strokes: a zigzag of joined segments, as a glyph's."""
    c = base_types_pb2.CompoundShape()
    for i in range(n):
        s = c.shapes.add()
        s.attributes.stroke.width.value_nm = width_nm
        s.segment.start.x_nm, s.segment.start.y_nm = int((x0 + i * 0.1) * 1e6), 0 if i % 2 else 200_000
        s.segment.end.x_nm, s.segment.end.y_nm = int((x0 + i * 0.1 + 0.1) * 1e6), 200_000 if i % 2 else 0
    return c


def _printed(monkeypatch, fields, strokes=None):
    drawn = []

    def text_shapes(board, items):
        drawn.extend(t.text.value for t in items)
        return [_glyphs(strokes(t) if strokes else 1) for t in items]
    monkeypatch.setattr(silkscreen, "_text_shapes", text_shapes)
    board = NS(get_text=lambda: [])
    assumed = []
    out = silkscreen.read_silkscreen(board, [], [NS(texts_and_fields=fields)], NULL, assumed)
    return drawn, out, assumed


def test_hidden_field_is_not_printed(monkeypatch):
    # KiCad 9.0.1 on: the field says it is hidden, its text's flag stays set.
    fields = [_field("R1", True), _field("eyJkb2N1bWVudCI6...", False)]
    assert _printed(monkeypatch, fields)[0] == ["R1"]


def test_kicad_9_0_0_hides_fields_on_their_text(monkeypatch):
    # KiCad 9.0.0 reports no field visible on the field itself.
    fields = [_field("R1", False, True), _field("10k", False, False)]
    assert _printed(monkeypatch, fields)[0] == ["R1"]


def test_the_largest_texts_are_left_out_past_the_budget():
    texts = ["R1", "huge", "C2", "big"]
    drawn = [_glyphs(n) for n in (10, 500, 20, 300)]
    kept, left_out = silkscreen._within_budget(texts, drawn, 400)
    assert left_out == ["huge"]
    assert [len(d.shapes) for d in kept] == [10, 20, 300]
    kept, left_out = silkscreen._within_budget(texts, drawn, 100)
    assert left_out == ["huge", "big"]


def test_text_past_the_budget_is_reported(monkeypatch):
    monkeypatch.setattr(silkscreen, "TEXT_STROKES", 100)
    fields = [_field("R1", True), _field("x" * 50, True)]
    _, out, assumed = _printed(monkeypatch, fields, strokes=lambda t: len(t.text.value) * 5)
    assert out["top"]
    assert any("largest texts are left out" in a for a in assumed)


def test_ink_matches_stroking_each_segment():
    drawn = [_glyphs(12), _glyphs(7, x0=5.0, width_nm=250_000)]
    fast = shapely.union_all(silkscreen._ink(drawn))
    each = shapely.union_all([shape_geometry(s) for d in drawn
                              for s in CompoundShape(d).shapes])
    assert fast.symmetric_difference(each).area < 1e-3 * each.area
