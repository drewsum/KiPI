"""Extracting a board from each KiCad version KiPI supports, KiCad 9.0.0 onwards.

The fake KiCad (``kicad_fake``) refuses the commands a version did not have yet, as the
real one does, and kipy's own ``Board`` parses every answer, so this also checks the
installed kicad-python release (CI runs it against the oldest one allowed too).
"""

import pytest

pytest.importorskip("kipy")

from kipy.errors import ApiError  # noqa: E402
from kipy.proto.board import board_types_pb2 as bt  # noqa: E402
from kipy.proto.board.board_types_pb2 import BoardLayer as L  # noqa: E402
from kipy.proto.common.envelope_pb2 import ApiStatusCode  # noqa: E402
from shapely.geometry import box  # noqa: E402

from kicad_fake import VERSIONS, FakeKiCad, parse  # noqa: E402
from kipi.core.geometry.copper import drill_holes  # noqa: E402
from kipi.core.kicad_io.extract import (  # noqa: E402
    _PAD_TYPES, _VIA_TYPES, extract_board, refill_and_wait,
)
from kipi.core.kicad_io.snapshot import from_dict, to_dict  # noqa: E402

COPPER = [L.BL_F_Cu, L.BL_In1_Cu, L.BL_In2_Cu, L.BL_B_Cu]


def _extract(version):
    return extract_board(FakeKiCad(version).board())


@pytest.fixture(scope="module", params=VERSIONS)
def extracted(request):
    return request.param, _extract(request.param)


def test_stackup(extracted):
    _, snap = extracted
    s = snap.stackup
    assert s.from_board
    assert [c.id for c in s.copper] == COPPER
    # In1.Cu keeps its user name whether KiCad can say it (9.0.8+) or only the stackup can.
    assert [c.name for c in s.copper] == ["F.Cu", "GND_PLANE", "In2.Cu", "B.Cu"]
    assert [round(c.thickness_mm * 1e4) for c in s.copper] == [350, 175, 175, 350]
    assert [(d.material, d.epsilon_r) for d in s.dielectrics] == \
        [("PP 2116", 4.3), ("Core FR4", 4.6), ("PP 2116", 4.3)]
    assert s.board_thickness_mm == pytest.approx(0.035 * 2 + 0.0175 * 2 + 1.465)


def test_nets_and_classes(extracted):
    version, snap = extracted
    assert sorted(n.name for n in snap.nets) == ["+3V3", "GND"]
    classes = {n.name: n.netclass for n in snap.nets}
    if parse(version) >= (9, 0, 1):
        assert classes == {"+3V3": "Power", "GND": "Default"}
    else:                                   # 9.0.0 cannot say: classes are a nicety
        assert classes == {"+3V3": "", "GND": ""}


def test_tracks_and_vias(extracted):
    _, snap = extracted
    assert len(snap.tracks) == 2
    straight, arc = sorted(snap.tracks, key=lambda t: t.is_arc)
    assert straight.width_mm == pytest.approx(0.5) and straight.end == pytest.approx((10, 0))
    assert arc.mid == pytest.approx((-2, 2))
    (via,) = snap.vias
    assert (via.net, via.via_type, via.layers) == ("+3V3", "through", COPPER)
    assert (via.drill_mm, via.diameter_mm) == pytest.approx((0.3, 0.6))


def test_footprints_and_pads(extracted):
    version, snap = extracted
    (fp,) = snap.footprints
    assert (fp.reference, fp.value, fp.side) == ("U1", "TPS62130", "top")
    pads = {p.number: p for p in snap.pads}
    assert pads["1"].pad_type == "smd" and set(pads["1"].shapes) == {L.BL_F_Cu}
    slot = pads["2"]
    assert (slot.pad_type, slot.footprint, slot.angle_deg) == ("pth", "U1", 90.0)
    assert slot.drill_mm == pytest.approx((2.0, 0.8))
    if parse(version) >= (9, 0, 3):
        # Copper only where KiCad keeps it ("remove unconnected" pads).
        assert set(slot.shapes) == {L.BL_F_Cu, L.BL_B_Cu}
    else:
        # Before 9.0.3 KiCad cannot say which layers a padstack is on: the pad is kept on
        # every layer it has a shape on. Documented in the README.
        assert set(slot.shapes) == set(COPPER)


