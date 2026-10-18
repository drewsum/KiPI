"""Part models guessed from KiCad's model library by footprint name (#130)."""

import os
from pathlib import Path

import numpy as np
import pytest

from kipi.core.model.board import BoardSnapshot, Footprint, Pad, Poly
from kipi.sources.kicad_ipc import model_guess, models3d
from kipi.sources.kicad_ipc.model_guess import Library, board_text, footprint_angle
from kipi.sources.kicad_pcb import sexpr

MODELS = ["Capacitor_SMD.3dshapes/C_0402_1005Metric.step",
          "Capacitor_SMD.3dshapes/C_0603_1608Metric.step",
          "Resistor_SMD.3dshapes/R_0805_2012Metric.step",
          "Resistor_SMD.3dshapes/R_0805_2012Metric.wrl",
          "Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step",
          "Package_SO.3dshapes/TSSOP-20_4.4x6.5mm_P0.65mm.step"]


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "3dmodels"
    for m in MODELS:
        (root / m).parent.mkdir(parents=True, exist_ok=True)
        (root / m).write_text("")
    return Library.read(root)


@pytest.mark.parametrize("name, model, how", [
    ("Capacitor_SMD:C_0402_1005Metric", "C_0402_1005Metric", "name"),
    ("c_0402_1005metric", "C_0402_1005Metric", "name"),
    ("Capacitor_SMD_C_0402_1005Metric", "C_0402_1005Metric", "name"),  # ODB++ from KiCad
    ("CAPC1608X90N", "C_0603_1608Metric", "size code"),                # IPC-7351: metric
    ("C0603", "C_0603_1608Metric", "size code"),                       # imperial
    ("RES_0805", "R_0805_2012Metric", "size code"),
    ("SOIC-8_3.9x4.9mm_P1.27mm_HandSolder", "SOIC-8_3.9x4.9mm_P1.27mm", "start of name"),
    ("TSSOP20_4.4x6.5mm_P0.65mm", "TSSOP-20_4.4x6.5mm_P0.65mm", "name"),
    ("TSOP-20_4.4x6.5mm_P0.65mm", "TSSOP-20_4.4x6.5mm_P0.65mm", "closest name"),
    ("SO8", None, ""), ("U1", None, ""), ("", None, ""), ("C9999", None, ""),
])
def test_footprint_names_find_their_model(library, name, model, how):
    path, why = library.match(name)
    assert (path.stem if path else None, why) == (model, how)


def test_step_is_preferred_over_vrml(library):
    assert library.match("R_0805_2012Metric")[0].suffix == ".step"


def test_the_library_is_found_from_kicads_variables_and_kicad_cli(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.endswith("_3DMODEL_DIR")]:
        monkeypatch.delenv(k)
    v9, v10 = tmp_path / "v9", tmp_path / "v10"
    v9.mkdir()
    v10.mkdir()
    monkeypatch.setenv("KICAD9_3DMODEL_DIR", str(v9))
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(v10))
    beside = tmp_path / "KiCad" / "share" / "kicad" / "3dmodels"
    beside.mkdir(parents=True)
    cli = [str(tmp_path / "KiCad" / "bin" / "kicad-cli.exe")]
    assert model_guess.library_dirs(cli)[:3] == [v10, v9, beside]    # newest KiCad first


def _fp(ref="C1", side="top", angle=None, pads=()):
    return Footprint(id=ref, reference=ref, value="", position=(10.0, 20.0), side=side,
                     pad_ids=[p.id for p in pads], footprint="C0402", angle_deg=angle)


def _pad(pid, x, angle=None, half=0.3):
    sq = [(x - half, -half), (x + half, -half), (x + half, half), (x - half, half)]
    return Pad(id=pid, net="N", footprint="C1", number=pid, position=(x, 0.0),
               pad_type="smd", shapes={0: [Poly(sq)]}, angle_deg=angle)


def test_a_snapshot_without_footprint_angles_takes_its_pads():
    pads = [_pad("1", 0, 90.0), _pad("2", 1, 90.0), _pad("3", 2, 0.0)]
    by_id = {p.id: p for p in pads}
    assert footprint_angle(_fp(angle=45.0, pads=pads), by_id) == 45.0
    assert footprint_angle(_fp(pads=pads), by_id) == 90.0
    assert footprint_angle(_fp(), by_id) == 0.0


def test_the_board_places_each_part_with_its_model():
    text = board_text([(_fp("C1", "top"), 30.0, "C:/lib/C.step"),
                       (_fp('Q"2', "bottom"), 90.0, "/lib/Q.step")], 1.6)
    root = sexpr.parse(text)
    fps = list(sexpr.children(root, "footprint"))
    assert [f[1] for f in fps] == ["C1", 'Q"2']
    assert [sexpr.value(f, "layer") for f in fps] == ["F.Cu", "B.Cu"]
    assert [sexpr.xy(sexpr.child(f, "at")) for f in fps] == [(10.0, 20.0)] * 2
    assert [float(sexpr.child(f, "at")[3]) for f in fps] == [30.0, 90.0]
    assert [sexpr.value(f, "model") for f in fps] == ["C:/lib/C.step", "/lib/Q.step"]
    assert sexpr.number(sexpr.child(root, "general"), "thickness") == 1.6


