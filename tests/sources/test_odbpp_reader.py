"""The ODB++ reader against what the KiCad API reads from the same board.

``tests/data/odbpp`` holds KiCad 10's ODB++ exports of the ``.kicad_pcb`` reader's fixture
board (``tests/data/kicad_pcb/make_fixtures.py``): ``features.zip`` at 6 decimals and
``features_p2.tgz`` at KiCad's default of 2. The reference is the KiCad API's reading of
that board. Where the export itself leaves something out, the tests say so.
"""

import zipfile
from collections import defaultdict
from pathlib import Path

import pytest

from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.model.layers import B_CU, F_CU, inner_cu
from kipi.core.model.snapshot_io import load
from kipi.sources.odbpp import features as feat
from kipi.sources.odbpp.archive import Job, OdbError
from kipi.sources.odbpp.reader import read_board

DATA = Path(__file__).parents[1] / "data"
ZIP = DATA / "odbpp" / "features.zip"
TGZ = DATA / "odbpp" / "features_p2.tgz"


@pytest.fixture(scope="module")
def ref():
    return load(DATA / "kicad_pcb" / "features.ref.kipi.json.gz")


@pytest.fixture(scope="module")
def snap():
    return read_board(ZIP)


def _area(polys) -> float:
    return sum(poly_to_shapely(p).area for p in polys)


def _pads_by_place(s):
    return {(p.footprint, p.number, p.position): p for p in s.pads}


# --- the precise export against the KiCad API ------------------------------------------------

def test_identity(snap):
    assert snap.board_name == "features" and snap.source_format == "odbpp"
    assert Path(snap.project_dir) == ZIP.parent.resolve()


def test_copper_layers_and_thickness(snap, ref):
    got = [(c.id, c.thickness_mm) for c in snap.stackup.copper]
    assert got == [(c.id, c.thickness_mm) for c in ref.stackup.copper]
    # The matrix upper-cases names; canonical ones come back as KiCad writes them.
    assert [c.name for c in snap.stackup.copper] == ["F.Cu", "In1.Cu", "GND_PLANE", "B.Cu"]


def test_lost_dielectric_sublayer_is_said(snap):
    """KiCad writes one folder for a dielectric's two sublayers: the second has no
    thickness, which the board says rather than hides."""
    notes = [a for a in snap.assumed if a.startswith("Stackup:")]
    assert len(notes) == 1 and "DIELECTRIC_2_2" in notes[0]


def test_nets_match_kicad(snap, ref):
    assert [n.name for n in snap.nets] == [n.name for n in ref.nets]


def test_tracks_match_kicad(snap, ref):
    def key(t):
        return (t.net, t.layer, t.width_mm, t.start, t.end)
    assert sorted(map(key, snap.tracks)) == sorted(map(key, ref.tracks))
    arcs = [t for t in snap.tracks if t.is_arc]
    assert len(arcs) == 1 and arcs[0].mid == pytest.approx((45.0, 37.0), abs=1e-6)


def test_vias_match_kicad(snap, ref):
    def key(v):
        # ODB++ cannot tell a micro via from a blind one.
        kind = "blind" if v.via_type == "micro" else v.via_type
        return (v.net, v.position, tuple(v.layers), v.diameter_mm, v.drill_mm, kind)
    assert sorted(map(key, snap.vias)) == sorted(map(key, ref.vias))


def test_pads_match_kicad(snap, ref):
    want = _pads_by_place(ref)
    assert sorted(want) == sorted(_pads_by_place(snap))
    for place, p in _pads_by_place(snap).items():
        r = want[place]
        assert (p.net, p.pad_type, p.angle_deg) == (r.net, r.pad_type, r.angle_deg), p.label
        assert p.drill_mm == pytest.approx(r.drill_mm, abs=1e-6), p.label
        assert sorted(p.shapes) == sorted(r.shapes), p.label
        # KiCad's export drops the rounding of a chamfered rounded rectangle (U1.6).
        tol = 0.02 if p.label == "U1.6" else 2e-3
        for lid in p.shapes:
            a, b = poly_to_shapely(p.shapes[lid][0]), poly_to_shapely(r.shapes[lid][0])
            assert a.symmetric_difference(b).area <= tol * b.area, (p.label, lid)


