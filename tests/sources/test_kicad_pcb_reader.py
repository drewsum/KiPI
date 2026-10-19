"""The ``.kicad_pcb`` reader against what the KiCad API reads from the same board.

``tests/data/kicad_pcb/features*.kicad_pcb`` were saved by KiCad 10 (and turned into KiCad
9's numbered nets); the ``.ref.kipi.json.gz`` beside each is the KiCad API's reading of it
(``make_fixtures.py`` says how they were made). The reader must give the same board.
"""

from pathlib import Path

import pytest

from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.layers import B_CU, F_CU, inner_cu
from kipi.core.model.snapshot_io import load
from kipi.sources.kicad_pcb import sexpr
from kipi.sources.kicad_pcb.reader import KiCadFileError, read_board

DATA = Path(__file__).parents[1] / "data" / "kicad_pcb"
AREA_TOL = 1e-3          # relative: KiCad's polygons and ours differ only in rounding


def _area(polys) -> float:
    return sum(poly_to_shapely(p).area for p in polys)


@pytest.fixture(scope="module", params=["features", "features_v9"])
def boards(request):
    name = request.param
    return name, read_board(DATA / f"{name}.kicad_pcb"), load(DATA / f"{name}.ref.kipi.json.gz")


# --- the file against the KiCad API -----------------------------------------------------

def test_board_identity_matches_kicad(boards):
    """Setups are found by board name and project folder, so these must be KiCad's: the
    board's file name, and the folder it is in. (The reference names that folder as it
    was on the computer that made it.)"""
    _, snap, ref = boards
    assert snap.board_name == ref.board_name
    assert Path(snap.project_dir) == DATA.resolve()
    assert ref.project_dir.replace("\\", "/").endswith("tests/data/kicad_pcb")
    assert snap.source_format == "kicad-pcb"
    assert snap.assumed == []


def test_stackup_matches_kicad(boards):
    _, snap, ref = boards
    got = [(c.id, c.name, c.thickness_mm, round(c.z_bottom_mm, 6)) for c in snap.stackup.copper]
    want = [(c.id, c.name, c.thickness_mm, round(c.z_bottom_mm, 6)) for c in ref.stackup.copper]
    assert got == want
    assert [c.name for c in snap.stackup.copper] == ["F.Cu", "In1.Cu", "GND plane", "B.Cu"]
    assert snap.stackup.board_thickness_mm == pytest.approx(ref.stackup.board_thickness_mm)
    got_d = [(d.thickness_mm, d.material) for d in snap.stackup.dielectrics]
    assert got_d == [(d.thickness_mm, d.material) for d in ref.stackup.dielectrics]
    # A dielectric's sublayers combine in series; KiCad's API gives εr in single precision.
    assert [d.epsilon_r for d in snap.stackup.dielectrics] == pytest.approx(
        [d.epsilon_r for d in ref.stackup.dielectrics], rel=1e-6)


def test_nets_match_kicad(boards):
    _, snap, ref = boards
    assert [n.name for n in snap.nets] == [n.name for n in ref.nets]


def test_net_classes_come_from_the_project(boards):
    _, snap, _ = boards
    classes = {n.name: n.netclass for n in snap.nets}
    assert classes["+5V"] == "Power"        # by pattern
    assert classes["VOUT"] == "Power"       # assigned
    assert classes["GND"] == "Default"


def test_tracks_match_kicad(boards):
    _, snap, ref = boards

    def key(t):
        return (t.net, t.layer, t.width_mm, t.start, t.end, t.mid)
    assert sorted(map(key, snap.tracks)) == sorted(map(key, ref.tracks))
    assert any(t.is_arc for t in snap.tracks)


def test_vias_match_kicad(boards):
    _, snap, ref = boards
    # The references predate via fill and per-layer pads (#89): where one has neither,
    # those are checked against the file instead (every pad the via's size, fill as saved).
    for v in snap.vias:
        r = next(x for x in ref.vias if x.id == v.id)
        if not r.pad_mm:
            assert v.pad_mm == dict.fromkeys(v.layers, v.diameter_mm)
            assert v.filled is False                    # the fixtures save "(filling no)"
            r.pad_mm, r.filled = v.pad_mm, v.filled
    assert {v.id: v for v in snap.vias} == {v.id: v for v in ref.vias}
    assert {v.via_type for v in snap.vias} == {"through", "blind", "micro"}


