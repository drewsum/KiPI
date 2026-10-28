"""Solving a symmetric positive definite sparse system for many right-hand sides.

Shared by every analysis that ends in an SPD matrix (DC conduction, the board heat
model). Small systems are factorized directly (see :mod:`kipi.core.solver.direct`);
large ones use conjugate gradients preconditioned by an algebraic multigrid hierarchy.
"""

from __future__ import annotations

import math
import time
from typing import List, Optional

import numpy as np
import scipy.sparse as sp

from kipi.core.memory import free_memory
from kipi.core.progress import ProgressReporter
from kipi.core.solver import direct

# Largest systems factorized directly, by backend (see kipi.core.solver.direct); AMG-CG above.
# Measured on board meshes: SuperLU beats AMG-CG up to ~250k unknowns (0.7 s vs 2.4 s at
# 160k; ~1 GB of factors at 420k), and multithreaded PARDISO beats it at every size
# tried (1.2 s vs 10 s at 640k).
DIRECT_SOLVE_MAX_UNKNOWNS = 200_000
DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO = 1_000_000
# Above that, PARDISO is still used while its factorization fits in this share of the
# memory free, at this many bytes per unknown: on board meshes it takes about 1.3 kB (6.5 GB
# at 4.9M unknowns, where it solved in 11 s what AMG-CG took 104 s for, #214), counted
# with a margin since the fill grows a little faster than the unknowns.
PARDISO_BYTES_PER_UNKNOWN = 2048
PARDISO_MEMORY_SHARE = 0.5
# CG on big boards can reach the floor that round-off allows just above the target
# tolerance; past that point it does not stall quietly but diverges. Stop once it has
# gone this many iterations without a new best residual, and keep the best iterate.
CG_STALL_ITERATIONS = 40
CG_ACCEPT_FACTOR = 100     # accept a stopped CG result within this factor of tol
CG_WARM_ACCEPT_FACTOR = 10  # skip CG when a warm start is already within this factor
CG_REUSED_MAX_ITERATIONS = 150  # past this, a reused preconditioner is set up afresh
# The AMG hierarchy's prolongation smoothing: energy minimization took 8-17% less time to
# the same answer than pyamg's default Jacobi smoothing on every system above 200k
# unknowns tried (Jetson AGX Thor rails up to 4.9M unknowns, the DSP board's rails and
# heat model, #218). Should CG not converge with it, the default is set up once before
# anything else is tried.
AMG_SMOOTH = "energy"


class _Stalled(Exception):
    """Raised from the CG callback when the residual stops improving."""


