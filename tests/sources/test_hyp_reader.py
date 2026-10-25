"""The HyperLynx (``.hyp``) reader.

``tests/data/hyp/small.hyp`` is written by hand in the form KiCad 10's HyperLynx export
writes (checked against its export of a 1300-pad, 8-layer board, whose stackup, tracks,
vias, zones and outline the reader gives as KiCad's board file does), with one of each
thing the reader handles. Its values are round inches, so the expected values are worked
out by hand.
"""

import math
from pathlib import Path

import pytest

from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.model.layers import B_CU, F_CU
from kipi.sources.hyp.parse import HypError, parse
from kipi.sources.hyp.reader import read_board

DATA = Path(__file__).parents[1] / "data" / "hyp"
SMALL = DATA / "small.hyp"
IN = 25.4
IN1, IN2 = F_CU + 1, F_CU + 2


@pytest.fixture(scope="module")
def snap():
    return read_board(SMALL)


def _area(polys) -> float:
    return sum(poly_to_shapely(p).area for p in polys)


def _pad(snap, ref, number):
    return next(p for p in snap.pads if p.footprint == ref and p.number == number)


def test_identity(snap):
    assert snap.board_name == "small" and snap.source_format == "hyp"
    assert snap.format_name == "HyperLynx"
    assert Path(snap.project_dir) == DATA.resolve()


def test_stackup(snap):
    st = snap.stackup
    assert [(c.id, c.name) for c in st.copper] == [
        (F_CU, "F.Cu"), (IN1, "GND_PLANE"), (IN2, "In2.Cu"), (B_CU, "B.Cu")]
    assert [c.thickness_mm for c in st.copper] == pytest.approx([0.035, 0.0175, 0.0175, 0.035],
                                                                abs=1e-6)
    assert [d.thickness_mm for d in st.dielectrics] == pytest.approx([0.1, 1.2, 0.1], abs=1e-6)
    assert [d.epsilon_r for d in st.dielectrics] == [4.1, 4.6, 4.1]
    assert [d.material for d in st.dielectrics] == ["Prepreg", "FR4 core", "Prepreg"]
    # The masks outside the copper are coatings, not dielectrics of the board.
    assert [(c.kind, c.side) for c in st.coatings] == [("mask", "top"), ("mask", "bottom")]
    assert st.from_board


def test_nets(snap):
    # KiCad's EmptyNet0 (a pad with no net) is no net.
    assert [n.name for n in snap.nets] == ["", "+5V", "GND", "Net-(U1-Pad1)", "SIG"]


def test_pads(snap):
    r1 = _pad(snap, "R1", "1")
    assert r1.net == "+5V" and r1.pad_type == "smd" and r1.position == (0.5 * IN, 0.5 * IN)
    # A 0.04 x 0.02 in rectangle at 90 degrees: tall on the board, as KiCad shows it.
    assert list(r1.shapes) == [F_CU] and r1.angle_deg == 90.0
    x0, y0, x1, y1 = poly_to_shapely(r1.shapes[F_CU][0]).bounds
    assert (x1 - x0, y1 - y0) == pytest.approx((0.02 * IN, 0.04 * IN))

    j1 = _pad(snap, "J1", "1")       # through, round, its anti-pad left out
    assert j1.pad_type == "pth" and j1.drill_mm == pytest.approx((0.04 * IN, 0.04 * IN))
    assert sorted(j1.shapes) == [F_CU, IN1, IN2, B_CU]
    assert _area(j1.shapes[IN2]) == pytest.approx(math.pi * (0.035 * IN) ** 2, rel=0.01)

    c1 = _pad(snap, "C1", "1")       # an oblong on the bottom: KiCad's rounded rectangle
    assert list(c1.shapes) == [B_CU] and c1.angle_deg == 0.0
    w, h, r = 0.04 * IN, 0.02 * IN, 0.005 * IN
    assert _area(c1.shapes[B_CU]) == pytest.approx(w * h - (4 - math.pi) * r * r, rel=0.01)

    u1 = _pad(snap, "U1", "1")       # an oval: round ends
    w, h = 0.06 * IN, 0.02 * IN
    assert _area(u1.shapes[F_CU]) == pytest.approx((w - h) * h + math.pi * (h / 2) ** 2,
                                                   rel=0.01)

    mh = _pad(snap, "MH1", "1")      # a drill and no copper
    assert mh.pad_type == "npth" and mh.net == "" and not mh.shapes


