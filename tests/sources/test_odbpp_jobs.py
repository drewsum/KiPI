"""ODB++ jobs as other tools write them, made from KiCad's export of the fixture board.

There are no Altium, OrCAD or Valor jobs in the tests, so each case is the KiCad export
(``tests/data/odbpp/features.zip``) changed the way the other tool writes it: zipped
inside a zip, files compressed with Unix ``compress``, a panel step beside the board, a
via with a pad left off a layer, copper drawn with no net. Each read is held to the
plain export's.
"""

import io
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import Polygon as SPolygon

from kipi.core.model.layers import F_CU
from kipi.core.progress import NULL
from kipi.sources.odbpp import features as feat, lzw
from kipi.sources.odbpp.archive import Job, OdbError
from kipi.sources.odbpp.reader import pick_step, read_board
from kipi.sources.odbpp.source import OdbFileSource

DATA = Path(__file__).parents[1] / "data" / "odbpp"
ZIP = DATA / "features.zip"
TGZ = DATA / "features_p2.tgz"
MM = feat.Units("MM")


@pytest.fixture(scope="module")
def snap():
    return read_board(ZIP)


def _files(src=ZIP):
    """The fixture job's files, by name."""
    with zipfile.ZipFile(src) as z:
        return {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}


def _zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _same_board(a, b):
    assert a.pads == b.pads and a.tracks == b.tracks and a.vias == b.vias
    assert a.footprints == b.footprints and a.outline == b.outline
    assert [(z.net, z.filled) for z in a.zones] == [(z.net, z.filled) for z in b.zones]


# --- Unix compress (.Z) ----------------------------------------------------------------------

def _compress(data: bytes, max_bits: int = 16, clear: bool = False) -> bytes:
    """``data`` as ncompress writes it, block mode. With ``clear`` the table is cleared each
    time it fills, which ncompress does only when compression gets worse."""
    acc = nacc = pos = group_start = 0
    out = bytearray()

    def put(code, n):
        nonlocal acc, nacc, pos
        acc |= code << nacc
        nacc += n
        pos += n
        while nacc >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nacc -= 8

    def end_group(n):
        nonlocal group_start
        used = (pos - group_start) % (n * 8)
        if used:
            put(0, n * 8 - used)
        group_start = pos

    def fresh():
        return {bytes([i]): i for i in range(256)}

    table, free, n_bits, max_code = fresh(), 257, 9, 1 << max_bits
    w = b""
    for c in data:
        wc = w + bytes([c])
        if wc in table:
            w = wc
            continue
        put(table[w], n_bits)
        if free >= (1 << n_bits) and n_bits < max_bits:
            end_group(n_bits)
            n_bits += 1
        if free < max_code:
            table[wc] = free
            free += 1
        elif clear:
            put(lzw.CLEAR, n_bits)
            end_group(n_bits)
            table, free, n_bits = fresh(), 257, 9
        w = bytes([c])
    if w:
        put(table[w], n_bits)
    if nacc:
        out.append(acc & 0xFF)
    return lzw.MAGIC + bytes([lzw.BLOCK_MODE | max_bits]) + bytes(out)


# "TOBEORNOTTOBEORTOBEORNOT" in .Z, 16-bit codes (gzip -d reads it back).
TOBEORNOT = bytes.fromhex("1f9d90549e0829f2448a932754020e2ca890a04184")


def test_a_known_z_stream_decodes():
    assert lzw.decompress(TOBEORNOT) == b"TOBEORNOTTOBEORTOBEORNOT"


def _samples():
    text = _files()["steps/pcb/layers/gnd_plane/features"]
    return [(b"", 16, False), (b"a", 16, False), (b"aaaaaaaaaaaaaaaaaaaa", 16, False),
            (text, 16, False),              # the code width grows to 13 bits
            (text, 10, False),              # the table fills and is kept
            (text, 10, True), (text, 12, True)]  # the table fills and is cleared


_IDS = ["empty", "one byte", "a run", "13-bit codes", "10-bit table kept",
        "10-bit table cleared", "12-bit table cleared"]


@pytest.mark.parametrize("data,bits,clear", _samples(), ids=_IDS)
def test_z_streams_decode(data, bits, clear):
    assert lzw.decompress(_compress(data, bits, clear)) == data


