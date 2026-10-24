"""Sparse direct solves of the symmetric positive definite systems KiPI builds.

Two backends, picked per machine:

* **PARDISO** (Intel MKL, multithreaded) through the optional ``pypardiso`` package. It is
  only installed where MKL has prebuilt packages (Windows and Linux on x86-64). Complex
  (AC) systems call the same MKL library directly, since pypardiso is real-only.
* **SuperLU** (SciPy, single-threaded), always available.

PARDISO is tried first. If it cannot be imported or loaded, KiPI uses SuperLU for the rest
of the session; if one factorization fails or comes back inaccurate, that system is
factorized again with SuperLU. Either way the solve goes on.
"""

from __future__ import annotations

import importlib
import logging
import os
import weakref
from typing import Callable, Optional

import numpy as np
import scipy.sparse as sp

log = logging.getLogger("kipi")

# A factorization is checked against one right-hand side; PARDISO results with a normwise
# backward error worse than this are discarded in favor of SuperLU. Not the relative
# residual |b - Ax| / |b|: on a big board's copper, with vias and series links far stiffer
# than the sheets, rounding alone leaves 1e-8 of it in a solution exact to machine
# precision (backward error ~1e-16), and SuperLU does no better (#163). A failed
# factorization is off by far more: zero or garbage gives about 1.
CHECK_TOL = 1e-12
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


def retry_pardiso() -> None:
    """Look for PARDISO again at the next factorization: it was just installed (#129)."""
    global _pardiso_problem, _native_problem
    if _pardiso is None:
        importlib.invalidate_caches()
        _pardiso_problem = None
        _native_problem = None


def backend() -> str:
    """Name of the backend new factorizations will use."""
    return "pardiso" if _pardiso_module() is not None else "superlu"


def backend_note() -> str:
    """One line for logs and bug reports: which backend, and why not PARDISO."""
    if _pardiso_module() is not None:
        return "PARDISO (Intel MKL, multithreaded)"
    return f"SuperLU (single-threaded; PARDISO unavailable: {_pardiso_problem})"


def backward_error(A: sp.spmatrix, x: np.ndarray, b: np.ndarray) -> float:
    """The normwise backward error of ``x`` as a solution of A x = b,
    |b - Ax| / (|A| |x| + |b|) in the infinity norm: the smallest relative change to A and b
    that makes ``x`` exact. A stable solve gives about the machine precision, however badly
    conditioned A is."""
    r = np.abs(b - A @ x).max()
    a = float(abs(A).sum(axis=1).max())
    return float(r / (a * np.abs(x).max() + np.abs(b).max()))


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
            err = backward_error(A, factor(b), b)
            if np.isfinite(err) and err <= CHECK_TOL:
                return factor
            reason = f"backward error {err:.1e}"
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


# ---------------------------------------------------------------------------
# Complex symmetric systems that share one sparsity pattern (an AC sweep)
# ---------------------------------------------------------------------------

# Bytes of one block of right-hand sides (and as much again for its solutions): the number
# of columns solved together is chosen to fit. Each solve reads the whole factorization,
# so fewer, wider blocks are faster.
RHS_BLOCK_BYTES = 512 * 2**20

_mkl = None                         # the MKL runtime pypardiso loads, once found
_native_problem: Optional[str] = None     # why native complex PARDISO is unusable here


def _mkl_library():
    """The MKL runtime library (ctypes), found the way pypardiso finds it, or None."""
    global _mkl, _native_problem
    if _mkl is not None or _native_problem is not None:
        return _mkl
    pypardiso = _pardiso_module()
    if pypardiso is None:
        _native_problem = "PARDISO unavailable"
        return None
    try:
        lib = pypardiso.PyPardisoSolver().libmkl
        lib["pardiso"]                       # the symbol must be there
    except Exception as e:
        _native_problem = f"{type(e).__name__}: {e}"
        return None
    _mkl = lib
    return lib


def _release_native(fn, pt):
    """Free PARDISO's memory for handle ``pt`` (phase -1)."""
    import ctypes
    if not pt.any():
        return
    one = ctypes.c_int32(1)
    try:
        fn(pt.ctypes.data, one, one, ctypes.c_int32(6), ctypes.c_int32(-1), one, None, None,
           None, None, one, np.zeros(64, np.int32).ctypes.data, ctypes.c_int32(0), None,
           None, ctypes.byref(ctypes.c_int32(0)))
    except Exception:                        # interpreter shutdown, MKL already unloaded
        pass
    pt[:] = 0


