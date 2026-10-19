"""Solder masks and silkscreens over the outer copper (#82): read from each board source,
with what each silkscreen prints, editable in the stackup, kept in snapshots and setups,
and drawn in the 3D view."""

import pickle
from pathlib import Path

import pytest
import shapely

from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.progress import NULL

from kipi.core.model.board import MASK, SILK, Coating, Poly
from kipi.core.model.snapshot_io import from_dict, to_dict
from kipi.core.model.stackup import (
    DEFAULT_MASK_MM, DEFAULT_SILK_MM, build_stackup, coating_of, coating_thicknesses,
    from_setup, kicad_color, restack, summary, thicknesses, to_setup,
)
from test_stackup_edit import four_layer

DATA = Path(__file__).resolve().parents[1] / "data"


def coated():
    st = four_layer()
    st.coatings = [Coating(SILK, "top", 0.02), Coating(MASK, "top", 0.025, color="#145124"),
                   Coating(MASK, "bottom", 0.015), Coating(SILK, "bottom", 0.02)]
    return st


def _kinds(st):
    return [(c.side, c.kind) for c in st.coatings]


ORDER = [("top", SILK), ("top", MASK), ("bottom", MASK), ("bottom", SILK)]


def test_a_kicad_board_file_gives_its_masks_and_silkscreens():
    from kipi.sources.kicad_pcb.reader import read_board
    st = read_board(DATA / "kicad_pcb" / "features_v9.kicad_pcb").stackup
    assert _kinds(st) == ORDER
    assert [c.thickness_mm for c in st.coatings] == pytest.approx(
        [DEFAULT_SILK_MM, 0.01, 0.01, DEFAULT_SILK_MM])     # KiCad gives silk no thickness


def test_an_odbpp_job_gives_its_masks_and_silkscreens():
    from kipi.sources.odbpp.reader import read_board
    st = read_board(DATA / "odbpp" / "features.zip").stackup
    assert _kinds(st) == ORDER
    masks = [c for c in st.coatings if c.kind == MASK]
    assert [c.thickness_mm for c in masks] == pytest.approx([0.01, 0.01])
    assert all(c.material == "" for c in st.coatings)       # "not_specified"


@pytest.mark.parametrize("name, kind, side", [
    ("F.Mask", MASK, "top"), ("B.Mask", MASK, "bottom"), ("F.SilkS", SILK, "top"),
    ("B.SilkS", SILK, "bottom"), ("F.Paste", None, None), ("F.Cu", None, None),
])
def test_a_stackup_layer_is_a_coating_by_its_name(name, kind, side):
    c = coating_of(name, "")
    assert (c.kind, c.side) == (kind, side) if kind else c is None


def test_coating_defaults_and_colors():
    assert coating_of("F.Mask", "Top Solder Mask").thickness_mm == DEFAULT_MASK_MM
    assert coating_of("B.SilkS", "", 0.03).thickness_mm == 0.03
    assert kicad_color("Green") == "#143324"
    assert kicad_color("#11AA33FF") == "#11aa33"
    assert kicad_color("Not specified") == ""


def test_a_default_stackup_has_kicads_default_coatings():
    st = build_stackup([], [3, 34], {3: "F.Cu", 34: "B.Cu"})
    assert not st.from_board
    assert _kinds(st) == ORDER


def test_coatings_are_kept_in_order_one_of_each():
    st = build_stackup([("cu", 3, 0.035, "", 0.0), ("diel", -1, 1.5, "FR4", 4.5),
                        ("cu", 34, 0.035, "", 0.0)], [3, 34], {3: "F.Cu", 34: "B.Cu"},
                       coatings=[Coating(MASK, "bottom", 0.01), Coating(MASK, "top", 0.02),
                                 Coating(MASK, "top", 0.05)])
    assert _kinds(st) == [("top", MASK), ("bottom", MASK)]
    assert st.coatings[0].thickness_mm == 0.02
    assert st.coatings_on("top") == [st.coatings[0]]


def test_restack_sets_coatings_without_moving_the_board():
    st = coated()
    ed = restack(st, thicknesses(st), coating_mm=[0.03, 0.05, 0.01, 0.02])
    assert coating_thicknesses(ed) == pytest.approx([0.03, 0.05, 0.01, 0.02])
    assert ed.board_thickness_mm == pytest.approx(st.board_thickness_mm)
    assert ed.coatings[1].color == "#145124"
    assert coating_thicknesses(restack(st, thicknesses(st))) == coating_thicknesses(st)
    with pytest.raises(ValueError):
        restack(st, thicknesses(st), coating_mm=[0.01])


def test_coatings_are_kept_in_the_setup():
    st = coated()
    ed = restack(st, thicknesses(st), coating_mm=[0.03, 0.05, 0.01, 0.02])
    back, why = from_setup(st, to_setup(ed))
    assert why == ""
    assert coating_thicknesses(back) == pytest.approx([0.03, 0.05, 0.01, 0.02])
    old = to_setup(ed)
    del old["coating_mm"]                       # a setup from before #82
    back, _ = from_setup(st, old)
    assert coating_thicknesses(back) == coating_thicknesses(st)