def _gnu_gzip() -> bool:
    """GNU gzip, whose .Z decoder follows ncompress. macOS ships BSD gzip, whose decoder
    reads an empty stream and a table filled at under 16 bits differently."""
    if shutil.which("gzip") is None:
        return False
    try:
        out = subprocess.run(["gzip", "--version"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return b"GNU" in out.stdout + out.stderr or b"Free Software" in out.stdout


@pytest.mark.skipif(not _gnu_gzip(), reason="needs GNU gzip to check the encoder")
@pytest.mark.parametrize("data,bits,clear", _samples(), ids=_IDS)
def test_the_test_encoder_writes_what_gzip_reads(data, bits, clear):
    """GNU gzip reads .Z files with its own decoder: the streams above are real ones."""
    z = _compress(data, bits, clear)
    got = subprocess.run(["gzip", "-dc"], input=z, capture_output=True, check=True).stdout
    assert got == data
    if data == b"TOBEORNOTTOBEORTOBEORNOT":
        assert z == TOBEORNOT


def test_a_corrupt_z_stream_is_said():
    with pytest.raises(lzw.LzwError):
        lzw.decompress(lzw.MAGIC + b"\x90" + b"\xff\xff\xff")     # a first code past 255
    with pytest.raises(lzw.LzwError, match="code width"):
        lzw.decompress(lzw.MAGIC + b"\x98abc")


def test_a_job_of_z_files_reads_like_the_plain_one(tmp_path, snap):
    files = {}
    for name, data in _files().items():
        if name.endswith(("/features", "/eda/data", "/components", "/profile")):
            files[name + ".Z"] = _compress(data)
        else:
            files[name] = data
    _same_board(read_board(_zip(tmp_path / "compressed.zip", files)), snap)


# --- where the job is ------------------------------------------------------------------------

def test_a_job_zipped_inside_a_zip_is_opened(tmp_path, snap):
    outer = _zip(tmp_path / "outer.zip", {"features.zip": ZIP.read_bytes(),
                                         "readme.txt": b"the job is inside"})
    _same_board(read_board(outer), snap)


def test_a_tgz_zipped_inside_a_zip_is_opened(tmp_path):
    """OrCAD's _ODBGBR.zip around its .tgz."""
    outer = _zip(tmp_path / "board_ODBGBR.zip", {"board.tgz": TGZ.read_bytes()})
    _same_board(read_board(outer), read_board(TGZ))


def test_a_zip_of_two_archives_is_not_a_job(tmp_path):
    outer = _zip(tmp_path / "two.zip", {"a.zip": ZIP.read_bytes(), "b.tgz": TGZ.read_bytes()})
    with pytest.raises(OdbError, match="no matrix/matrix"):
        read_board(outer)


def _tgz(path, files):
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(f"features/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


def test_a_tgz_is_read_in_one_pass(tmp_path, snap, monkeypatch):
    """A .tgz's files are taken out in archive order, each once: going back for an earlier
    one decompresses the archive from the start again. Only the small files read to pick
    the step, before the step is known, come before the pass."""
    job = _tgz(tmp_path / "features.tgz", _files())
    taken = []
    extract = tarfile.TarFile.extractfile

    def counted(self, member):
        taken.append(member.name if isinstance(member, tarfile.TarInfo) else member)
        return extract(self, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", counted)
    _same_board(read_board(job), snap)
    with tarfile.open(job) as t:
        order = {m.name: i for i, m in enumerate(t.getmembers())}
    first = next(i for i, n in enumerate(taken) if n.rsplit("/", 1)[1] not in ("info", "stephdr"))
    assert {n.rsplit("/", 1)[1] for n in taken[:first]} <= {"info", "stephdr"}
    rest = [order[n] for n in taken[first:]]
    assert rest == sorted(set(rest))


# --- the board step, not the panel -----------------------------------------------------------

def _steps_job(tmp_path, steps):
    """A job of steps only: {name: stephdr text, or None for a board step with a netlist}."""
    files = {"matrix/matrix": b"", "misc/info": b"UNITS=MM\n"}
    for name, hdr in steps.items():
        files[f"steps/{name}/stephdr"] = (hdr or "UNITS=MM\n").encode()
        if hdr is None or "pcb+panel" in name:
            files[f"steps/{name}/eda/data"] = b"HDR\n"
    return _zip(tmp_path / "steps.zip", files)


def _repeat(*names):
    return "UNITS=MM\n" + "".join(
        f"STEP-REPEAT {{\n   NAME={n.upper()}\n   X=0\n   Y=0\n   DX=60\n   DY=0\n"
        f"   NX=1\n   NY=1\n   ANGLE=0\n   FLIP=NO\n   MIRROR=NO\n}}\n" for n in names)


def _pick(path, wanted=None):
    assumed = []
    with Job(path) as job:
        return pick_step(job, NULL, wanted, assumed), assumed


def test_the_board_is_read_not_its_panel(tmp_path):
    """A panel that sorts first, with a netlist of its own, used to be read."""
    step, assumed = _pick(_steps_job(tmp_path, {"array": _repeat("single", "single"),
                                                "single": None}))
    assert step == "single" and not assumed


def test_the_board_the_panel_places_most_is_read(tmp_path):
    step, assumed = _pick(_steps_job(tmp_path, {
        "a_coupon": None, "b_board": None,
        "panel": _repeat("b_board", "a_coupon", "b_board", "b_board")}))
    assert step == "b_board"
    assert len(assumed) == 1
    assert "2 board steps (b_board, a_coupon)" in assumed[0]
    assert "b_board x3" in assumed[0] and "a_coupon x1" in assumed[0]
    assert "b_board is read" in assumed[0]


def test_a_flattened_panel_comes_after_the_board(tmp_path):
    """Valor's pcb+panel: the panel in one step, with no step-and-repeat to show it."""
    step, _ = _pick(_steps_job(tmp_path, {"pcb+panel": "UNITS=MM\n", "unit": None}))
    assert step == "unit"


def test_a_step_named_pcb_is_still_picked(tmp_path):
    step, _ = _pick(_steps_job(tmp_path, {"aaa": None, "pcb": None, "zzz": None}))
    assert step == "pcb"


def test_a_step_asked_for_is_read(tmp_path, snap):
    files = _files()
    for name, data in list(files.items()):
        if name.startswith("steps/pcb/"):
            files["steps/copy/" + name[len("steps/pcb/"):]] = data
    job = _zip(tmp_path / "two_boards.zip", files)
    assert _pick(job, "COPY")[0] == "copy"
    _same_board(OdbFileSource(job, step="copy").load(), snap)
    with pytest.raises(OdbError, match="no step 'other'; it has copy, pcb"):
        read_board(job, step="other")
    assert any("copy" in a and "pcb is read" in a for a in read_board(job).assumed)


# --- a surface's holes -----------------------------------------------------------------------

def _one_hole_at_a_time(f: feat.Feature):
    """A surface as it was cut before: each hole out of the whole island in turn."""
    parts, current = [], None
    for is_hole, pts in f.contours:
        if not is_hole:
            if current is not None:
                parts.append(current)
            current = SPolygon(pts)
            if not current.is_valid:
                current = current.buffer(0)
        elif current is not None:
            current = current.difference(SPolygon(pts).buffer(0))
    if current is not None:
        parts.append(current)
    from shapely.ops import unary_union
    return unary_union(parts)


def _square(x, y, s):
    return [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]


def _surface(contours):
    lines = ["UNITS=MM", "S P 0"]
    for is_hole, pts in contours:
        (x0, y0), rest = pts[0], pts[1:]
        lines.append(f"OB {x0} {y0} {'H' if is_hole else 'I'}")
        lines += [f"OS {x} {y}" for x, y in rest]
        lines.append("OE")
    lines.append("SE")
    return feat.parse("\n".join(lines) + "\n", MM).features[0]


def test_holes_are_cut_out_of_their_own_island():
    plane = [(False, _square(0, 0, 20))]
    plane += [(True, _square(1 + 2 * i, 1 + 2 * j, 1)) for i in range(8) for j in range(8)]
    plane += [(True, _square(19.5, 10, 1)),          # crosses the island's edge
              (True, _square(17, 1, 1)), (True, _square(17.5, 1.5, 1))]  # holes that overlap
    island = [(False, _square(30, 0, 5)), (True, _square(31, 1, 1)), (True, _square(33, 3, 1))]
    f = _surface(plane + island + [(False, _square(40, 0, 2))])
    got, want = feat.geometry(f, MM), _one_hole_at_a_time(f)
    assert got.symmetric_difference(want).area < 1e-9
    assert got.area == pytest.approx(400 - 64 - 0.5 - 1.75 + 25 - 2 + 4)


def test_the_boards_planes_cut_as_before():
    with Job(ZIP) as job:
        surfaces = [f for layer in ("f.cu", "in1.cu", "gnd_plane", "b.cu")
                    for f in feat.parse(job.read(f"steps/pcb/layers/{layer}/features"),
                                        MM).features if f.kind == "S"]
    assert surfaces
    for f in surfaces:
        got, want = feat.geometry(f, MM), _one_hole_at_a_time(f)
        assert got.symmetric_difference(want).area < 1e-6 * max(want.area, 1.0)


# --- a via's pad on each layer ---------------------------------------------------------------

_FEATURE = re.compile(r"^(P|L|A|S|T|B) ")


def _new_symbol(text: str, symbol: str):
    """``text`` with ``symbol`` added to its symbol table, and the symbol's number."""
    lines = text.splitlines()
    last = max(i for i, l in enumerate(lines) if re.match(r"^\$\d+ ", l))
    n = int(lines[last].split()[0][1:]) + 1
    lines.insert(last + 1, f"${n} {symbol}")
    return "\n".join(lines) + "\n", n


def _with_pad_symbol(text: str, index: int, symbol: str) -> str:
    """``text`` with feature ``index`` (a pad) drawn with ``symbol``."""
    text, n = _new_symbol(text, symbol)
    lines, k = text.splitlines(), -1
    for i, l in enumerate(lines):
        if _FEATURE.match(l):
            k += 1
            if k == index:
                parts = l.split(" ")
                assert parts[0] == "P"
                parts[3] = str(n)
                lines[i] = " ".join(parts)
                break
    return "\n".join(lines) + "\n"


def test_a_via_pad_is_read_on_each_layer(tmp_path, snap):
    """The through via whose drill is hole 1: KiCad wrote its pads as feature 12 of f.cu,
    2 of gnd_plane and 2 of in1.cu. Its in1.cu pad is made larger, and its gnd_plane pad
    left out of its subnet, as a via with unused pads removed is written."""
    files = _files()
    eda = files["steps/pcb/eda/data"].decode()
    subnet = "SNT VIA\nFID C 0 4\nFID H 3 1\nFID C 5 12\nFID C 8 2\nFID C 9 2\n"
    assert subnet in eda
    files["steps/pcb/eda/data"] = eda.replace(subnet, subnet.replace("FID C 8 2\n", ""))
    files["steps/pcb/layers/in1.cu/features"] = _with_pad_symbol(
        files["steps/pcb/layers/in1.cu/features"].decode(), 2, "r800.0")
    got = read_board(_zip(tmp_path / "via_pads.zip", files))

    with Job(ZIP) as job:
        pad = feat.parse(job.read("steps/pcb/layers/in1.cu/features"), MM).features[2]
    before = {v.position: v for v in snap.vias}
    after = {v.position: v for v in got.vias}
    assert before.keys() == after.keys()
    changed = [p for p in before if before[p] != after[p]]
    assert changed == [pytest.approx(pad.at)]
    old, new = before[changed[0]], after[changed[0]]
    assert new.layers == old.layers and len(old.pad_mm) == len(old.layers) == 4
    gone = set(old.pad_mm) - set(new.pad_mm)
    assert len(gone) == 1
    (lid,) = gone
    assert new.pad_diameter(lid) == 0.0
    grown = [l for l in new.pad_mm if new.pad_mm[l] != old.pad_mm[l]]
    assert len(grown) == 1 and new.pad_mm[grown[0]] == pytest.approx(0.8)
    assert new.diameter_mm == pytest.approx(0.8)
    assert all(new.pad_mm[l] == old.pad_mm[l] for l in new.pad_mm if l != grown[0])


# --- copper with no net ----------------------------------------------------------------------

def _with_features(text: str, extra) -> str:
    lines = text.rstrip("\n").splitlines()
    i = next(i for i, l in enumerate(lines) if re.match(r"^F \d+$", l))
    lines[i] = f"F {int(lines[i][2:]) + len(extra)}"
    return "\n".join(lines + extra) + "\n"


def test_netless_copper_takes_its_net_pass_after_pass(tmp_path, snap):
    """A fill touching the GND via at (60, 40), and an outline stroke touching only the
    fill: the stroke takes GND once the fill has it. Neither is in eda/data (no net)."""
    gnd = next(v for v in snap.vias if v.position == (60.0, 40.0))
    assert gnd.net == "GND"
    # ODB++ is y up: the board's (x, y) is (x, -y) in the file.
    fill = ["S P 0", "OB 60.1 -39.9 I", "OS 62 -39.9", "OS 62 -40.1", "OS 60.1 -40.1",
            "OS 60.1 -39.9", "OE", "SE"]
    stroke = ["L 62 -40 64 -40 1 P 0"]                  # $1 is r200.0
    files = _files()
    cu = files["steps/pcb/layers/f.cu/features"].decode()
    assert "\n$1 r200.0\n" in cu
    files["steps/pcb/layers/f.cu/features"] = _with_features(cu, fill + stroke)
    got = read_board(_zip(tmp_path / "netless.zip", files))

    old = {z.id for z in snap.zones}
    new = [z for z in got.zones if z.id not in old]
    assert len(new) == 2
    assert all(z.net == "GND" and set(z.filled) == {F_CU} for z in new)
    xs = sorted(p[0] for z in new for poly in z.filled[F_CU] for p in poly.outline)
    assert xs[0] == pytest.approx(60.1) and xs[-1] == pytest.approx(64.1, abs=0.01)
    def guessed(s):
        note = next((a for a in s.assumed if a.startswith("Copper drawings:")), "")
        return int(note.split()[2]) if note else 0
    assert guessed(got) == guessed(snap) + 2
