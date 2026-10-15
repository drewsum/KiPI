"""Results files saved by earlier KiPI versions, before the modules moved into
kipi.core and kipi.dc, and results of electro-thermal solves."""
import gzip
import pickle
import re

import pytest

from kipi import compat
from kipi.dc.pi.report import RESULTS_FORMAT, load_results, save_results
from kipi.dc.thermal.cosim import solve_electrothermal
from kipi.dc.thermal.model import ThermalSettings
from kipi.dc.viz.results import build_result_scene
from tree_boards import board, setup


@pytest.mark.parametrize("old, new", [
    (("kipi.pi.tree", "TreeResult"), ("kipi.dc.pi.tree", "TreeResult")),
    (("kipi.solver.post", "ViaResult"), ("kipi.dc.solver.post", "ViaResult")),
    (("kipi.solver.direct", "DirectFactor"), ("kipi.core.solver.direct", "DirectFactor")),
    (("kipi.solver.fem", "_Factor"), ("kipi.core.solver.spd", "SpdSolver")),
    (("kipi.viz.results", "ResultScene"), ("kipi.dc.viz.results", "ResultScene")),
    (("kipi.viz.meshes", "NetScene"), ("kipi.core.viz.meshes", "NetScene")),
    (("kipi.mesh.mesher", "NetMesh"), ("kipi.core.mesh.mesher", "NetMesh")),
    (("kipi.model.board", "Pad"), ("kipi.core.model.board", "Pad")),
    (("kipi.thermal.cosim", "ThermalResult"), ("kipi.dc.thermal.cosim", "ThermalResult")),
    (("kipi.dc.pi.tree", "TreeResult"), ("kipi.dc.pi.tree", "TreeResult")),
    (("numpy", "ndarray"), ("numpy", "ndarray")),
])
def test_moved_modules(old, new):
    assert compat.moved(*old) == new


def _solved():
    snap = board()
    s = setup("ldo", iq=0.01, ground=True)
    s.thermal = ThermalSettings(enabled=True, iterations=2)
    tree = solve_electrothermal(snap, s, s.thermal)
    temp = tree.thermal.sampler()
    scenes = {n: build_result_scene(snap, g.result, temperature=temp)
              for n, g in tree.groups.items()}
    return snap, s, tree, scenes


@pytest.fixture(scope="module")
def solved():
    return _solved()


def test_electrothermal_results_save_and_load(solved, tmp_path):
    snap, s, tree, scenes = solved
    back = load_results(save_results(tmp_path / "r.kipi-results", tree, scenes, snap, s))
    assert back["tree"].thermal.t_max == pytest.approx(tree.thermal.t_max)
    assert [l.drop for l in back["tree"].loads] == [l.drop for l in tree.loads]
    # The live temperature field is not saved; its effect on resistivity is.
    r = back["tree"].groups[tree.loads[0].group].result.solution
    assert r.problem.temperature is None and r.tri_rho_factor is not None


class _OldPaths(pickle._Pickler):
    """Names classes by where 0.6 kept them: kipi.core.x and kipi.dc.x were kipi.x."""

    def save_global(self, obj, name=None):
        name = name or getattr(obj, "__qualname__", None) or obj.__name__
        module = pickle.whichmodule(obj, name)
        if not module.startswith("kipi."):
            return super().save_global(obj, name)
        self.save(re.sub(r"^kipi\.(?:core|dc)\.", "kipi.", module))
        self.save(name)
        self.write(pickle.STACK_GLOBAL)
        self.memoize(obj)


def test_results_saved_with_the_old_module_paths_still_load(solved, tmp_path):
    snap, s, tree, scenes = solved
    payload = {"format": RESULTS_FORMAT, "saved": "2026-01-01 00:00:00",
               "board": snap.board_name, "tree": tree, "scenes": scenes, "setup": s}
    path = tmp_path / "old.kipi-results"
    with gzip.open(path, "wb") as fh:
        _OldPaths(fh, protocol=4).dump(payload)
    with gzip.open(path, "rb") as fh:
        old = fh.read()
    assert b"kipi.pi.results" in old and b"kipi.dc." not in old and b"kipi.core." not in old
    with pytest.raises(ModuleNotFoundError):
        pickle.loads(old)
    back = load_results(path)
    assert type(back["tree"]).__module__ == "kipi.dc.pi.results"
    assert [l.drop for l in back["tree"].loads] == [l.drop for l in tree.loads]
    name = next(iter(scenes))
    assert type(back["scenes"][name]).__module__ == "kipi.dc.viz.results"