def test_the_summary_names_the_coatings():
    text = summary(coated())
    assert text.startswith("top silkscreen 20 µm, top solder mask 25 µm, F.Cu 35 µm")
    assert "B.Cu 35 µm, bottom solder mask 15 µm, bottom silkscreen 20 µm;" in text


def test_snapshots_keep_coatings_and_older_ones_have_none():
    from synthetic import make_board
    snap = make_board()
    snap.stackup.coatings = coated().coatings
    d = to_dict(snap)
    assert from_dict(d).stackup.coatings == snap.stackup.coatings
    del d["stackup"]["coatings"]
    assert from_dict(d).stackup.coatings == []


def test_results_pickled_before_coatings_have_none():
    st = four_layer()
    state = dict(st.__dict__)
    del state["coatings"]
    old = pickle.loads(pickle.dumps(st))
    old.__dict__.clear()
    old.__setstate__(state)
    assert old.coatings == []


def test_masks_are_open_over_pads_and_sit_outside_the_board():
    pytest.importorskip("pyvista")
    from kipi.core.viz.meshes import board_coatings
    from kipi.sources.kicad_pcb.reader import read_board
    snap = read_board(DATA / "kicad_pcb" / "features_v9.kicad_pcb")
    got = board_coatings(snap)
    assert [k for k, _, _ in got] == [MASK, SILK, MASK, SILK]
    top_mask, top_silk, bottom_mask, bottom_silk = (m for _, m, _ in got)
    t = snap.stackup.board_thickness_mm
    assert top_mask.bounds[4] >= t and top_silk.bounds[4] >= top_mask.bounds[5] - 1e-9
    assert bottom_mask.bounds[5] <= 0 and bottom_silk.bounds[5] <= bottom_mask.bounds[4] + 1e-9
    # The mask covers the board but the pads; the silkscreen only what it prints.
    from kipi.core.geometry.silkscreen import mask_openings, printed_silk
    board = shapely.union_all([poly_to_shapely(p) for p in snap.outline])
    open_area = mask_openings(snap, "top").intersection(board).area
    assert top_mask.volume == pytest.approx((board.area - open_area) * 0.01, rel=1e-3)
    assert top_silk.volume == pytest.approx(printed_silk(snap, "top").area * DEFAULT_SILK_MM,
                                            rel=1e-3)
    assert top_silk.volume < top_mask.volume * 0.01


def _centers(polys):
    return [poly_to_shapely(p).centroid for p in polys]


def test_a_kicad_board_file_gives_what_its_silkscreens_print():
    """Each footprint's drawings and its reference and value text on F.SilkS/B.SilkS; a
    footprint on the bottom prints on the bottom."""
    from kipi.sources.kicad_pcb.reader import read_board
    snap = read_board(DATA / "kicad_pcb" / "features_v9.kicad_pcb")
    assert set(snap.silkscreen) == {"top", "bottom"}
    u1 = next(f for f in snap.footprints if f.reference == "U1")
    j1 = next(f for f in snap.footprints if f.reference == "J1")
    top = shapely.union_all([poly_to_shapely(p) for p in snap.silkscreen["top"]])
    bottom = shapely.union_all([poly_to_shapely(p) for p in snap.silkscreen["bottom"]])
    assert top.distance(shapely.Point(u1.position)) < 6
    assert bottom.distance(shapely.Point(j1.position)) < 6
    assert top.distance(shapely.Point(j1.position)) > 10


def test_odbpp_and_the_board_file_print_the_same_silkscreen():
    """The same board exported to ODB++ (KiCad's own strokes) and read from its file
    (a similar font): every piece lands in the same place."""
    from kipi.sources.kicad_pcb.reader import read_board as read_pcb
    from kipi.sources.odbpp.reader import read_board as read_odb
    odb = read_odb(DATA / "odbpp" / "features.zip").silkscreen
    pcb = read_pcb(DATA / "kicad_pcb" / "features.kicad_pcb").silkscreen
    for side in ("top", "bottom"):
        assert len(odb[side]) == len(pcb[side])
        for c in _centers(odb[side]):
            assert min(c.distance(d) for d in _centers(pcb[side])) < 0.4


def test_text_is_placed_turned_and_justified_as_kicad_does():
    from kipi.core.geometry.text import text_geometry
    g = text_geometry("HI", (10, 20), 1.0, 1.0)
    assert g.centroid.x == pytest.approx(10, abs=0.05)
    assert g.centroid.y == pytest.approx(20, abs=0.05)
    assert g.bounds[3] - g.bounds[1] == pytest.approx(1.0, abs=0.05)    # a capital's height
    turned = text_geometry("HI", (10, 20), 1.0, 1.0, angle_deg=90)
    assert turned.bounds[3] - turned.bounds[1] > turned.bounds[2] - turned.bounds[0]
    left = text_geometry("HI", (10, 20), 1.0, 1.0, h_justify="left")
    assert left.bounds[0] == pytest.approx(10, abs=0.15)
    mirrored = text_geometry("HI", (10, 20), 1.0, 1.0, h_justify="left", mirror=True)
    assert mirrored.bounds[2] == pytest.approx(10, abs=0.15)
    assert text_geometry("  ", (0, 0), 1.0, 1.0) is None


