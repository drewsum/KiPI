"""Build the 3D view's nets in several processes (#210).

Each net's copper and meshes are built apart from every other's, mostly in Python and
shapely, which hold the GIL: on one core a large board takes half a minute. A pool of
worker processes, each given the board once, builds the nets in parallel and returns
their meshes as plain arrays (:class:`~kipi.core.viz.batch.Packed`), which cross between
processes in a fraction of the time a pyvista mesh takes.

Wherever a pool can't start or breaks, the nets left are built here as before.
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import multiprocessing
import os
import pickle
import sys
import tempfile
import threading
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union

from shapely.geometry.base import BaseGeometry

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.core.viz.meshes import NetScene

#: Below this many nets the pool's start (a second or two) costs more than it saves.
MIN_NETS = 40
#: Each worker holds the whole board; more than this rarely helps and costs memory.
MAX_WORKERS = 8
#: Set to 1 to build in one process only.
SERIAL_ENV = "KIPI_SERIAL_VIEW"

_snap: Optional[BoardSnapshot] = None      # in a worker: the board it builds nets of


def workers(n_nets: int) -> int:
    """How many processes to build ``n_nets`` nets with; 0 to build them here."""
    if n_nets < MIN_NETS or os.environ.get(SERIAL_ENV) == "1":
        return 0
    if not _can_spawn():
        return 0
    n = min((os.cpu_count() or 1) - 1, MAX_WORKERS, n_nets // 8)
    return n if n >= 2 else 0


def _renamed() -> bool:
    """Whether this is KiPI.exe: a copy of Python under KiPI's name (#124)."""
    return Path(sys.executable or "").stem.lower() == "kipi"


def _can_spawn() -> bool:
    """Whether a worker can be started by running this program again: a Python, or
    KiPI.exe. KiCad's own Python, embedded in it, can't."""
    return Path(sys.executable or "").stem.lower().startswith("python") or _renamed()


def _spawn_renamed():
    """Let a worker start as KiPI.exe. Python's multiprocessing takes a venv's program
    for its redirector to the base Python and starts that instead, under the venv
    program's name: a KiPI.exe that isn't there. KiPI.exe is the interpreter itself and
    needs no such detour."""
    try:
        from multiprocessing import popen_spawn_win32
        popen_spawn_win32.WINENV = False
    except ImportError:                 # not Windows
        pass


def _start(board: str, colors):
    global _snap
    from kipi.core.viz import meshes
    with open(board, "rb") as f:
        _snap = pickle.load(f)
    meshes.set_layer_colors(colors)


def _build(net: str, copper: Optional[Dict[int, BaseGeometry]],
           layers: Optional[List[int]] = None, holes: bool = True):
    """One net's copper and packed scene; with ``layers``, only the copper on those, and
    the vias and pads only if ``holes``: one piece of a net built in several."""
    from kipi.core.geometry.copper import net_copper, net_layer_copper
    from kipi.core.viz.batch import pack_scene
    from kipi.core.viz.meshes import build_net_scene
    if copper is None and layers is None:
        copper = net_copper(_snap, net)
    elif copper is None:
        copper = {lid: g for lid, g in ((lid, net_layer_copper(_snap, net, lid))
                                        for lid in layers) if not g.is_empty}
    return net, copper, pack_scene(build_net_scene(_snap, net, copper, holes=holes))


class Coatings:
    """The solder masks and silkscreens, from :func:`build_nets`: ``(kind, mesh, color)``
    each, as :func:`~kipi.core.viz.meshes.board_coatings` makes them."""

    def __init__(self, coatings):
        self.coatings = coatings


def _coatings(side: str):
    from kipi.core.viz.batch import pack
    from kipi.core.viz.meshes import board_coatings
    return [(kind, pack(mesh), color) for kind, mesh, color in board_coatings(_snap, [side])]


def build_nets(snap: BoardSnapshot, nets: List[str], cache: Dict[str, Dict],
               progress: ProgressReporter = NULL, coatings: bool = False
               ) -> Iterator[Union[NetScene, Coatings]]:
    """Each of ``nets``' scenes as it is built, in any order, and with ``coatings`` the
    board's :class:`Coatings`. The nets' copper goes in ``cache`` (net -> layer ->
    geometry), and copper already there is used."""
    left = list(nets)
    wanted = [coatings]                 # still to build
    n = workers(len(left))
    if n:
        try:
            yield from _in_pool(snap, left, cache, n, progress, wanted)
        except (OSError, RuntimeError, BrokenProcessPool) as e:
            progress.warn(f"Could not build the view in {n} processes ({e}); building "
                          f"the {len(left)} net(s) left in one")
    from kipi.core.geometry.copper import net_copper
    from kipi.core.viz.meshes import build_net_scene
    for net in left:
        progress.check_cancel()
        if net not in cache:
            cache[net] = net_copper(snap, net, progress)
        yield build_net_scene(snap, net, cache[net], progress)
    if wanted[0]:                       # hidden until asked for, so after the copper
        from kipi.core.viz.meshes import board_coatings
        progress.check_cancel()
        with progress.timed("Building solder masks and silkscreens"):
            yield Coatings(board_coatings(snap))