def test_footprints_match_kicad(boards):
    import dataclasses
    _, snap, ref = boards
    # The references were saved before footprints recorded their rotation (#130).
    def unturned(fps):
        return sorted((dataclasses.replace(f, angle_deg=None) for f in fps), key=lambda f: f.id)
    assert unturned(snap.footprints) == unturned(ref.footprints)
    assert {f.reference: f.angle_deg for f in snap.footprints} == {"U1": 30.0, "J1": 90.0}
    assert {f.reference: f.side for f in snap.footprints} == {"U1": "top", "J1": "bottom"}


def test_pads_match_kicad(boards):
    _, snap, ref = boards
    want = {p.id: p for p in ref.pads}
    assert sorted(want) == sorted(p.id for p in snap.pads)
    for p in snap.pads:
        r = want[p.id]
        assert (p.net, p.footprint, p.number, p.pad_type) == (r.net, r.footprint, r.number,
                                                              r.pad_type), p.label
        assert p.position == r.position, p.label
        assert p.angle_deg == r.angle_deg, p.label
        assert tuple(p.drill_mm) == tuple(r.drill_mm), p.label
        assert sorted(p.shapes) == sorted(r.shapes), p.label
        for lid in p.shapes:
            a, b = _area(p.shapes[lid]), _area(r.shapes[lid])
            assert a == pytest.approx(b, rel=AREA_TOL), (p.label, lid)
            diff = poly_to_shapely(p.shapes[lid][0]).symmetric_difference(
                poly_to_shapely(r.shapes[lid][0])).area
            assert diff <= AREA_TOL * b, (p.label, lid)


def test_pad_layers_follow_kicad(boards):
    """Plated pads: every layer, except where "remove unused layers" drops them; the
    padstack gives each layer its own shape."""
    _, snap, _ = boards
    pads = {p.label: p for p in snap.pads}
    every = [F_CU, inner_cu(1), inner_cu(2), B_CU]
    assert pads["J1.1"].layers == every
    assert pads["J1.3"].layers == [F_CU, B_CU]          # unconnected inside, ends kept
    j4 = pads["J1.4"].shapes
    assert _area(j4[inner_cu(1)]) < _area(j4[F_CU]) < _area(j4[B_CU])
    assert pads["J1.5"].layers == [B_CU]


def test_zone_fills_match_kicad(boards):
    name, snap, ref = boards
    want = {z.id: z for z in ref.zones}
    assert sorted(want) == sorted(z.id for z in snap.zones)
    if name == "features_v9":
        pytest.skip("KiCad 10 refills an older board's zones when it opens it; the file's "
                    "own fills are compared below")
    for z in snap.zones:
        r = want[z.id]
        assert (z.net, z.name, z.priority, sorted(z.filled)) == (r.net, r.name, r.priority,
                                                                 sorted(r.filled))
        for lid in z.filled:
            assert _area(z.filled[lid]) == pytest.approx(_area(r.filled[lid]), rel=AREA_TOL)


def test_outline_matches_kicad(boards):
    _, snap, ref = boards
    assert len(snap.outline) == len(ref.outline) == 1
    assert _area(snap.outline) == pytest.approx(_area(ref.outline), rel=1e-6)
    assert len(snap.outline[0].holes) == 1                    # the round cutout


def test_numbered_nets_read_like_named_ones():
    """KiCad 9 numbers its nets; the board is the same once they are resolved."""
    v10 = read_board(DATA / "features.kicad_pcb")
    v9 = read_board(DATA / "features_v9.kicad_pcb")
    for kind in ("tracks", "pads", "footprints", "zones"):
        assert getattr(v9, kind) == getattr(v10, kind), kind
    assert [v.net for v in v9.vias] == [v.net for v in v10.vias]