def test_slot_turns_with_its_pad(extracted):
    _, snap = extracted
    hole = drill_holes(snap, "GND", L.BL_F_Cu, quad_segs=16).intersection(box(20, 3, 24, 7))
    x0, y0, x1, y1 = hole.bounds
    assert (x1 - x0, y1 - y0) == pytest.approx((0.8, 2.0), abs=0.01)


def test_zones_and_outline(extracted):
    _, snap = extracted
    (zone,) = snap.zones                    # the rule area is not copper
    assert (zone.net, zone.name, set(zone.filled)) == ("GND", "GND pour", {L.BL_In1_Cu})
    (outline,) = snap.outline
    assert len(outline.outline) == 4 and not outline.holes
    assert (snap.board_name, snap.project_dir) == ("test.kicad_pcb", "/projects/test")


def test_snapshot_round_trip(extracted):
    _, snap = extracted
    assert to_dict(from_dict(to_dict(snap))) == to_dict(snap)


def test_newer_kicads_give_the_same_board():
    """From 9.0.3 every version gives the same snapshot: the fallbacks are exact."""
    latest = to_dict(_extract(VERSIONS[-1]))
    for v in VERSIONS:
        if parse(v) >= (9, 0, 3):
            assert to_dict(_extract(v)) == latest, v


@pytest.mark.parametrize("version", VERSIONS)
def test_refill_waits_for_the_fill(version):
    kicad = FakeKiCad(version)
    refill_and_wait(kicad.board(), poll_s=0, grace_s=60)
    assert kicad.busy_polls == 0                       # it saw the fill end
    assert "RefillZones" in kicad.calls


def test_unexpected_errors_are_not_mistaken_for_an_old_kicad():
    class Broken(FakeKiCad):
        def _GetBoardEnabledLayers(self, cmd):
            raise ApiError("bad document", code=ApiStatusCode.AS_BAD_REQUEST)

    with pytest.raises(ApiError):
        extract_board(Broken().board())


def test_kicad_enum_values_match():
    """KiPI maps KiCad's via and pad types by number: the numbers must not move."""
    via = {"VT_THROUGH": "through", "VT_BLIND_BURIED": "blind_buried", "VT_MICRO": "micro",
           "VT_BLIND": "blind", "VT_BURIED": "buried"}      # the last two: KiCad 10
    for name, kind in via.items():
        if name in bt.ViaType.keys():
            assert _VIA_TYPES[bt.ViaType.Value(name)] == kind
    assert _PAD_TYPES == {bt.PT_PTH: "pth", bt.PT_SMD: "smd", bt.PT_EDGE_CONNECTOR: "edge",
                          bt.PT_NPTH: "npth"}


@pytest.mark.parametrize("version, warned", [("9.0.2", True), ("9.0.3", False),
                                             ("10.0.7", False)])
def test_old_kicad_is_named_in_the_log(version, warned):
    from types import SimpleNamespace as NS

    from kipi.core.kicad_io.extract import _warn_old_kicad

    said = []
    major, minor, patch = parse(version)
    _warn_old_kicad(NS(major=major, minor=minor, patch=patch), NS(warn=said.append))
    assert bool(said) == warned


def test_kicad_9_0_7_without_layer_names():
    """Reported on KiCad 9.0.7: "no handler available for request of type
    kiapi.board.commands.GetBoardLayerName". The names come from the stackup instead."""
    kicad = FakeKiCad("9.0.7")
    snap = extract_board(kicad.board())
    assert "GetBoardLayerName" in kicad.calls
    assert [c.name for c in snap.stackup.copper] == ["F.Cu", "GND_PLANE", "In2.Cu", "B.Cu"]


def test_unhandled_is_recognized_by_its_message_too():
    class OtherStatus(FakeKiCad):
        def send(self, cmd, response_type):
            try:
                return super().send(cmd, response_type)
            except ApiError as e:
                raise ApiError(str(e), code=ApiStatusCode.AS_BAD_REQUEST) from None

    snap = extract_board(OtherStatus("9.0.0").board())
    assert [c.name for c in snap.stackup.copper] == ["F.Cu", "GND_PLANE", "In2.Cu", "B.Cu"]
