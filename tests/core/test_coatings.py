"""Solder masks and silkscreens over the outer copper (#82): read from each board source,
editable in the stackup, kept in snapshots and setups, and drawn in the 3D view."""

import pickle
from pathlib import Path

import pytest

from kipi.core.model.board import MASK, SILK, Coating
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
    # The silkscreen covers the face; the mask leaves the pads open.
    assert top_mask.volume < top_silk.volume * 0.01 / DEFAULT_SILK_MM


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