def _in_pool(snap, left: List[str], cache, n: int, progress, wanted: list):
    """Build ``left`` in ``n`` processes, removing each net from it once yielded, and the
    coatings if ``wanted[0]``, clearing it once they are."""
    from kipi.core.viz import meshes
    # The workers read the board from a file. Passed to them as they start, it is
    # written down a pipe that blocks for good if a worker exits before reading it. The
    # copy leaves out the snapshot's indexes, which each worker builds for itself.
    fd, board = tempfile.mkstemp(prefix="kipi-board-", suffix=".pkl")
    with os.fdopen(fd, "wb") as f:
        pickle.dump(dataclasses.replace(snap), f, protocol=pickle.HIGHEST_PROTOCOL)
    # Spawned everywhere: forking a process with Qt's threads running is unsafe.
    if _renamed():
        _spawn_renamed()
    pool = cf.ProcessPoolExecutor(n, mp_context=multiprocessing.get_context("spawn"),
                                  initializer=_start,
                                  initargs=(board, dict(meshes._layer_colors)))
    try:
        progress.debug(f"Building {len(left)} net(s) in {n} processes")
        # The biggest nets first, so a ground plane isn't left to finish on its own; a
        # net bigger than a worker's share is built a layer to a task.
        size = {net: sum(map(len, snap.items_on_net(net))) for net in left}
        share = sum(size.values()) / n
        pending, pieces = set(), {}
        # The coatings first, a side to a task: they are the longest tasks.
        coats = [pool.submit(_coatings, side) for side in ("top", "bottom")] if wanted[0] else []
        pending.update(coats)
        coated: Dict[int, list] = {}
        for net in sorted(left, key=size.get, reverse=True):
            if size[net] > share and net not in cache:
                lids = [cu.id for cu in snap.stackup.copper]
                pieces[net] = [len(lids) + 1, {}, None]      # pieces left, copper, scene
                pending |= {pool.submit(_build, net, None, [lid], False) for lid in lids}
                pending.add(pool.submit(_build, net, None, [], True))   # vias and pads
            else:
                pending.add(pool.submit(_build, net, cache.get(net)))
        while pending:
            progress.check_cancel()
            done, pending = cf.wait(pending, timeout=0.2, return_when=cf.FIRST_COMPLETED)
            for f in done:
                if f in coats:
                    coated[coats.index(f)] = f.result()
                    if len(coated) == len(coats):
                        from kipi.core.viz.batch import unpack
                        wanted[0] = False
                        yield Coatings([(kind, unpack(m), color) for k in sorted(coated)
                                        for kind, m, color in coated[k]])
                    continue
                net, copper, ns = f.result()
                if net in pieces:
                    ns = _join(snap, pieces[net], copper, ns)
                    if ns is None:
                        continue
                    copper = pieces.pop(net)[1]
                cache[net] = copper
                left.remove(net)
                yield ns
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        _remove_later(board, pool)


def _join(snap, piece: list, copper, ns) -> Optional[NetScene]:
    """Add one piece of a net to what has come of it (``[pieces left, copper, scene]``);
    once all have, the whole net's scene, its copper in stackup order."""
    piece[0] -= 1
    piece[1].update(copper)
    if piece[2] is None:
        piece[2] = ns
    else:
        piece[2].copper += ns.copper
        if ns.vias is not None or ns.pads:
            piece[2].vias, piece[2].pads = ns.vias, ns.pads
    if piece[0]:
        return None
    order = {cu.id: k for k, cu in enumerate(snap.stackup.copper)}
    piece[1] = dict(sorted(piece[1].items(), key=lambda kv: order[kv[0]]))
    piece[2].copper.sort(key=lambda lm: order[lm.layer_id])
    return piece[2]


def _remove_later(path: str, pool):
    """Delete the board file once the workers have read it: on Windows not while one
    still has it open, so after they stop, from a thread rather than waiting here."""
    def remove():
        pool.shutdown(wait=True)
        try:
            os.remove(path)
        except OSError:
            pass
    threading.Thread(target=remove, name="kipi-pool-cleanup", daemon=True).start()
