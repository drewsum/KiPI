# Portions Copyright (c) 2026 Tomachie LLC.
"""Via fill and per-layer via pads, read from a ``.kicad_pcb`` file (#89)."""

import pytest
from shapely.geometry import box

from kipi.core.model.board import Pad, Track
from kipi.core.model.layers import B_CU, F_CU, inner_cu
from kipi.sources.kicad_pcb.reader import _via_filled, _via_pads, trim_unused_via_pads
from kipi.sources.kicad_pcb.sexpr import parse
from kipi.core.geometry.copper import shapely_to_polys

IN1, IN2 = inner_cu(1), inner_cu(2)
SPAN = [F_CU, IN1, IN2, B_CU]


def via(extra: str = ""):
    return parse(f'(via (at 0 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") {extra} '
                 '(net "N") (uuid "v1"))')


KICAD9, KICAD10 = 20241229, 20260206


@pytest.mark.parametrize("text,fmt,expect", [
    ("(filling yes)", KICAD10, True), ("(filling no)", KICAD10, False),
    ("", KICAD10, None),            # KiCad 10 writes (filling no); none = design rules
    ("(filling yes)", KICAD9, True),
    ("", KICAD9, False),            # KiCad 9 omits (filling no): the API says unfilled
])
def test_via_filling(text, fmt, expect):
    assert _via_filled(via(text), fmt) is expect


def test_normal_padstack_is_one_pad_on_every_layer():
    assert _via_pads(via(), SPAN, 0.6) == dict.fromkeys(SPAN, 0.6)


def test_front_inner_back_padstack():
    node = via('(padstack (mode front_inner_back) (layer "Inner" (size 0.45)) '
               '(layer "B.Cu" (size 0.5)))')
    assert _via_pads(node, SPAN, 0.6) == {F_CU: 0.6, IN1: 0.45, IN2: 0.45, B_CU: 0.5}


def test_custom_padstack_takes_each_layers_own_pad():
    node = via('(padstack (mode custom) (layer "In2.Cu" (size 0.4)) (layer "B.Cu" (size 0.55)))')
    assert _via_pads(node, SPAN, 0.6) == {F_CU: 0.6, IN1: 0.6, IN2: 0.4, B_CU: 0.55}


def _trim_one(keep_ends: bool):
    from kipi.core.model.board import Via
    v = Via("v1", "N", (0.0, 0.0), 0.3, 0.6, SPAN, pad_mm=dict.fromkeys(SPAN, 0.6))
    tracks = [Track(net="N", layer=F_CU, width_mm=0.2, start=(0.0, 0.0), end=(5.0, 0.0))]
    pads = [Pad(id="p", net="N", footprint="U1", number="1", position=(0.0, 0.0),
                pad_type="smd", drill_mm=(0.0, 0.0),
                shapes={IN2: shapely_to_polys(box(-0.5, -0.5, 0.5, 0.5))})]
    n = trim_unused_via_pads([(v, keep_ends)], tracks, pads, [], (F_CU, B_CU))
    return n, v


def test_unused_via_pads_are_removed_where_nothing_connects():
    n, v = _trim_one(keep_ends=False)
    assert n == 2 and set(v.pad_mm) == {F_CU, IN2}       # the track and the pad
    assert v.pad_diameter(IN1) == 0.0 and v.pad_diameter(F_CU) == 0.6


def test_keep_end_layers_keeps_the_outer_pads():
    n, v = _trim_one(keep_ends=True)
    assert n == 1 and set(v.pad_mm) == {F_CU, IN2, B_CU}


def _pair(gap_mm: float, keep_ends: bool):
    """Two through vias of one net, 0.6 mm pads and 0.3 mm drills, both set to remove
    unused layers, with nothing else on the board."""
    from kipi.core.model.board import Via
    a, b = (Via(id=i, net="N", position=(x, 0.0), drill_mm=0.3, diameter_mm=0.6,
                layers=list(SPAN), pad_mm=dict.fromkeys(SPAN, 0.6))
            for i, x in (("a", 0.0), ("b", gap_mm)))
    trim_unused_via_pads([(a, keep_ends), (b, keep_ends)], [], [], [], (F_CU, B_CU), [a, b])
    return a


def test_vias_whose_kept_outer_pads_overlap_keep_every_pad():
    # KiCad connects the two vias through their outer pads, and keeps a via's pad on
    # every layer a connected via spans.
    assert set(_pair(0.5, keep_ends=True).pad_mm) == set(SPAN)


def test_vias_whose_kept_outer_pads_only_touch_are_not_connected():
    assert set(_pair(0.6, keep_ends=True).pad_mm) == {F_CU, B_CU}


def test_removable_pads_do_not_connect_vias_only_their_drills_do():
    assert set(_pair(0.5, keep_ends=False).pad_mm) == set()
    assert set(_pair(0.2, keep_ends=False).pad_mm) == set(SPAN)
