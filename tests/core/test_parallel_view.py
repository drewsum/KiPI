"""The 3D view's nets, and its solder masks and silkscreens, built in processes (#210):
the same meshes as in one process, a net bigger than a worker's share built a layer to a
task, and the build carried on here when a pool can't start."""

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyvista")

from kipi.core.model.board import MASK, Coating
from kipi.core import workers
from kipi.core.viz import parallel
from kipi.core.viz.batch import _arrays
from kipi.core.viz.parallel import Coatings, build_nets
from synthetic import make_board


def board():
    snap = make_board()
    snap.stackup.coatings = [Coating(MASK, "top", 0.02), Coating(MASK, "bottom", 0.015)]
    return snap


def built(snap, nets, **kw):
    cache = {}
    out = list(build_nets(snap, nets, cache, coatings=True, **kw))
    scenes = {ns.net: ns for ns in out if not isinstance(ns, Coatings)}
    coatings, = [c.coatings for c in out if isinstance(c, Coatings)]
    return scenes, coatings, cache


def same(a, b):
    pa, ta = _arrays(a)
    pb, tb = _arrays(b)
    return np.allclose(pa, pb) and (ta == tb).all()


class Said(parallel.ProgressReporter):
    def __init__(self, said):
        super().__init__()
        self.said = said

    def message(self, text, level=0):
        self.said.append(text)


def test_processes_build_what_one_does(monkeypatch):
    snap = board()
    nets = snap.net_names()
    one = built(snap, nets)
    monkeypatch.setattr(parallel, "workers", lambda n: 2)
    said = []
    many = built(snap, nets, progress=Said(said))
    assert said == [f"Building {len(nets)} net(s) in 2 processes"]       # no fallback
    for (s1, c1, k1), (s2, c2, k2) in [(one, many)]:
        assert set(s1) == set(s2) == set(nets)
        for net in nets:
            a, b = s1[net], s2[net]
            assert [lm.layer_id for lm in a.copper] == [lm.layer_id for lm in b.copper]
            assert all(same(x.mesh, y.mesh) and x.color == y.color
                       for x, y in zip(a.copper, b.copper))
            assert a.pads.keys() == b.pads.keys()
            assert all(same(a.pads[k], b.pads[k]) for k in a.pads)
            assert (a.vias is None) == (b.vias is None)
            assert a.vias is None or same(a.vias, b.vias)
            assert list(k1[net]) == list(k2[net])            # layers in stackup order
            assert all(k1[net][lid].equals(k2[net][lid]) for lid in k1[net])
        assert [(k, col) for k, _, col in c1] == [(k, col) for k, _, col in c2]
        assert all(same(m1, m2) for (_, m1, _), (_, m2, _) in zip(c1, c2))
        assert len(c1) == 2


def test_a_pool_that_cannot_start_leaves_the_build_here(monkeypatch):
    snap = board()
    monkeypatch.setattr(parallel, "workers", lambda n: 2)

    def broken(*a, **k):
        raise OSError("no processes here")
    monkeypatch.setattr(workers.cf, "ProcessPoolExecutor", broken)
    warnings = []
    scenes, coatings, _ = built(snap, snap.net_names(), progress=Said(warnings))
    assert set(scenes) == set(snap.net_names()) and len(coatings) == 2
    assert any("no processes here" in w for w in warnings)


def test_small_boards_and_non_pythons_build_here(monkeypatch):
    assert parallel.workers(parallel.MIN_NETS - 1) == 0
    monkeypatch.setattr(workers, "spare", lambda: 19)
    assert parallel.workers(1000) == 19
    monkeypatch.setenv(workers.SERIAL_ENV, "1")
    assert parallel.workers(1000) == 0
    monkeypatch.delenv(workers.SERIAL_ENV)
    # Paths in this system's own form, as sys.executable is.
    monkeypatch.setattr(workers.sys, "executable", str(Path("KiCad", "bin", "kicad.exe")))
    assert parallel.workers(1000) == 0
    monkeypatch.setattr(workers.sys, "executable", str(Path("venv", "Scripts", "KiPI.exe")))  # #124
    assert parallel.workers(1000) == 19
