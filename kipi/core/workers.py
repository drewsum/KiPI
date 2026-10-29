"""Worker processes that each hold a copy of the board (#210, #211).

The 3D view's nets and the solvers' meshes are built mostly in Python, shapely and
Triangle, which hold the GIL: one process uses one core. A :class:`BoardPool` starts
worker processes, gives each the board once, and runs tasks that read it through
:func:`board`. Their results should be plain arrays: they cross between processes in a
fraction of the time objects take.

A pool has a process for each spare core, as many as half the memory free holds (#226).

Wherever a pool can't start or breaks (:data:`POOL_ERRORS`), the callers do the work left
in this process.
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
from typing import Iterator, Optional, Set

from kipi.core.cores import spare
from kipi.core.memory import free_memory
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter

#: A worker's memory: Python and its libraries, plus this many times the board's file
#: (its copy of the board, its indexes and what it builds; Thor's demo: 21 MB, ~370 MB).
WORKER_BYTES = 256 * 2**20
WORKER_BOARD_FILES = 16
#: Of the memory free, the share the workers may take.
MEMORY_SHARE = 0.5
#: Most workers where the free memory can't be read.
UNKNOWN_MEMORY_WORKERS = 8
#: Set to 1 to do everything in one process.
SERIAL_ENV = "KIPI_SERIAL"
#: What a pool that can't start or has broken raises.
POOL_ERRORS = (OSError, RuntimeError, BrokenProcessPool)

_snap: Optional[BoardSnapshot] = None      # in a worker: the board it works on


def board() -> BoardSnapshot:
    """In a worker, the board its pool was started with."""
    return _snap


def workers(n_tasks: int) -> int:
    """How many processes to run ``n_tasks`` tasks in; 0 to run them here."""
    if os.environ.get(SERIAL_ENV) == "1" or not can_spawn():
        return 0
    n = min(spare(), n_tasks)
    return n if n >= 2 else 0


def fit(n: int, board_bytes: int) -> int:
    """Of ``n`` workers, how many memory holds with a board file of ``board_bytes``:
    at least 2, since the caller wants a pool."""
    free = free_memory()
    if free is None:
        return min(n, UNKNOWN_MEMORY_WORKERS)
    each = WORKER_BYTES + WORKER_BOARD_FILES * board_bytes
    return max(min(n, int(MEMORY_SHARE * free // each)), min(n, 2))


def _renamed() -> bool:
    """Whether this is KiPI.exe: a copy of Python under KiPI's name (#124)."""
    return Path(sys.executable or "").stem.lower() == "kipi"


def can_spawn() -> bool:
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


def _start(path: str, colors):
    global _snap
    with open(path, "rb") as f:
        _snap = pickle.load(f)
    if colors is not None:
        from kipi.core.viz import meshes
        meshes.set_layer_colors(colors)


class BoardPool:
    """Up to ``n`` worker processes holding ``snap``, as many as memory holds (:attr:`n`);
    with ``colors``, the 3D view's layer
    colors set in each. A context manager: leaving it stops the workers, dropping tasks
    not yet started."""

    def __init__(self, snap: BoardSnapshot, n: int, colors=None):
        # The workers read the board from a file. Passed to them as they start, it is
        # written down a pipe that blocks for good if a worker exits before reading it.
        # The copy leaves out the snapshot's indexes, which each worker builds for itself.
        fd, self._path = tempfile.mkstemp(prefix="kipi-board-", suffix=".pkl")
        try:
            with os.fdopen(fd, "wb") as f:
                pickle.dump(dataclasses.replace(snap), f, protocol=pickle.HIGHEST_PROTOCOL)
                self.n = fit(n, f.tell())
            # Spawned everywhere: forking a process with Qt's threads running is unsafe.
            if _renamed():
                _spawn_renamed()
            self._pool = cf.ProcessPoolExecutor(
                self.n, mp_context=multiprocessing.get_context("spawn"),
                initializer=_start, initargs=(self._path, colors))
        except BaseException:
            _remove(self._path)
            raise

    def submit(self, fn, *args) -> cf.Future:
        return self._pool.submit(fn, *args)

    def __enter__(self) -> "BoardPool":
        return self

    def __exit__(self, *exc):
        self._pool.shutdown(wait=False, cancel_futures=True)
        _remove_later(self._path, self._pool)


def finished(pending: Set[cf.Future], progress: ProgressReporter = NULL
             ) -> Iterator[cf.Future]:
    """Each of ``pending`` as it finishes, removing it; cancel is checked meanwhile."""
    while pending:
        progress.check_cancel()
        done, _ = cf.wait(pending, timeout=0.2, return_when=cf.FIRST_COMPLETED)
        for f in done:
            pending.discard(f)
            yield f


def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_later(path: str, pool):
    """Delete the board file once the workers have read it: on Windows not while one
    still has it open, so after they stop, from a thread rather than waiting here."""
    def remove():
        pool.shutdown(wait=True)
        _remove(path)
    threading.Thread(target=remove, name="kipi-pool-cleanup", daemon=True).start()
