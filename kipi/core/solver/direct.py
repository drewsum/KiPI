"""Sparse direct solves of the symmetric positive definite systems KiPI builds.

Two backends, picked per machine:

* **PARDISO** (Intel MKL, multithreaded) through the optional ``pypardiso`` package. It is
  only installed where MKL has prebuilt packages (Windows and Linux on x86-64).
* **SuperLU** (SciPy, single-threaded), always available.

PARDISO is tried first. If it cannot be imported or loaded, KiPI uses SuperLU for the rest
of the session; if one factorization fails or comes back inaccurate, that system is
factorized again with SuperLU. Either way the solve goes on.
"""

from __future__ import annotations

import logging
import os
import weakref
from typing import Callable, Optional

import numpy as np
import scipy.sparse as sp

log = logging.getLogger("kipi")

# A factorization is checked against one right-hand side; PARDISO results worse than this
# relative residual are discarded in favor of SuperLU.
CHECK_TOL = 1e-8
# The same check for complex (AC) systems. PARDISO solves them through an indefinite real
# system with pivot perturbation, which lands around 1e-8; impedances need far less.
CHECK_TOL_COMPLEX = 1e-6
# Set KIPI_DIRECT_SOLVER=superlu to keep the multithreaded backend out (e.g. to compare).
_ENV = "KIPI_DIRECT_SOLVER"

_pardiso = None                     # the pypardiso module, once loaded and working
_pardiso_problem: Optional[str] = None    # why PARDISO is unusable here, once known


def _pardiso_module():
    """The pypardiso module, or None (with the reason kept) when it cannot be used."""
    global _pardiso, _pardiso_problem
    if _pardiso is not None or _pardiso_problem is not None:
        return _pardiso
    if os.environ.get(_ENV, "").strip().lower() == "superlu":
        _pardiso_problem = f"disabled by {_ENV}"
        return None
    try:
        import pypardiso
        probe = pypardiso.PyPardisoSolver()  # loads mkl_rt; fails if MKL is missing
    except Exception as e:                   # ImportError, OSError from ctypes, ...
        _pardiso_problem = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return None
    # Each new solver looks for mkl_rt again, and when the library is not on the system
    # search path (pip's MKL on Windows) that is a recursive walk of the whole Python
    # prefix, about a second per factorization. Point later solvers straight at it.
    path = getattr(getattr(probe, "libmkl", None), "_name", None)
    if path and os.path.isfile(path):
        os.environ.setdefault("PYPARDISO_MKL_RT", path)
    _pardiso = pypardiso
    return _pardiso


def backend() -> str:
    """Name of the backend new factorizations will use."""
    return "pardiso" if _pardiso_module() is not None else "superlu"


def backend_note() -> str:
    """One line for logs and bug reports: which backend, and why not PARDISO."""
    if _pardiso_module() is not None:
        return "PARDISO (Intel MKL, multithreaded)"
    return f"SuperLU (single-threaded; PARDISO unavailable: {_pardiso_problem})"


class DirectFactor:
    """A factorization of A; call it with a right-hand side to get the solution."""

    def __init__(self, solve: Callable[[np.ndarray], np.ndarray], name: str):
        self._solve = solve
        self.name = name

    def __call__(self, b: np.ndarray) -> np.ndarray:
        return np.asarray(self._solve(b), dtype=float).reshape(np.shape(b))


def _superlu(A: sp.spmatrix) -> DirectFactor:
    from scipy.sparse.linalg import splu
    # SuperLU's default COLAMD ordering: on these meshes its MMD_AT_PLUS_A ordering takes
    # minutes where COLAMD takes a second.
    lu = splu(sp.csc_matrix(A), permc_spec="COLAMD")
    return DirectFactor(lu.solve, "superlu")


def _free_pardiso(solver):
    try:
        solver.free_memory(everything=False)
    except Exception:                        # interpreter shutdown, MKL already unloaded
        pass


def _pardiso_factor(pypardiso, A: sp.spmatrix) -> DirectFactor:
    solver = pypardiso.PyPardisoSolver(mtype=2)      # real symmetric positive definite
    upper = sp.triu(A, format="csr")                 # mtype 2 reads the upper triangle
    upper.sort_indices()
    solver.factorize(upper)
    factor = DirectFactor(lambda b: solver.solve(upper, b), "pardiso")
    # MKL keeps the factors in its own memory until told to free them.
    weakref.finalize(factor, _free_pardiso, solver)
    return factor


