"""Nets' copper layers meshed in processes (#211): the same mesh as in one process, and
the meshing carried on here when a pool can't start or breaks."""

import concurrent.futures as cf
from concurrent.futures.process import BrokenProcessPool

import numpy as np

from kipi.core import workers
from kipi.core.mesh import parallel
from kipi.core.mesh.mesher import MeshOptions, mesh_many
from kipi.core.progress import ProgressReporter
from synthetic import make_board

JOBS = [("+3V3", ["p1", "p2"]), ("GND", ["p3"])]
OPTS = MeshOptions(max_edge_mm=0.8)


class Dead:
    """A pool whose every task fails as when its worker dies."""
    made = []

    def __init__(self, n, **kw):
        pass

    def submit(self, fn, *args):
        f = cf.Future()
        f.set_exception(BrokenProcessPool("a worker died"))
        Dead.made.append(f)
        return f

    def shutdown(self, **kw):
        pass


class Said(ProgressReporter):
    def __init__(self):
        super().__init__()
        self.said = []

    def message(self, text, level=0):
        self.said.append(text)


def pooled(monkeypatch, n=2):
    monkeypatch.setattr(parallel, "MIN_TRIANGLES", 0)
    monkeypatch.setattr(parallel, "MIN_SPREAD", 0)
    monkeypatch.setattr(workers, "workers", lambda n_tasks: n)


def assert_same(a, b):
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x.net == y.net and x.layers == y.layers
        assert np.array_equal(x.points, y.points) and np.array_equal(x.tris, y.tris)
        assert np.array_equal(x.node_layer, y.node_layer)
        assert [b.owner_id for b in x.barrels] == [b.owner_id for b in y.barrels]
        for bx, by in zip(x.barrels, y.barrels):
            assert bx.ring_nodes.keys() == by.ring_nodes.keys()
            assert all(np.array_equal(bx.ring_nodes[k], by.ring_nodes[k]) for k in bx.ring_nodes)
        assert x.pad_nodes.keys() == y.pad_nodes.keys()
        for pid in x.pad_nodes:
            assert all(np.array_equal(x.pad_nodes[pid][k], y.pad_nodes[pid][k])
                       for k in x.pad_nodes[pid])


def test_processes_mesh_what_one_does(monkeypatch):
    snap = make_board()
    here = Said()
    one = mesh_many(snap, JOBS, OPTS, here)
    pooled(monkeypatch)
    there = Said()
    many = mesh_many(snap, JOBS, OPTS, there)
    assert_same(one, many)
    assert "Meshing 2 net(s) in 2 processes" in there.said
    assert not any("Could not" in t for t in there.said)
    # Each layer's lines from the workers are said, in stackup order, under their net.
    def layer_lines(said):
        return [t for t in said if t.startswith(("--", "  "))]
    assert layer_lines(there.said) == layer_lines(here.said)
    assert len(layer_lines(here.said)) > 2


def test_a_pool_that_cannot_start_meshes_here(monkeypatch):
    snap = make_board()
    one = mesh_many(snap, JOBS, OPTS)
    pooled(monkeypatch)

    def broken(*a, **k):
        raise OSError("no processes here")
    monkeypatch.setattr(workers.cf, "ProcessPoolExecutor", broken)
    said = Said()
    assert_same(one, mesh_many(snap, JOBS, OPTS, said))
    assert any("no processes here" in t for t in said.said)


def test_a_pool_that_breaks_leaves_the_layers_left_here(monkeypatch):
    snap = make_board()
    one = mesh_many(snap, JOBS, OPTS)
    pooled(monkeypatch)
    Dead.made = []
    monkeypatch.setattr(workers.cf, "ProcessPoolExecutor", Dead)
    said = Said()
    assert_same(one, mesh_many(snap, JOBS, OPTS, said))
    assert Dead.made and sum("a worker died" in t for t in said.said) == 1


def test_small_meshes_and_one_big_layer_stay_here(monkeypatch):
    snap = make_board()
    monkeypatch.setattr(workers, "workers", lambda n_tasks: 4)
    monkeypatch.setattr(workers.cf, "ProcessPoolExecutor", Dead)
    features = {(net, cu.id): (np.zeros((0, 3)), 0) for net, _ in JOBS
                for cu in snap.stackup.copper}
    assert parallel.mesh_layers(snap, JOBS, OPTS, features, Said()) is None   # small
    monkeypatch.setattr(parallel, "MIN_TRIANGLES", 0)
    # The planes on In1 (GND) and In2 (+3V3) are the same size: spread over two layers.
    monkeypatch.setattr(parallel, "MIN_SPREAD", 1)
    p = parallel.mesh_layers(snap, JOBS[1:], OPTS, features, Said())          # one plane
    assert p is None
    p = parallel.mesh_layers(snap, JOBS, OPTS, features, Said())
    try:
        assert p is not None
    finally:
        p.close()


def test_estimate_counts_planes_and_graded_holes():
    snap = make_board()
    opts = MeshOptions(max_edge_mm=0.5, min_edge_mm=0.02, grading=0.2)
    lid = snap.stackup.copper[1].id                      # In1: GND's 20 x 10 mm plane
    plane = parallel.estimate(snap, "GND", lid, opts, 0)
    area = 200 - 1.0                                    # less the 1 mm square hole
    assert plane == np.float64(2 * area / opts.max_area)
    assert parallel.estimate(snap, "GND", lid, opts, 10) > plane + 10 * 100
    assert parallel.estimate(snap, "GND", snap.stackup.copper[2].id, opts, 0) == 0
