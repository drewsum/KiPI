"""Part 3D models through kicad-cli, with a stand-in kicad-cli so no KiCad is needed."""

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyvista")

from glb_writer import TRI  # noqa: E402
from kipi.core.kicad_io import models3d  # noqa: E402
from kipi.core.model.board import BoardSnapshot, Stackup  # noqa: E402
from kipi.core.progress import Canceled, ProgressReporter  # noqa: E402
from kipi.core.viz.glb import Part  # noqa: E402

FAKE_CLI = """
import json, os, sys, time
sys.path.insert(0, {tests!r})
from glb_writer import kicad_like
cfg = json.loads(os.environ["FAKE_CLI"])
args = sys.argv[1:]
with open(cfg["log"], "a") as f:
    f.write(json.dumps(args) + "\\n")
for opt in cfg.get("reject", []):
    if opt in args:
        print("Unknown argument: " + opt)
        sys.exit(1)
time.sleep(cfg.get("sleep", 0))
if cfg.get("fail"):
    print("Failed to load board")
    sys.exit(2)
refs = cfg.get("all", [])
if "--component-filter" in args:
    refs = [r for r in args[args.index("--component-filter") + 1].split(",") if r in refs]
for ref, why in cfg.get("missing", {{}}).items():
    if ref in refs:
        print("Could not add 3D model for " + ref + ".")
        print(why)
        refs.remove(ref)
open(args[args.index("-o") + 1], "wb").write(kicad_like(refs, cfg.get("named", True)))
print("Binary GLTF file created.")
"""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    script = tmp_path / "fake_kicad_cli.py"
    script.write_text(FAKE_CLI.format(tests=str(Path(__file__).parent)))
    log = tmp_path / "calls.jsonl"

    def configure(**cfg):
        cfg["log"] = str(log)
        monkeypatch.setenv("FAKE_CLI", json.dumps(cfg))
        return [sys.executable, str(script)]

    configure.calls = lambda: [json.loads(x) for x in log.read_text().splitlines()]
    return configure


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text("(kicad_pcb)")
    return path


def test_parts_load_with_their_models(cli, board):
    out = models3d.load_models(cli(all=["C1", "C2", "U1"]), board, ["C2", "C1", "C5"])
    assert set(out) == {"C1", "C2", "C5"}
    assert out["C5"] is None                    # not on the board / no model
    assert out["C2"].n_cells == 1 and "rgb" in out["C2"].cell_data
    call = cli.calls()[0]
    assert call[:3] == ["pcb", "export", "glb"]
    assert call[call.index("--component-filter") + 1] == "C1,C2,C5"
    assert "--no-board-body" in call and "--user-origin" in call
    assert call[-1] == str(board)


def test_an_older_kicad_cli_is_asked_with_fewer_options(cli, board):
    out = models3d.load_models(cli(all=["C1", "C2"], reject=["--user-origin"]), board, ["C1"])
    assert out["C1"] is not None
    calls = cli.calls()
    assert len(calls) == 2 and "--user-origin" not in calls[1]
    assert "--component-filter" in calls[1]

    # Without the filter every part comes back, and the wanted ones are picked out.
    out = models3d.load_models(cli(all=["C1", "C2"], reject=["--component-filter"]),
                               board, ["C2"])
    assert set(out) == {"C2"} and out["C2"] is not None
    assert "--component-filter" not in cli.calls()[-1]


def test_unnamed_parts_are_asked_for_one_at_a_time(cli, board):
    out = models3d.load_models(cli(all=["C1", "C2"], named=False), board, ["C1", "C2"])
    assert out["C1"] is not None and out["C2"] is not None
    assert len(cli.calls()) == 3                # all at once, then each on its own


def test_missing_models_are_reported(cli, board):
    said = []
    prog = ProgressReporter()
    prog.message = lambda text, level=0: said.append(text)
    out = models3d.load_models(
        cli(all=["C1", "C2"], missing={"C2": "File not found: ${X}/C.step"}), board,
        ["C1", "C2"], prog)
    assert out["C2"] is None
    assert any("No 3D model for C2: File not found: ${X}/C.step" in s for s in said)
    assert models3d.missing_models("Could not add 3D model for R7.\r\nFile not found: a\n") \
        == {"R7": "File not found: a"}


def test_a_failed_export_raises(cli, board):
    with pytest.raises(models3d.ModelError, match="Failed to load board"):
        models3d.load_models(cli(all=["C1"], fail=True), board, ["C1"])


def test_refs_kicad_cli_would_read_as_patterns_are_skipped(cli, board):
    out = models3d.load_models(cli(all=["C1"]), board, ["C1", "C*", "A,B", ""])
    assert out["C1"] is not None and out["C*"] is None and out["A,B"] is None
    assert cli.calls()[0][cli.calls()[0].index("--component-filter") + 1] == "C1"
    assert models3d.load_models(cli(), board, ["X?"]) == {"X?": None}