def test_a_missing_pad_stack_is_said(snap):
    u1 = _pad(snap, "U1", "2")
    assert u1.net == "GND" and not u1.shapes


def test_pad_ids_follow_pin_order(snap):
    assert [p.id for p in snap.pads if p.footprint == "U1"] == ["U1#0", "U1#1"]
    assert [p.number for p in snap.pads if p.footprint == "U1"] == ["1", "2"]


def test_footprints(snap):
    fps = {f.reference: f for f in snap.footprints}
    assert sorted(fps) == ["C1", "J1", "MH1", "R1", "U1"]        # LOGO1 has no pads
    assert fps["R1"].position == pytest.approx((0.55 * IN, 0.5 * IN))
    assert fps["C1"].side == "bottom" and fps["R1"].side == "top"
    assert (fps["C1"].value, fps["C1"].footprint) == ("100n", "C_0402")
    assert any(a.startswith("Footprints:") for a in snap.assumed)


def test_vias(snap):
    through, blind = sorted(snap.vias, key=lambda v: v.position)
    assert through.layers == [F_CU, IN1, IN2, B_CU] and through.via_type == "through"
    assert through.drill_mm == pytest.approx(0.012 * IN)
    assert through.diameter_mm == pytest.approx(0.024 * IN)
    assert blind.layers == [F_CU, IN1] and blind.via_type == "blind"
    assert blind.pad_mm == pytest.approx({F_CU: 0.016 * IN, IN1: 0.018 * IN})
    assert through.net == blind.net == "+5V"


def test_tracks(snap):
    seg, arc = sorted(snap.tracks, key=lambda t: t.is_arc)
    assert (seg.layer, seg.start, seg.end) == (F_CU, (0.5 * IN, 0.5 * IN), (1.0 * IN, 0.5 * IN))
    assert seg.width_mm == pytest.approx(0.01 * IN)
    # Counterclockwise (y up) from the left of its centre round the bottom to its top: the
    # long way, through the point 315 degrees round.
    assert arc.layer == IN2 and arc.is_arc
    a = math.radians(315)
    mid = (1.2 + 0.2 * math.cos(a), -(-0.5 + 0.2 * math.sin(a)))
    assert arc.mid == pytest.approx((mid[0] * IN, mid[1] * IN), abs=1e-6)


def test_copper_polygons(snap):
    by = {(z.net, z.name, lid): polys for z in snap.zones for lid, polys in z.filled.items()}
    # A rectangle with a round bottom (a CURVE followed backwards) less a square void.
    pour = by[("+5V", "", F_CU)]
    want = 0.4 * 0.2 + math.pi * 0.2 ** 2 / 2 - 0.2 * 0.1
    assert _area(pour) == pytest.approx(want * IN * IN, rel=0.005)
    assert len(pour) == 1 and len(pour[0].holes) == 1
    line = by[("+5V", "copper", B_CU)]           # a POLYLINE: drawn with its width
    assert _area(line) == pytest.approx((0.6 * 0.02 + math.pi * 0.01 ** 2) * IN * IN, rel=0.01)
    assert _area(by[("GND", "", IN1)]) == pytest.approx(1.8 * 0.8 * IN * IN)
    assert _area(by[("GND", "copper", B_CU)]) == pytest.approx(0.04 * IN * IN)


