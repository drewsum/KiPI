"""Mesh nets' copper layers in several processes (#211).

Meshing a layer of copper is shapely and Triangle, which hold the GIL, and a large
board's ground is seconds a plane layer on one core. Each layer is meshed apart from the
others; only joining them through the plated holes needs them all. So a large mesh's
layers go to a :class:`~kipi.core.workers.BoardPool`, and each net is joined here, in
stackup order, as soon as its layers are in: the mesh is the same as one meshed here.

Wherever a pool can't start or breaks, the layers left are meshed here as before.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from kipi.core import workers
from kipi.core.mesh.mesher import LayerMesh, MeshError, MeshOptions, mesh_layer
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import ProgressReporter

#: Below this many triangles (estimated) a pool's start, a second or so, costs more than
#: it saves: one core meshes about 300 k a second. Nor is a pool used where all but this
#: many of them are on one layer, which is meshed in one process either way.
MIN_TRIANGLES = 1_500_000
MIN_SPREAD = 500_000


class _Notes(ProgressReporter):
    """What a worker's mesher says, kept to be said in order by the main process."""

    def __init__(self):
        super().__init__()
        self.said: List[Tuple[int, str]] = []

    def message(self, text: str, level: int = logging.INFO):
        self.said.append((level, text))


def _layer(net: str, ids: Sequence[str], lid: int, opts: MeshOptions, features: np.ndarray):
    notes = _Notes()
    return mesh_layer(workers.board(), net, lid, ids, opts, features, notes), notes.said


def _area(poly) -> float:
    def ring(r):
        xy = np.asarray(r, dtype=float).reshape(-1, 2)
        if len(xy) < 3:
            return 0.0
        x, y = xy[:, 0], xy[:, 1]
        return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2
    return ring(poly.outline) - sum(ring(h) for h in poly.holes)


def estimate(snap: BoardSnapshot, net: str, lid: int, opts: MeshOptions,
             n_holes: int) -> float:
    """About how many triangles ``net``'s copper on ``lid`` meshes into, with ``n_holes``
    plated holes graded: the planes' area over the largest triangle's (twice, for
    Triangle's quality refinement), and the rings of finer triangles around the holes,
    most of a large ground's mesh. Tracks are left out; they are a small part of any
    mesh worth a pool."""
    _, _, _, zones = snap.items_on_net(net)
    area = sum(_area(p) for z in zones for p in z.filled.get(lid, ()))
    return 2 * area / opts.max_area + n_holes * _per_hole(opts)


def _per_hole(opts: MeshOptions) -> float:
    """Triangles around one graded hole: edges grow from ``min_edge_mm`` by ``grading``
    a mm until ``max_edge_mm``, the integral of 2πr / (√3/4 h(r)²) out to there, times
    0.6 for neighbouring holes' rings overlapping (fitted on a large board's ground)."""
    lo, hi, g = opts.min_edge_mm, opts.max_edge_mm, opts.grading
    if g <= 0 or lo <= 0 or hi <= lo:
        return 0.0
    return 0.6 * 2 * math.pi / (math.sqrt(3) / 4 * g * g) * (math.log(hi / lo) + lo / hi - 1)


class Pooled:
    """Layers being meshed in a pool; :meth:`layers` gives a net's as they come in."""

    def __init__(self, snap: BoardSnapshot, jobs, opts: MeshOptions, features,
                 tasks: List[Tuple[str, int]], n: int):
        self.snap, self.opts, self.features = snap, opts, features
        self.ids = dict(jobs)
        self.pool = workers.BoardPool(snap, n)
        self.futures: Dict[Tuple[str, int], cf.Future] = {}
        try:
            for net, lid in tasks:
                self.futures[net, lid] = self.pool.submit(
                    _layer, net, self.ids[net], lid, opts, features[net, lid][0])
        except BaseException:
            self.close()
            raise
        self.broken = False

    def layers(self, net: str, progress: ProgressReporter) -> List[Optional[LayerMesh]]:
        """``net``'s layers in stackup order, what the workers said about them said."""
        out = []
        for cu in self.snap.stackup.copper:
            f = self.futures.get((net, cu.id))
            if f is not None and not self.broken:
                try:
                    for _ in workers.finished({f}, progress):
                        pass
                    meshed, said = f.result()
                    for level, text in said:
                        progress.message(text, level)
                    out.append(meshed)
                    continue
                except MeshError:
                    raise
                except workers.POOL_ERRORS as e:
                    self.broken = True
                    progress.warn(f"Could not mesh in processes ({e}); meshing the layers "
                                  "left in one")
            progress.check_cancel()
            out.append(mesh_layer(self.snap, net, cu.id, self.ids[net], self.opts,
                                  self.features[net, cu.id][0], progress))
        return out

    def close(self):
        self.pool.__exit__(None, None, None)


def mesh_layers(snap: BoardSnapshot, jobs: Sequence[Tuple[str, Sequence[str]]],
                opts: MeshOptions, features, progress: ProgressReporter) -> Optional[Pooled]:
    """``jobs``' layers started in a pool, the largest first; None to mesh them here (a
    small mesh, or no pool to be had)."""
    sizes = {(net, cu.id): estimate(snap, net, cu.id, opts, len(features[net, cu.id][0]))
             for net, _ in jobs for cu in snap.stackup.copper}
    total = sum(sizes.values())
    if total < MIN_TRIANGLES or total - max(sizes.values()) < MIN_SPREAD:
        return None
    n = workers.workers(sum(s > 0 for s in sizes.values()))
    if not n:
        return None
    tasks = sorted(sizes, key=sizes.get, reverse=True)
    try:
        pooled = Pooled(snap, jobs, opts, features, tasks, n)
    except workers.POOL_ERRORS as e:
        progress.warn(f"Could not mesh in {n} processes ({e}); meshing in one")
        return None
    progress.debug(f"Meshing {len(jobs)} net(s) in {n} processes")
    return pooled