def test_cancel_stops_kicad_cli(cli, board):
    prog = ProgressReporter()
    threading.Timer(0.3, prog.cancel).start()
    t0 = time.perf_counter()
    with pytest.raises(Canceled):
        models3d.load_models(cli(all=["C1"], sleep=30), board, ["C1"], prog)
    assert time.perf_counter() - t0 < 10


def test_meshes_are_in_the_views_frame():
    # glTF: meters, Y up, board Y along Z. The view: millimeters, (x, -y, z).
    part = Part(points=np.array([[0.1, 0.002, 0.05], [0.101, 0.002, 0.05],
                                 [0.1, 0.003, 0.05]]),
                tris=np.array([[0, 1, 2]]), rgb=np.array([[1, 2, 3]], np.uint8),
                normals=np.array([[0.0, 1.0, 0.0]] * 2 + [[0.0, 0.0, 1.0]]))
    mesh = models3d.to_mesh(part)
    assert mesh.points[0] == pytest.approx([100.0, -50.0, 2.0])
    assert mesh.points[2] == pytest.approx([100.0, -50.0, 3.0])
    assert mesh.point_data["Normals"][0] == pytest.approx([0, 0, 1])     # up stays up
    assert mesh.point_data["Normals"][2] == pytest.approx([0, -1, 0])
    assert mesh.cell_data["rgb"].tolist() == [[1, 2, 3]]
    flat = models3d.to_mesh(Part(np.array(TRI, float), np.array([[0, 1, 2]]),
                                 np.zeros((1, 3), np.uint8)))
    assert "Normals" not in flat.point_data


def _snap(name, project_dir="", source="kicad"):
    return BoardSnapshot(name, project_dir, Stackup([], [], 1.6), [], [], [], [], [], [], [],
                         source=source)


def test_board_file_is_found_next_to_the_project_or_the_snapshot(tmp_path, board):
    assert models3d.board_file(_snap("b.kicad_pcb", str(tmp_path))) == board
    saved = tmp_path / "b.kipi.json.gz"
    assert models3d.board_file(_snap("b.kicad_pcb", "/gone", str(saved))) == board
    assert models3d.board_file(_snap("b.kicad_pcb", "/gone")) is None
    assert models3d.board_file(_snap("synthetic", str(tmp_path))) is None


def test_kicad_cli_is_found(tmp_path, monkeypatch):
    exe = tmp_path / "kicad-cli"
    exe.write_text("")
    monkeypatch.setenv(models3d.CLI_ENV, str(exe))
    assert models3d.find_kicad_cli() == [str(exe)]

    monkeypatch.delenv(models3d.CLI_ENV)
    monkeypatch.setattr(models3d.shutil, "which", lambda name: None)
    monkeypatch.setattr(models3d, "_installed_locations", lambda: [])
    monkeypatch.setattr(models3d.Path, "home", lambda: tmp_path)

    class KiCad:
        def get_kicad_binary_path(self, name):
            assert name == "kicad-cli"
            return str(exe)

    class OldKiCad:
        def get_kicad_binary_path(self, name):
            raise RuntimeError("no handler available")

    assert models3d.find_kicad_cli(KiCad()) == [str(exe)]
    assert models3d.find_kicad_cli(OldKiCad()) is None

    # A Flatpak KiCad is run through flatpak.
    (tmp_path / ".var" / "app" / models3d.FLATPAK_ID).mkdir(parents=True)
    monkeypatch.setattr(models3d.shutil, "which",
                        lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
    assert models3d.find_kicad_cli() == ["/usr/bin/flatpak", "run", "--command=kicad-cli",
                                         models3d.FLATPAK_ID]


def test_install_folders_are_listed_newest_first(tmp_path, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows install layout")
    for v in ("8.0", "10.0", "9.0"):
        (tmp_path / "KiCad" / v / "bin").mkdir(parents=True)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramW6432", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    found = models3d._installed_locations()
    assert [p.parent.parent.name for p in found] == ["10.0", "9.0", "8.0"]


def test_setup_lists_the_parts_it_models():
    from kipi.ac.model import CapModel, Probe
    from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm, VrmPhase
    setup = PISetup(
        vrms=[Vrm("U1", PinRef("L1", "VOUT"), 1.0, "smps", input=PinRef("U1", "VIN"),
                  sense=PinRef("U9", "VOUT"), phases=[VrmPhase(PinRef("L2", "VOUT"))])],
        loads=[Load("U2", PinRef("U2", "VOUT"), 1.0, ground=PinRef("U3", "GND"))],
        links=[DcLink("R1", PinRef("R1", "A"), PinRef("R1", "B"), 0.01)],
        caps=[CapModel("100n", 1e-7, 1e-9, 0.01, ["C1", "C2"])],
        probes=[Probe("P", PinRef("U7", "VOUT"), PinRef("U7", "GND"))])
    setup.loads.append(Load("empty", PinRef("", "VOUT"), 0.0))
    assert setup.part_refs() == {"L1", "L2", "U1", "U9", "U2", "U3", "R1", "C1", "C2"}