def test_outline(snap):
    # A 2 x 1 in rectangle with a round bottom (radius 1 in), less a 0.2 in square cutout.
    assert len(snap.outline) == 1 and len(snap.outline[0].holes) == 1
    want = 2.0 + math.pi / 2 - 0.04
    assert _area(snap.outline) == pytest.approx(want * IN * IN, rel=0.005)
    ys = [p[1] for p in snap.outline[0].outline]
    assert max(ys) == pytest.approx(2.0 * IN, abs=1e-3)      # the arc bulges down (y up)


def test_metric_units_and_copper_weight(tmp_path):
    f = tmp_path / "metric.hyp"
    f.write_text("""{VERSION=2.14}
{UNITS=METRIC WEIGHT}
{STACKUP
  (SIGNAL T=0.305 L="TOP")
  (DIELECTRIC T=0.0015 C=4.5 L="CORE")
  (SIGNAL T=0.305 L="BOTTOM")
}
{NET="A"
  (SEG X1=0.01 Y1=-0.01 X2=0.02 Y2=-0.01 W=0.0005 L="TOP")
}
""", encoding="utf-8")
    s = read_board(f)
    # 0.305 kg/m² of copper (1 oz/ft²) is 34 um.
    assert [c.thickness_mm for c in s.stackup.copper] == pytest.approx([0.034, 0.034], abs=1e-3)
    assert [c.name for c in s.stackup.copper] == ["TOP", "BOTTOM"]
    assert s.stackup.dielectrics[0].thickness_mm == pytest.approx(1.5)
    t = s.tracks[0]
    assert (t.start, t.end, t.width_mm) == ((10.0, 10.0), (20.0, 10.0), 0.5)


def test_english_copper_weight(tmp_path):
    f = tmp_path / "oz.hyp"
    f.write_text('{UNITS=ENGLISH WEIGHT}\n{STACKUP\n  (SIGNAL T=1 L="A")\n'
                 '  (DIELECTRIC T=0.06 L="D")\n  (SIGNAL T=0.5 L="B")\n}\n', encoding="utf-8")
    s = read_board(f)
    assert [c.thickness_mm for c in s.stackup.copper] == pytest.approx([0.035, 0.0175])


def test_parse_keeps_quoted_values_and_skips_comments():
    top = parse('note (with words) {NET="Net-(D1-A)" (PIN R="D1.A" P=3) junk}\n'
                '{PADSTACK="X 1", 0.5\n  (MDEF, 0, 1, 1, 0)\n}')
    net = next(r for r in top if r.kind == "NET")
    assert net.value == "Net-(D1-A)" and net.items[0].fields == {"R": "D1.A", "P": "3"}
    ps = next(r for r in top if r.kind == "PADSTACK")
    assert ps.value == "X 1" and ps.words == ["0.5"] and ps.items[0].words[0] == "MDEF"


def test_files_kipi_cannot_read(tmp_path):
    cut = tmp_path / "cut.hyp"
    cut.write_text(SMALL.read_text(encoding="utf-8")[:3000], encoding="utf-8")
    with pytest.raises(HypError, match="ends inside"):
        read_board(cut)
    other = tmp_path / "notes.hyp"
    other.write_text("some notes, not a board", encoding="utf-8")
    with pytest.raises(HypError, match="not a HyperLynx board"):
        read_board(other)
    no_copper = tmp_path / "empty.hyp"
    no_copper.write_text("{BOARD\n}\n", encoding="utf-8")
    with pytest.raises(HypError, match="no copper layers"):
        read_board(no_copper)


def test_the_registry_opens_hyp_files():
    from kipi.sources import registry
    from kipi.sources.hyp.source import HypFileSource
    assert registry.format_for(Path("Board.HYP")).name == "HyperLynx"
    src = registry.open_board(SMALL)
    assert isinstance(src, HypFileSource) and src.caps.reload and src.files() == [SMALL]
    assert not src.caps.refill_zones and not src.caps.select_items
    assert src.load().pads == read_board(SMALL).pads