def test_mirrored_pads_turn_the_right_way(snap, ref):
    """J1.6 is a bottom pad with one corner cut, turned off the grid: only the right
    mirror and turn put the corner where KiCad has it."""
    got = next(p for p in snap.pads if p.label == "J1.6").shapes[B_CU][0]
    want = next(p for p in ref.pads if p.label == "J1.6").shapes[B_CU][0]
    a, b = poly_to_shapely(got), poly_to_shapely(want)
    assert a.symmetric_difference(b).area < 1e-6 * b.area


def test_planes_match_kicad(snap, ref):
    def per(s, names):
        out = defaultdict(float)
        for z in s.zones:
            if z.name in names:
                for lid, polys in z.filled.items():
                    out[(z.net, lid)] += _area(polys)
        return out
    got, want = per(snap, {""}), per(ref, {"ground", "five"})
    assert sorted(got) == sorted(want)
    for k in want:
        assert got[k] == pytest.approx(want[k], rel=1e-3), k


def test_footprints_match_kicad(snap, ref):
    def key(f):
        return (f.reference, f.value, f.position, f.side, len(f.pad_ids))
    assert sorted(map(key, snap.footprints)) == sorted(map(key, ref.footprints))
    # Turned as KiCad shows them, on either side (#130)
    assert {f.reference: f.angle_deg for f in snap.footprints} == {"U1": 30.0, "J1": 90.0}


def test_outline_matches_kicad(snap, ref):
    assert _area(snap.outline) == pytest.approx(_area(ref.outline), rel=1e-4)
    assert len(snap.outline) == 1 and len(snap.outline[0].holes) == 1


# --- KiCad's default precision ----------------------------------------------------------------

def test_two_decimals_read_within_their_rounding(ref):
    s = read_board(TGZ)
    # 35 um is written 0.03 at two decimals; the copper weight gives it back.
    assert [c.thickness_mm for c in s.stackup.copper] == [0.035, 0.0175, 0.0175, 0.035]
    want = sorted((p.footprint, p.number, p.position) for p in ref.pads)
    got = sorted((p.footprint, p.number, p.position) for p in s.pads)
    for (fa, na, pa), (fb, nb, pb) in zip(got, want):
        assert (fa, na) == (fb, nb)
        assert pa == pytest.approx(pb, abs=0.0051)


# --- where the job is ------------------------------------------------------------------------

def test_a_job_folder_reads_like_its_archive(tmp_path, snap):
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(tmp_path)
    folder = next(p.parent.parent for p in tmp_path.rglob("matrix") if p.is_file())
    for path in (folder, folder / "matrix" / "matrix"):
        s = read_board(path)
        assert s.board_name == folder.name
        assert s.pads == snap.pads and s.tracks == snap.tracks


def test_the_registry_opens_jobs(tmp_path):
    from kipi.sources import registry
    from kipi.sources.odbpp.source import OdbFileSource
    for path in (ZIP, TGZ, Path("board.tar.gz"), Path("x/matrix/matrix")):
        assert registry.format_for(path).name == "ODB++"
    src = registry.open_board(ZIP)
    assert isinstance(src, OdbFileSource) and src.caps.reload and src.files() == [ZIP]
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(tmp_path)
    folder = next(p.parent.parent for p in tmp_path.rglob("matrix") if p.is_file())
    assert registry.format_for(folder).name == "ODB++"
    assert registry.open_board(folder / "matrix" / "matrix").files() == [
        folder / "matrix" / "matrix"]
    assert registry.format_for(tmp_path / "empty_dir_that_is_not_a_job") is None


def test_other_archives_are_refused(tmp_path):
    gerbers = tmp_path / "gerbers.zip"
    with zipfile.ZipFile(gerbers, "w") as z:
        z.writestr("board-F_Cu.gbr", "G04*")
    with pytest.raises(OdbError, match="no matrix/matrix"):
        read_board(gerbers)
    text = tmp_path / "notes.zip"
    text.write_text("not a zip")
    with pytest.raises(OdbError):
        read_board(text)


