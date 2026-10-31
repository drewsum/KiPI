"""Which of the running KiCad's footprint fields the silkscreen prints."""

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("kipy")

from kipy.board_types import Field  # noqa: E402
from kipy.proto.board import board_types_pb2 as b  # noqa: E402

from kipi.core.progress import NULL  # noqa: E402
from kipi.sources.kicad_ipc import silkscreen  # noqa: E402


def _field(value, field_visible, text_visible=True):
    f = Field()
    f.layer = b.BL_F_SilkS
    f.text.value = value
    f.visible = field_visible
    f.text.attributes.visible = text_visible
    return f


def _printed(monkeypatch, fields):
    drawn = []
    monkeypatch.setattr(silkscreen, "_text_shapes",
                        lambda board, items: drawn.extend(t.text.value for t in items) or [])
    board = NS(get_text=lambda: [])
    silkscreen.read_silkscreen(board, [], [NS(texts_and_fields=fields)], NULL, [])
    return drawn


def test_hidden_field_is_not_printed(monkeypatch):
    # KiCad 9.0.1 on: the field says it is hidden, its text's flag stays set.
    fields = [_field("R1", True), _field("eyJkb2N1bWVudCI6...", False)]
    assert _printed(monkeypatch, fields) == ["R1"]


def test_kicad_9_0_0_hides_fields_on_their_text(monkeypatch):
    # KiCad 9.0.0 reports no field visible on the field itself.
    fields = [_field("R1", False, True), _field("10k", False, False)]
    assert _printed(monkeypatch, fields) == ["R1"]