def factorize(A: sp.spmatrix, warn: Optional[Callable[[str], None]] = None) -> DirectFactor:
    """Factorize a symmetric positive definite A with the best backend that works here."""
    pypardiso = _pardiso_module()
    if pypardiso is not None and A.shape[0] > 0:
        try:
            factor = _pardiso_factor(pypardiso, A)
            b = np.ones(A.shape[0])
            x = factor(b)
            rel = float(np.linalg.norm(b - A @ x) / np.linalg.norm(b))
            if np.isfinite(rel) and rel <= CHECK_TOL:
                return factor
            reason = f"relative residual {rel:.1e}"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        msg = f"PARDISO factorization failed ({reason}); using SuperLU for this system"
        log.warning(msg)
        if warn is not None:
            warn(msg)
    return _superlu(A)


# ---------------------------------------------------------------------------
# Complex symmetric systems (AC analysis)
# ---------------------------------------------------------------------------

class ComplexFactor:
    """A factorization of a complex symmetric A; call it with (n,) or (n, k) right-hand
    sides to get the solutions."""

    def __init__(self, solve: Callable[[np.ndarray], np.ndarray], name: str):
        self._solve = solve
        self.name = name

    def __call__(self, b: np.ndarray) -> np.ndarray:
        return self._solve(np.asarray(b, dtype=complex))


def _superlu_complex(A: sp.spmatrix) -> ComplexFactor:
    from scipy.sparse.linalg import splu
    lu = splu(sp.csc_matrix(A, dtype=complex), permc_spec="COLAMD")
    return ComplexFactor(lu.solve, "superlu")


def _pardiso_complex(pypardiso, A: sp.spmatrix) -> ComplexFactor:
    """PARDISO through pypardiso, which takes real matrices only: solve the equivalent
    real system [[Ar, Ai], [Ai, -Ar]] [xr; -xi] = [br; bi], which is symmetric (indefinite)
    when A is complex symmetric."""
    A = sp.csr_matrix(A)
    n = A.shape[0]
    Ar, Ai = A.real, A.imag
    big = sp.bmat([[Ar, Ai], [Ai, -Ar]], format="csr")
    solver = pypardiso.PyPardisoSolver(mtype=-2)     # real symmetric indefinite
    # Symmetric scaling and weighted matching (iparm 11, 13), which MKL recommends for
    # highly indefinite systems, plus more iterative refinement (iparm 8): the AC system
    # is badly scaled at low frequency, where the rail is tied to ground only by the
    # plane capacitance.
    solver.set_iparm(11, 1)
    solver.set_iparm(13, 1)
    solver.set_iparm(8, 20)
    upper = sp.triu(big, format="csr")
    upper.sort_indices()
    solver.factorize(upper)

    def solve(b):
        rhs = np.concatenate([b.real, b.imag], axis=0)
        x = np.asarray(solver.solve(upper, np.ascontiguousarray(rhs)))
        x = x.reshape(rhs.shape)
        return x[:n] - 1j * x[n:]
    factor = ComplexFactor(solve, "pardiso")
    weakref.finalize(factor, _free_pardiso, solver)
    return factor


def factorize_complex(A: sp.spmatrix,
                      warn: Optional[Callable[[str], None]] = None) -> ComplexFactor:
    """Factorize a complex symmetric A with the best backend that works here (PARDISO on
    the real-equivalent system, checked; otherwise SuperLU on A itself)."""
    pypardiso = _pardiso_module()
    if pypardiso is not None and A.shape[0] > 0:
        try:
            factor = _pardiso_complex(pypardiso, A)
            b = np.ones(A.shape[0], dtype=complex)
            x = factor(b)
            rel = float(np.linalg.norm(b - A @ x) / np.linalg.norm(b))
            if np.isfinite(rel) and rel <= CHECK_TOL_COMPLEX:
                return factor
            reason = f"relative residual {rel:.1e}"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        msg = f"PARDISO factorization failed ({reason}); using SuperLU for this system"
        log.warning(msg)
        if warn is not None:
            warn(msg)
    return _superlu_complex(A)