class _NativePardiso:
    """MKL PARDISO called directly for complex symmetric matrices (mtype 6), which
    pypardiso doesn't support. Against the real-equivalent system that is half the
    unknowns and no pivoting across its blocks: several times faster to factorize and
    solve. The ordering and symbolic factorization (phase 11) are kept while the sparsity
    pattern stays the same; each new matrix is only factorized numerically (phase 22)."""

    def __init__(self, lib):
        import ctypes
        self._ct = ctypes
        fn = lib["pardiso"]                  # a new function object, apart from pypardiso's
        p_int = ctypes.POINTER(ctypes.c_int32)
        vp = ctypes.c_void_p
        fn.argtypes = [vp, p_int, p_int, p_int, p_int, p_int, vp, vp, vp, vp, p_int, vp,
                       p_int, vp, vp, p_int]
        fn.restype = None
        self._fn = fn
        self.pt = np.zeros(64, dtype=np.intp)        # PARDISO's handle: 64 pointers
        ip = self.iparm = np.zeros(64, dtype=np.int32)
        ip[0] = 1                            # use these values, not PARDISO's defaults
        ip[1] = 2                            # METIS nested dissection ordering
        ip[7] = 0                            # refine iteratively when pivots are perturbed
        ip[9] = 8                            # perturb pivots below 1e-8
        ip[20] = 1                           # Bunch-Kaufman pivoting
        ip[34] = 1                           # zero-based indices
        self._pattern = None                 # (indptr, indices) analyzed, as int32
        self._finalizer = weakref.finalize(self, _release_native, fn, self.pt)

    def _call(self, phase: int, data, ia, ja, b=None, x=None):
        ct = self._ct
        nrhs = 1 if b is None or b.ndim == 1 else b.shape[1]
        err = ct.c_int32(0)
        one = ct.c_int32(1)
        self._fn(self.pt.ctypes.data, ct.byref(one), ct.byref(one), ct.byref(ct.c_int32(6)),
                 ct.byref(ct.c_int32(phase)), ct.byref(ct.c_int32(len(ia) - 1)),
                 data.ctypes.data, ia.ctypes.data, ja.ctypes.data, None,
                 ct.byref(ct.c_int32(nrhs)), self.iparm.ctypes.data, ct.byref(ct.c_int32(0)),
                 None if b is None else b.ctypes.data, None if x is None else x.ctypes.data,
                 ct.byref(err))
        if err.value:
            raise RuntimeError(f"PARDISO error {err.value} (phase {phase})")

    def factorize(self, upper: sp.csr_matrix) -> ComplexFactor:
        if upper.nnz >= 2**31 - 1:
            raise RuntimeError("too many non-zeros for 32-bit indices")
        data = np.ascontiguousarray(upper.data, dtype=complex)
        ia = np.ascontiguousarray(upper.indptr, dtype=np.int32)
        ja = np.ascontiguousarray(upper.indices, dtype=np.int32)
        pat = self._pattern
        if pat is None or not (np.array_equal(pat[0], ia) and np.array_equal(pat[1], ja)):
            _release_native(self._fn, self.pt)
            self._pattern = None
            self._call(11, data, ia, ja)
            self._pattern = (ia, ja)
        ia, ja = self._pattern
        self._call(22, data, ia, ja)
        n = len(ia) - 1

        def solve(b):
            b = np.asarray(b, dtype=complex)
            b2 = np.asfortranarray(b.reshape(n, -1))
            x = np.zeros_like(b2, order="F")
            self._call(33, data, ia, ja, b2, x)
            return x.reshape(b.shape)
        return ComplexFactor(solve, "pardiso")


def _symmetric_matvec(upper: sp.csr_matrix, x: np.ndarray) -> np.ndarray:
    """A @ x for the symmetric A whose upper triangle (with the diagonal) is ``upper``."""
    return upper @ x + upper.T @ x - upper.diagonal() * x


class ComplexSeries:
    """Factorizations of a series of complex symmetric matrices that share one sparsity
    pattern, such as an AC model at each frequency of a sweep.

    :meth:`factorize` takes the upper triangle (with the diagonal) of each matrix. Native
    complex PARDISO is used where it loads, with the pattern analyzed once. Each
    factorization is checked; one that fails or comes back inaccurate is redone with
    :func:`factorize_complex` (PARDISO on the real-equivalent system, then SuperLU). A
    factorization is only valid until the next call to :meth:`factorize`.
    """

    def __init__(self):
        self._native: Optional[_NativePardiso] = None
        self._off = False                    # native PARDISO can't be used here

    def _native_solver(self) -> Optional[_NativePardiso]:
        if self._native is None and not self._off:
            if os.environ.get(_ENV, "").strip().lower() == "superlu":
                self._off = True
                return None
            lib = _mkl_library()
            try:
                self._native = _NativePardiso(lib) if lib is not None else None
            except Exception as e:           # ctypes could not bind the symbol
                log.warning("Native complex PARDISO unavailable: %s", e)
            self._off = self._native is None
        return self._native

    def factorize(self, upper: sp.spmatrix,
                  warn: Optional[Callable[[str], None]] = None) -> ComplexFactor:
        upper = sp.csr_matrix(upper)
        upper.sort_indices()
        n = upper.shape[0]
        native = self._native_solver() if n > 0 else None
        if native is not None:
            try:
                factor = native.factorize(upper)
                b = np.ones(n, dtype=complex)
                x = factor(b)
                rel = float(np.linalg.norm(b - _symmetric_matvec(upper, x))
                            / np.linalg.norm(b))
                if np.isfinite(rel) and rel <= CHECK_TOL_COMPLEX:
                    return factor
                reason = f"relative residual {rel:.1e}"
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
            msg = (f"Complex PARDISO factorization failed ({reason}); "
                   "solving this system another way")
            log.warning(msg)
            if warn is not None:
                warn(msg)
        full = (upper + upper.T - sp.diags_array(upper.diagonal())).tocsr()
        factor = factorize_complex(full, warn)
        if factor.name == "pardiso":
            factor.name = "pardiso (real form)"
        return factor

    def close(self):
        """Release PARDISO's memory now, rather than when this is garbage collected."""
        if self._native is not None:
            self._native._finalizer()
            self._native = None


def rhs_block(n: int, max_cols: int = 64) -> int:
    """How many right-hand sides of ``n`` complex unknowns to solve together."""
    return int(np.clip(RHS_BLOCK_BYTES // (16 * max(n, 1)), 1, max_cols))