def test_footprint_text_reads_upright_unless_unlocked():
    """KiCad turns footprint text (and board text, from KiCad 8) half round to read
    upright; ``unlocked`` text keeps its angle."""
    from kipi.sources.kicad_pcb.reader import read_silkscreen
    from kipi.sources.kicad_pcb.sexpr import parse
    from kipi.core.geometry.text import text_geometry
    upright = text_geometry("U12", (0, 0), 1.0, 1.0, 0.15, 0)

    def fp(at):
        root = parse('(kicad_pcb (footprint "x" (layer "F.Cu") (at 0 0) (property "Reference" '
                     f'"U12" (at {at}) (layer "F.SilkS") (effects (font (size 1 1) '
                     '(thickness 0.15))))))')
        return shapely.union_all([poly_to_shapely(p) for p in read_silkscreen(root, NULL)["top"]])

    assert fp("0 0 180").symmetric_difference(upright).area < 0.02 * upright.area
    assert fp("0 0 180 unlocked").symmetric_difference(upright).area > 0.5 * upright.area


def test_kicad_gives_what_its_silkscreens_print():
    """Graphics on F.SilkS and B.SilkS, and text drawn by KiCad itself (here, one 2 mm
    stroke through each text's position)."""
    pytest.importorskip("kipy")
    from kicad_fake import VERSIONS, FakeKiCad
    from kipi.sources.kicad_ipc.extract import extract_board
    snap = extract_board(FakeKiCad(VERSIONS[-1]).board())
    top = sorted(poly_to_shapely(p).bounds for p in snap.silkscreen["top"])
    bottom = [poly_to_shapely(p).bounds for p in snap.silkscreen["bottom"]]
    assert top == [pytest.approx((1.9, 11.9, 12.1, 12.1)),                   # the line
                   pytest.approx((18.925, 1.925, 21.075, 2.075), abs=1e-3)]  # U1
    assert bottom == [pytest.approx((13.925, 9.925, 16.075, 10.075), abs=1e-3)]  # "KiPI"
    assert not any("Silkscreen" in a for a in snap.assumed)


def test_without_kicads_text_the_silkscreen_keeps_its_graphics():
    pytest.importorskip("kipy")
    from kicad_fake import VERSIONS, FakeKiCad
    from kipy.errors import ApiError
    from kipi.sources.kicad_ipc.extract import extract_board
    kicad = FakeKiCad(VERSIONS[-1])

    def refuse(cmd):
        raise ApiError("no handler")

    kicad._GetTextAsShapes = refuse
    snap = extract_board(kicad.board())
    assert [poly_to_shapely(p).bounds for p in snap.silkscreen["top"]] == \
        [pytest.approx((1.9, 11.9, 12.1, 12.1))]
    assert "bottom" not in snap.silkscreen
    assert any("Silkscreen" in a for a in snap.assumed)


def test_snapshots_keep_the_silkscreen_and_older_ones_print_nothing():
    from synthetic import make_board
    snap = make_board()
    snap.silkscreen = {"top": [Poly([(0, 0), (1, 0), (1, 1)])]}
    d = to_dict(snap)
    assert from_dict(d).silkscreen == snap.silkscreen
    del d["silkscreen"]
    assert from_dict(d).silkscreen == {}
    old = pickle.loads(pickle.dumps(snap))
    state = dict(snap.__dict__)
    del state["silkscreen"]
    old.__dict__.clear()
    old.__setstate__(state)
    assert old.silkscreen == {}


def test_silkscreen_is_printed_on_the_board_and_off_the_pads():
    from synthetic import make_board
    from kipi.core.geometry.silkscreen import printed_silk
    snap = make_board()
    board = shapely.union_all([poly_to_shapely(p) for p in snap.outline])
    pad = next(p for p in snap.pads if snap.stackup.copper[0].id in p.shapes)
    pad_geom = poly_to_shapely(pad.shapes[snap.stackup.copper[0].id][0])
    x0, y0, x1, y1 = board.bounds
    snap.silkscreen = {"top": [Poly([(x0 - 5, y0 - 5), (x1 + 5, y0 - 5), (x1 + 5, y1 + 5),
                                     (x0 - 5, y1 + 5)])]}
    ink = printed_silk(snap, "top")
    assert ink.within(board.buffer(1e-6))
    assert ink.intersection(pad_geom).area < 1e-9
    assert printed_silk(snap, "bottom") is None


def test_kicad_gives_its_masks_and_silkscreens():
    pytest.importorskip("kipy")
    from kicad_fake import VERSIONS, FakeKiCad
    from kipi.sources.kicad_ipc.extract import extract_board
    st = extract_board(FakeKiCad(VERSIONS[-1]).board()).stackup
    assert _kinds(st) == ORDER
    top_silk, top_mask = st.coatings[:2]
    assert top_mask.thickness_mm == pytest.approx(0.01)
    assert top_mask.material == "Epoxy" and top_mask.color == "#ff0000"
    assert top_silk.thickness_mm == DEFAULT_SILK_MM and top_silk.color == ""