# --- what the file does not give ---------------------------------------------------------

def _board_text():
    return (DATA / "features.kicad_pcb").read_text(encoding="utf-8")


def test_without_a_project_every_net_is_default(tmp_path):
    board = tmp_path / "lonely.kicad_pcb"
    board.write_text(_board_text(), encoding="utf-8")
    snap = read_board(board)
    assert {n.netclass for n in snap.nets} == {"Default"}
    assert any(a.startswith("Net classes:") for a in snap.assumed)


def test_unfilled_zones_are_said(tmp_path):
    text = _board_text()
    start = text.index("(filled_polygon")
    # Drop every fill: cut from the first filled_polygon to the end of its zone each time.
    while "(filled_polygon" in text:
        start = text.index("\t\t(filled_polygon")
        end = text.index("\n\t)\n", start)
        text = text[:start] + text[end + 1:]
    board = tmp_path / "unfilled.kicad_pcb"
    board.write_text(text, encoding="utf-8")
    snap = read_board(board)
    assert all(not z.filled for z in snap.zones if z.name != "graphic shape")
    assert any(a.startswith("Zones: 2 zones") for a in snap.assumed)


def test_missing_stackup_gets_the_default(tmp_path):
    text = _board_text()
    start = text.index("\t\t(stackup")
    end = text.index("\n\t\t)\n", start)
    board = tmp_path / "nostack.kicad_pcb"
    board.write_text(text[:start] + text[end + 5:], encoding="utf-8")
    snap = read_board(board)
    assert not snap.stackup.from_board
    assert len(snap.stackup.copper) == 4
    assert any(a.startswith("Stackup:") for a in snap.assumed)


def test_kicad_7_footprint_fields(tmp_path):
    text = _board_text().replace('(property "Reference" "U1"', '(fp_text reference "U1"')
    board = tmp_path / "k7.kicad_pcb"
    board.write_text(text, encoding="utf-8")
    snap = read_board(board)
    assert "U1" in {f.reference for f in snap.footprints}


def test_old_files_are_refused_with_advice(tmp_path):
    board = tmp_path / "old.kicad_pcb"
    board.write_text("(kicad_pcb (version 20211014) (generator pcbnew))", encoding="utf-8")
    with pytest.raises(KiCadFileError, match="KiCad 7 or later"):
        read_board(board)


@pytest.mark.parametrize("text", ["", "hello", "(kicad_sch (version 20231120))",
                                  "(kicad_pcb (version 20241229)"])
def test_other_files_are_refused(tmp_path, text):
    board = tmp_path / "bad.kicad_pcb"
    board.write_text(text, encoding="utf-8")
    with pytest.raises(KiCadFileError):
        read_board(board)


def test_snapshot_round_trip_keeps_the_board(tmp_path):
    from kipi.core.model.snapshot_io import save
    snap = read_board(DATA / "features.kicad_pcb")
    save(snap, tmp_path / "s.kipi.json.gz")
    back = load(tmp_path / "s.kipi.json.gz")
    assert isinstance(back, BoardSnapshot)
    assert back.source_format == "kicad-pcb"
    assert back.pads == snap.pads


# --- S-expressions --------------------------------------------------------------------------

def test_sexpr_strings_and_escapes():
    tree = sexpr.parse(r'(a (b "x y") (c "q\"uote\\d" "line\nbreak") 1.5 sym)')
    assert tree == ["a", ["b", "x y"], ["c", 'q"uote\\d', "line\nbreak"], "1.5", "sym"]
    assert sexpr.value(tree, "b") == "x y"
    assert sexpr.child(tree, "missing") is None


def test_sexpr_flags():
    tree = sexpr.parse("(pad (remove_unused_layers) (keep_end_layers no) locked)")
    assert sexpr.flag(tree, "remove_unused_layers")
    assert not sexpr.flag(tree, "keep_end_layers")
    assert sexpr.flag(tree, "locked")
    assert not sexpr.flag(tree, "absent")
