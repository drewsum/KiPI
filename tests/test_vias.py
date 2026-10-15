"""Via pads per layer and drill filling, read from a KiCad padstack."""

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("kipy")

from kipy.proto.board import board_types_pb2 as b  # noqa: E402

from kipi.kicad_io.vias import _filled, _pad_sizes  # noqa: E402

NM = 1_000_000
F, IN1, IN2, B = b.BL_F_Cu, b.BL_In1_Cu, b.BL_In2_Cu, b.BL_B_Cu


def via(stack_type, sizes, filled=b.VDFM_UNKNOWN):
    layers = [NS(layer=l, size=NS(x=int(d * NM), y=int(d * NM))) for l, d in sizes.items()]
    drill = NS(proto=NS(filled=filled))
    return NS(padstack=NS(type=stack_type, copper_layers=layers, drill=drill))


def test_normal_padstack_is_one_pad_on_every_layer():
    assert _pad_sizes(via(b.PST_NORMAL, {F: 0.6}), [F, IN1, B]) == {F: 0.6, IN1: 0.6, B: 0.6}


def test_front_inner_back_padstack():
    v = via(b.PST_FRONT_INNER_BACK, {F: 0.6, IN1: 0.45, B: 0.5})
    assert _pad_sizes(v, [F, IN1, IN2, B]) == {F: 0.6, IN1: 0.45, IN2: 0.45, B: 0.5}


def test_custom_padstack_takes_each_layers_own_pad():
    v = via(b.PST_CUSTOM, {F: 0.6, IN2: 0.4, B: 0.55})
    assert _pad_sizes(v, [F, IN2, B]) == {F: 0.6, IN2: 0.4, B: 0.55}


@pytest.mark.parametrize("mode,expect", [(b.VDFM_FILLED, True), (b.VDFM_UNFILLED, False),
                                         (b.VDFM_FROM_DESIGN_RULES, None),
                                         (b.VDFM_UNKNOWN, None)])
def test_drill_filling(mode, expect):
    assert _filled(via(b.PST_NORMAL, {F: 0.6}, mode)) is expect