def _snap(footprints, pads, thickness=1.2):
    snap = BoardSnapshot.__new__(BoardSnapshot)
    snap.footprints, snap.pads = footprints, pads
    snap.stackup = type("Stackup", (), {"board_thickness_mm": thickness})()
    return snap


class _Log:
    def __init__(self):
        self.said = []

    def info(self, text):
        self.said.append(text)

    def check_cancel(self):
        pass


def test_a_guess_that_does_not_fit_its_pads_is_left_out(library, monkeypatch):
    pv = pytest.importorskip("pyvista")
    pads = [_pad("1", 0), _pad("2", 1)]                   # about 1.6 x 0.6 mm
    snap = _snap([Footprint("a", "C1", "", (5, 5), "top", ["1", "2"], "C0402", 0.0),
                  Footprint("b", "C2", "", (9, 5), "bottom", ["1", "2"], "C0402", 90.0),
                  Footprint("c", "C3", "", (1, 1), "top", ["1", "2"],
                            "Capacitor_SMD:C_0402_1005Metric"),
                  Footprint("d", "U1", "", (1, 1), "top", [], "mystery")], pads)
    seen = {}

    def box(w, h):
        return pv.Box(bounds=(0, w, 0, h, 0, 1))

    def load(cli, board, refs, progress=None):
        seen["board"] = Path(board).read_text(encoding="utf-8")
        seen["refs"] = list(refs)
        return {"C1": box(1.0, 0.5), "C2": box(12.0, 9.0), "C3": box(12.0, 9.0)}

    monkeypatch.setattr(models3d, "load_models", load)
    log = _Log()
    got = model_guess.GuessedModels(["kicad-cli"], library, snap).load(
        ["C1", "C2", "C3", "U1"], log)
    assert seen["refs"] == ["C1", "C2", "C3"]              # U1: no model like it
    assert "(thickness 1.2000)" in seen["board"]
    assert got["C1"] is not None and got["U1"] is None
    assert got["C2"] is None                               # far larger than its pads
    assert got["C3"] is not None                           # found by name: kept as is
    assert any("C1" in s and "guessed C_0402_1005Metric by size code" in s for s in log.said)
    assert any("C2" in s and "does not fit" in s for s in log.said)


def test_installed_kicad_guesses_only_when_asked(tmp_path, monkeypatch):
    from kipi.sources.kicad_ipc.source import InstalledKiCad, KiCadPartModels
    monkeypatch.setattr(models3d, "find_kicad_cli", lambda kicad=None: ["kicad-cli"])
    monkeypatch.setattr(models3d, "board_file", lambda snap: None)
    snap = type("Snap", (), {"board_name": "b.zip"})()
    tool = InstalledKiCad()
    provider, why = tool.part_models(snap)
    assert provider is None and "Guess models" in why     # never guessed on its own
    monkeypatch.setattr(model_guess, "library_dirs", lambda cli: [])
    provider, why = tool.guessed_models(snap)
    assert provider is None and "library" in why
    lib = tmp_path / "3dmodels" / "Capacitor_SMD.3dshapes"
    lib.mkdir(parents=True)
    (lib / "C_0402_1005Metric.step").write_text("")
    monkeypatch.setattr(model_guess, "library_dirs", lambda cli: [lib.parent])
    provider, why = tool.guessed_models(snap)
    assert isinstance(provider, model_guess.GuessedModels) and why == ""

    assert KiCadPartModels().offers_part_models
    monkeypatch.setattr(models3d, "find_kicad_cli", lambda kicad=None: None)
    assert not KiCadPartModels().offers_part_models       # no KiCad: no models offered


def _real_kicad():
    cli = models3d.find_kicad_cli()
    dirs = model_guess.library_dirs(cli) if cli else []
    return (cli, Library.read(dirs[0])) if dirs else (None, None)


@pytest.mark.skipif(_real_kicad()[0] is None, reason="needs KiCad and its 3D model library")
def test_kicad_places_the_guessed_models():
    pytest.importorskip("pyvista")
    cli, lib = _real_kicad()
    snap = _snap([Footprint("a", "C1", "", (30.0, 30.0), "top", [], "C_0805_2012Metric", 90.0),
                  Footprint("b", "C2", "", (50.0, 40.0), "bottom", [],
                            "Capacitor_SMD:C_0805_2012Metric", 0.0)], [], 1.6)
    got = model_guess.GuessedModels(cli, lib, snap).load(["C1", "C2"])
    c1, c2 = got["C1"].bounds, got["C2"].bounds
    assert np.mean(c1[:2]) == pytest.approx(30.0, abs=0.05)
    assert np.mean(c1[2:4]) == pytest.approx(-30.0, abs=0.05)      # the view flips Y
    assert c1[3] - c1[2] > c1[1] - c1[0]                   # turned 90°: long along Y
    assert c1[4] >= 1.55                                   # on top of the board
    assert c2[5] <= 0.05 and c2[1] - c2[0] > c2[3] - c2[2]  # under it, unturned