def test_unix_compressed_files_are_explained(tmp_path):
    job = tmp_path / "job.zip"
    with zipfile.ZipFile(job, "w") as z:
        z.writestr("job/matrix/matrix", "")
        z.writestr("job/misc/info", "UNITS=MM\n")
        z.writestr("job/steps/pcb/eda/data.Z", b"\x1f\x9d")
    with Job(job) as j:
        with pytest.raises(OdbError, match="Unix compress"):
            j.read("steps/pcb/eda/data")


# --- the features format ---------------------------------------------------------------------

MM = feat.Units("MM")


def test_pads_turn_clockwise_unless_mirrored():
    lf = feat.parse("UNITS=MM\n$0 r100\nP 1 2 0 P 0 8 30.0\nP 1 2 0 P 0 9 30.0\n"
                    "P 1 2 0 P 0 1\nP 1 2 0 P 0 5\n", MM)
    got = [(f.at, f.angle_deg, f.mirror) for f in lf.features]
    assert got == [((1.0, -2.0), 330.0, False), ((1.0, -2.0), 30.0, True),
                   ((1.0, -2.0), 270.0, False), ((1.0, -2.0), 90.0, True)]


def test_arcs_keep_their_direction():
    """A clockwise arc over the top in ODB++ (y up) is the same arc on KiPI's board."""
    lf = feat.parse("UNITS=MM\n$0 r100\nA 0 0 2 0 1 0 0 P 0 Y\nA 0 0 2 0 1 0 0 P 0 N\n", MM)
    cw, ccw = lf.features
    assert feat._arc_mid(cw.start, cw.end, cw.center, cw.clockwise) == pytest.approx((1, -1))
    assert feat._arc_mid(ccw.start, ccw.end, ccw.center, ccw.clockwise) == pytest.approx((1, 1))


def test_features_keep_their_numbers():
    """eda/data refers to features by their place in the file, text included."""
    lf = feat.parse("UNITS=MM\n$0 r100\nT 0 0 standard P 0 0 1 1 1 'x' 0\nL 0 0 1 1 0 P 0\n"
                    "S P 0\nOB 0 0 I\nOS 1 0\nOS 1 1\nOS 0 0\nOE\nSE\nP 0 0 0 P 0 8 0\n", MM)
    assert [f.kind for f in lf.features] == ["T", "L", "S", "P"]


@pytest.mark.parametrize("name,size", [("r250", (0.25, 0.25)), ("s400", (0.4, 0.4)),
                                       ("rect1000x600", (1.0, 0.6)),
                                       ("rect1200x700xr175", (1.2, 0.7)),
                                       ("oval1600x800", (1.6, 0.8)), ("di500x300", (0.5, 0.3))])
def test_standard_symbols(name, size):
    from shapely.geometry import Polygon
    pts = feat.symbol_outline(name, MM)
    minx, miny, maxx, maxy = Polygon(pts).bounds
    # Curves are cut as KiCad cuts them, with no vertex at the very ends.
    assert (maxx - minx, maxy - miny) == pytest.approx(size, abs=0.01)
    assert feat.symbol_size(name, MM) == pytest.approx(size)


def test_chamfered_corners_are_where_odb_numbers_them():
    """Corner 2 is the upper left: y is negative there on KiPI's board."""
    pts = feat.symbol_outline("rect1000x1000xc300x2", MM)
    assert (-0.5, -0.5) not in pts and (0.5, -0.5) in pts and (-0.5, 0.5) in pts


def test_inch_jobs_scale():
    units = feat.Units("INCH")
    lf = feat.parse("UNITS=INCH\n$0 r10\nP 1 1 0 P 0 8 0\n", units)
    assert lf.features[0].at == (25.4, -25.4)
    assert feat.round_width("r10", units) == pytest.approx(0.254)


def test_copper_drawings_without_a_net_are_said(snap):
    """KiCad's export does not tie copper drawings to nets: an isolated one has none."""
    assert all(z.name != "graphic shape" for z in snap.zones)
    # The +5V polygon on F.Cu and the SIG ring on In1.Cu touch nothing of their nets.
    assert not any(F_CU in z.filled for z in snap.zones if z.net == "+5V")
    assert not any(inner_cu(1) in z.filled for z in snap.zones if z.net == "SIG")
