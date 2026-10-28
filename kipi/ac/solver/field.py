"""The field solves of an AC sweep: the board's port impedances at each frequency.

With PARDISO each factorization already uses every core, so the frequencies are solved
one at a time. Without it, each is a SuperLU factorization on one core, which leaves the
GIL while it works: several frequencies are solved at once, in threads, each with its own
factorization, as many as memory allows (#213).
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import time

import numpy as np

from kipi.core.memory import free_memory
from kipi.core.progress import Canceled, ProgressReporter
from kipi.core.solver import direct

#: Most frequencies solved at once.
MAX_THREADS = 8
#: Of the memory free, the share the threads' factorizations may take.
MEMORY_SHARE = 0.5
#: Threads where the free memory can't be read.
UNKNOWN_MEMORY_THREADS = 2


def fmt_hz(f: float) -> str:
    from kipi.ac.solver.sweep import fmt_hz as fmt
    return fmt(f)


def field_solves(system, f_field: np.ndarray, progress: ProgressReporter) -> np.ndarray:
    """``system``'s port impedance matrices at each of ``f_field`` (Hz), (F, P, P). The
    solver's memory is released once done."""
    n = len(f_field)
    zb = np.zeros((n, len(system.ports), len(system.ports)), complex)
    t1 = time.perf_counter()
    try:
        progress.check_cancel()
        progress.info(f"  [1/{n}] solving {fmt_hz(f_field[0])}")
        zb[0], factor = system.port_impedance(2 * np.pi * f_field[0], progress,
                                              with_factor=True)
        progress.fraction(1 / n)
        progress.info(f"  first frequency took {time.perf_counter() - t1:.2f} s "
                      f"({system.stats.method})")
        left = list(range(1, n))
        k = threads(system, factor, len(left))
        del factor                       # its memory is one thread's share
        if k:
            left = _in_threads(system, f_field, zb, left, k, progress)
        for i in left:
            progress.check_cancel()
            progress.info(f"  [{i + 1}/{n}] solving {fmt_hz(f_field[i])}")
            zb[i] = system.port_impedance(2 * np.pi * f_field[i], progress)
            progress.fraction((i + 1) / n)
    finally:
        system.close()
    return zb


def threads(system, factor, n_left: int) -> int:
    """How many frequencies to solve at once after the first, whose factorization was
    ``factor``; 0 to solve them one at a time."""
    if factor.name != "superlu" or n_left < 2:
        return 0
    # One thread holds a factorization and a block of right-hand sides with its solutions.
    nf = system.n_free
    each = factor.nbytes + 2 * 16 * nf * direct.rhs_block(nf) + 64 * system.stats.nnz
    free = free_memory()
    n = min((os.cpu_count() or 1) - 1, MAX_THREADS, n_left)
    if free is None:
        n = min(n, UNKNOWN_MEMORY_THREADS)
    else:
        n = min(n, int(MEMORY_SHARE * free // max(each, 1)))
    return n if n >= 2 else 0


def _in_threads(system, f_field, zb, left, k: int, progress) -> list:
    """Solve ``left`` (indices of ``f_field``) into ``zb`` in ``k`` threads; those left
    unsolved if a thread fails, to be solved one at a time."""
    n = len(f_field)
    progress.info(f"Solving {len(left)} frequencies {k} at a time (SuperLU, one core each)")
    pool = cf.ThreadPoolExecutor(k, thread_name_prefix="kipi-field")
    pending = {pool.submit(_solve, system, 2 * np.pi * f_field[i], progress,
                           f"  [{i + 1}/{n}] solving {fmt_hz(f_field[i])}"): i for i in left}
    done = 0
    try:
        while pending:
            progress.check_cancel()
            finished, _ = cf.wait(pending, timeout=0.2, return_when=cf.FIRST_COMPLETED)
            for f in finished:
                i = pending.pop(f)
                try:
                    zb[i] = f.result()
                except Canceled:
                    raise
                except Exception as e:  # noqa: BLE001 - MemoryError above all
                    for g in pending:
                        g.cancel()
                    rest = sorted([i, *pending.values()])
                    progress.warn(f"Could not solve frequencies {k} at a time "
                                  f"({type(e).__name__}: {e}); solving the {len(rest)} "
                                  "left one at a time")
                    pool.shutdown(wait=True)
                    return rest
                done += 1
                progress.fraction((done + 1) / n)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return []


def _solve(system, omega: float, progress, label: str) -> np.ndarray:
    progress.info(label)                 # as the thread starts on it
    series = direct.ComplexSeries(native=False)
    try:
        return system.port_impedance(omega, progress, series=series)
    finally:
        series.close()