def direct_solve_max_unknowns() -> int:
    """Largest system solved directly with the backend this machine has: with PARDISO, as
    many as its factorization fits in memory for."""
    if direct.backend() != "pardiso":
        return DIRECT_SOLVE_MAX_UNKNOWNS
    free = free_memory()
    fits = 0 if free is None else int(PARDISO_MEMORY_SHARE * free // PARDISO_BYTES_PER_UNKNOWN)
    return max(DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO, fits)


class SpdSolver:
    """Solver for the fixed reduced matrix, reused for every right-hand side.

    Small systems are factorized once; large ones keep their AMG hierarchy and warm start
    CG from the previous solution (power-tree iterations change only the right-hand side,
    and only slightly).

    ``previous`` is the solver of an earlier matrix with the same unknowns and nearly the
    same values (the electro-thermal loop changes only copper resistivity). If it used AMG,
    its hierarchy preconditions CG on the new matrix, which skips the AMG setup; if that
    does not converge, the new matrix gets a setup of its own. Direct factorizations are
    not reused: a refactorization costs less than running CG for every right-hand side."""

    def __init__(self, A: sp.csr_matrix, settings, progress: ProgressReporter, stats: dict,
                 previous: Optional["SpdSolver"] = None):
        self.A = A
        self.s = settings
        self.stats = stats
        self.x_prev = None
        self.lu = None            # direct factorization of A itself
        self.M = None             # CG preconditioner: this matrix's AMG cycle, or a previous one's
        self.reused = False
        self.smooth = None        # the AMG hierarchy's prolongation smoother, once set up
        n = A.shape[0]
        if (previous is not None and previous.M is not None
                and previous.A.shape == A.shape and previous.A.nnz == A.nnz):
            self.M = previous.M
            self.x_prev = previous.x_prev
            self.reused = True
            stats["method"] = "amg-cg (reused hierarchy)"
            progress.info(f"Reusing the previous AMG hierarchy as preconditioner "
                          f"({n} unknowns)")
        else:
            self._setup(progress)

    def _setup(self, progress: ProgressReporter, smooth: str = AMG_SMOOTH):
        """Factorization (small systems) or AMG hierarchy (large ones) for A itself;
        ``smooth`` is the hierarchy's prolongation smoother."""
        A, n = self.A, self.A.shape[0]
        self.reused = False
        self.M = None
        if n <= direct_solve_max_unknowns():
            progress.info(f"Direct solve ({n} unknowns, {direct.backend()})")
            progress.fraction(None)
            if n == 0:
                self.lu = None
                self.stats.update(method="direct", iterations=0)
                return
            try:
                # Past PARDISO's usual limit SuperLU would take far too long: if PARDISO
                # fails there, AMG-CG instead (#214).
                self.lu = direct.factorize(A, progress.warn,
                                           superlu=n <= DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO)
            except direct.DirectError as e:
                progress.warn(f"{e}; solving this system iteratively (AMG-CG)")
            else:
                self.stats.update(method=f"direct ({self.lu.name})", iterations=0)
                return
        import pyamg
        t = time.perf_counter()
        progress.info(f"AMG setup ({n} unknowns, {A.nnz} non-zeros)...")
        kw = {} if smooth == "jacobi" else {"smooth": smooth}      # jacobi: pyamg's default
        ml = pyamg.smoothed_aggregation_solver(A, symmetry="hermitian", max_coarse=500, **kw)
        self.smooth = smooth
        progress.info(f"AMG hierarchy: {len(ml.levels)} levels, operator complexity "
                      f"{ml.operator_complexity():.2f} ({time.perf_counter() - t:.2f} s)")
        self.M = ml.aspreconditioner()
        self.stats["method"] = "amg-cg"

    def solve(self, rhs: np.ndarray, progress: ProgressReporter) -> np.ndarray:
        if self.A.shape[0] == 0:
            return np.zeros(0)
        if self.lu is not None:
            return np.asarray(self.lu(rhs))
        x, rel = self._cg(rhs, progress)
        if rel > self.s.tol * CG_ACCEPT_FACTOR and self.reused:
            progress.info(f"The reused AMG hierarchy did not converge (relative residual "
                          f"{rel:.1e}); setting up this matrix afresh")
            self._setup(progress)
            if self.lu is not None:
                self.x_prev = np.asarray(self.lu(rhs))
                return self.x_prev
            x, rel = self._cg(rhs, progress)
        if (rel > self.s.tol * CG_ACCEPT_FACTOR and self.M is not None and not self.reused
                and self.smooth not in (None, "jacobi")):
            progress.info(f"AMG-CG did not converge (relative residual {rel:.1e}); setting "
                          "the hierarchy up again with Jacobi smoothing")
            self._setup(progress, smooth="jacobi")
            x, rel = self._cg(rhs, progress)
        if rel > self.s.tol * CG_ACCEPT_FACTOR:
            progress.warn("Iterative solve did not converge; switching this system to a "
                          "direct solve")
            progress.fraction(None)
            # Kept for this system's later right-hand sides (power-tree passes).
            self.lu = direct.factorize(self.A, progress.warn)
            self.M = None
            x = self.lu(rhs)
            self.stats["method"] = f"direct ({self.lu.name}, fallback)"
        self.x_prev = np.asarray(x)
        return self.x_prev

    def _cg(self, rhs: np.ndarray, progress: ProgressReporter):
        """Preconditioned CG from the previous solution: (x, relative residual)."""
        from pyamg.krylov import cg
        s = self.s
        # The target is ||b - A x|| <= tol * ||b||, which is also what pyamg's CG tests
        # (its default 'rr' criterion), so tol is passed straight through.
        norm_b = float(np.linalg.norm(rhs))
        if norm_b == 0:
            return np.zeros_like(rhs), 0.0
        x0 = self.x_prev if self.x_prev is not None and len(self.x_prev) == len(rhs) else None
        if x0 is not None:
            # Re-solves (power-tree passes, electro-thermal iterations) often start right
            # at the round-off floor, a little above tol, where CG cannot improve and
            # would only spend CG_STALL_ITERATIONS finding that out.
            r0 = float(np.linalg.norm(rhs - self.A @ x0))
            if r0 <= s.tol * CG_WARM_ACCEPT_FACTOR * norm_b:
                self.stats["iterations"] = 0
                self.stats["relative_residual"] = r0 / norm_b
                progress.info(f"Warm start already converged (relative residual {r0 / norm_b:.2e})")
                return x0, r0 / norm_b

        residuals: List[float] = []
        state = {"last": 0.0}
        best = {"res": math.inf, "x": None, "it": 0}
        log_target = math.log10(max(s.tol, 1e-16))

        def cb(x):
            progress.check_cancel()
            if not residuals:
                return
            it = len(residuals) - 1
            if residuals[-1] < best["res"]:
                best.update(res=residuals[-1], x=x.copy(), it=it)
            elif it - best["it"] >= CG_STALL_ITERATIONS:
                raise _Stalled
            now = time.perf_counter()
            if now - state["last"] > 0.25:
                state["last"] = now
                r = residuals[-1] / norm_b
                progress.info(f"  CG iteration {it}: relative residual {r:.2e}")
                progress.fraction(min(1.0, math.log10(max(r, 1e-300)) / log_target))

        # A reused preconditioner that needs many iterations is a worse deal than a setup
        # of this matrix's own.
        maxiter = min(s.max_iter, CG_REUSED_MAX_ITERATIONS) if self.reused else s.max_iter
        stalled = False
        try:
            x, _ = cg(self.A, rhs, x0=x0, tol=s.tol, maxiter=maxiter, M=self.M,
                      callback=cb, residuals=residuals)
        except _Stalled:
            stalled = True
            x = best["x"]
        else:
            if best["x"] is not None and residuals[-1] > best["res"]:
                x = best["x"]          # hit maxiter after wandering off its best iterate
        rel = float(np.linalg.norm(rhs - self.A @ x)) / norm_b
        self.stats["iterations"] = len(residuals) - 1
        self.stats["relative_residual"] = rel
        if stalled:
            progress.info(f"CG stopped improving after {best['it']} iterations at relative "
                          f"residual {rel:.2e} (target {s.tol:.0e}; round-off limits this "
                          "system)")
        else:
            progress.info(f"CG converged in {len(residuals) - 1} iterations, "
                          f"relative residual {rel:.2e}")
        return x, rel
