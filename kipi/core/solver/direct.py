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

from kipi.core.memory import free_memory

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


class DirectError(RuntimeError):
    """PARDISO could not factorize a system that SuperLU is not to be tried on."""


def factorize(A: sp.spmatrix, warn: Optional[Callable[[str], None]] = None,
              superlu: bool = True) -> DirectFactor:
    """Factorize a symmetric positive definite A with the best backend that works here.
    With ``superlu`` False (a system too big for it), a failed PARDISO factorization raises
    :class:`DirectError` instead of falling back to SuperLU."""
    pypardiso = _pardiso_module()
    if pypardiso is not None and A.shape[0] > 0:
        try:
            factor = _pardiso_factor(pypardiso, A)
            b = np.ones(A.shape[0])
            err = backward_error(A, factor(b), b)
            if np.isfinite(err) and err <= CHECK_TOL:
                return factor
            reason = f"backward error {err:.1e}"
            del factor                       # its memory, before anything else is tried
        except Exception as e:               # MemoryError among them
            reason = f"{type(e).__name__}: {e}"
        if not superlu:
            raise DirectError(f"PARDISO factorization failed ({reason})")
        msg = f"PARDISO factorization failed ({reason}); using SuperLU for this system"
        log.warning(msg)
        if warn is not None:
            warn(msg)
    elif not superlu:
        raise DirectError(f"PARDISO is not available ({_pardiso_problem})")
    return _superlu(A)


# ---------------------------------------------------------------------------
# Complex symmetric systems (AC analysis)
# ---------------------------------------------------------------------------

class ComplexFactor:
    """A factorization of a complex symmetric A; call it with (n,) or (n, k) right-hand
    sides to get the solutions."""

    def __init__(self, solve: Callable[[np.ndarray], np.ndarray], name: str,
                 nbytes: int = 0, schur: Optional[np.ndarray] = None):
        self._solve = solve
        self.name = name
        self.nbytes = nbytes                 # the factors' memory, where known
        # With Schur rows asked for (see ComplexSeries.factorize): A's Schur complement on
        # them, in their order. A right-hand side that is zero off those rows has its
        # solution there from this alone, x_s = S^-1 b_s, without a solve (#225).
        self.schur = schur

    def __call__(self, b: np.ndarray) -> np.ndarray:
        return self._solve(np.asarray(b, dtype=complex))


def _superlu_complex(A: sp.spmatrix) -> ComplexFactor:
    from scipy.sparse.linalg import splu
    lu = splu(sp.csc_matrix(A, dtype=complex), permc_spec="COLAMD")
    # L and U: a complex value and a row index each, and the permutations' few.
    return ComplexFactor(lu.solve, "superlu", nbytes=24 * int(lu.nnz))


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
# Where the memory free can be read, one block and its solutions may take this share of it
# instead, if that is more (#215). On a 4.6M-unknown AC model, 45 columns at once in place
# of 7 took a frequency from 29.8 s to 18.0 s.
RHS_MEMORY_SHARE = 0.25

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


def _release_native(fn, pt, iparm=None):
    """Free PARDISO's memory for handle ``pt`` (phase -1), with the ``iparm`` it was
    factorized with: MKL's two-level factorization faults if released with others."""
    import ctypes
    if not pt.any():
        return
    one = ctypes.c_int32(1)
    try:
        fn(pt.ctypes.data, one, one, ctypes.c_int32(6), ctypes.c_int32(-1), one, None, None,
           None, None, one, (np.zeros(64, np.int32) if iparm is None else iparm).ctypes.data,
           ctypes.c_int32(0), None,
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
        # Parallel METIS nested dissection, and the two-level factorization that uses
        # its ordering in parallel: on a 4.6M-unknown AC model with its Schur complement,
        # 12 s for the analysis and factorization rather than 18.6 s (#225).
        ip[1] = 3
        ip[23] = 1
        ip[7] = 0                            # refine iteratively when pivots are perturbed
        ip[9] = 8                            # perturb pivots below 1e-8
        ip[20] = 1                           # Bunch-Kaufman pivoting
        ip[34] = 1                           # zero-based indices
        self._pattern = None                 # (indptr, indices, Schur rows, perm) analyzed
        self._finalizer = weakref.finalize(self, _release_native, fn, self.pt, ip)

    def _call(self, phase: int, data, ia, ja, b=None, x=None, perm=None):
        ct = self._ct
        nrhs = 1 if b is None or b.ndim == 1 else b.shape[1]
        err = ct.c_int32(0)
        one = ct.c_int32(1)
        self._fn(self.pt.ctypes.data, ct.byref(one), ct.byref(one), ct.byref(ct.c_int32(6)),
                 ct.byref(ct.c_int32(phase)), ct.byref(ct.c_int32(len(ia) - 1)),
                 data.ctypes.data, ia.ctypes.data, ja.ctypes.data,
                 None if perm is None else perm.ctypes.data,
                 ct.byref(ct.c_int32(nrhs)), self.iparm.ctypes.data, ct.byref(ct.c_int32(0)),
                 None if b is None else b.ctypes.data, None if x is None else x.ctypes.data,
                 ct.byref(err))
        if err.value:
            raise RuntimeError(f"PARDISO error {err.value} (phase {phase})")

    def factorize(self, upper: sp.csr_matrix,
                  schur: Optional[np.ndarray] = None) -> ComplexFactor:
        """``schur``: rows (sorted) to compute the Schur complement on as well. The rest of
        the matrix is then factorized with those rows last, and a solve goes through the
        partial phases with the Schur complement solved between them."""
        if upper.nnz >= 2**31 - 1:
            raise RuntimeError("too many non-zeros for 32-bit indices")
        data = np.ascontiguousarray(upper.data, dtype=complex)
        ia = np.ascontiguousarray(upper.indptr, dtype=np.int32)
        ja = np.ascontiguousarray(upper.indices, dtype=np.int32)
        n = len(ia) - 1
        rows = None if schur is None else np.asarray(schur, dtype=np.int64)
        pat = self._pattern
        if pat is None or not (np.array_equal(pat[0], ia) and np.array_equal(pat[1], ja)
                               and _same_rows(pat[2], rows)):
            _release_native(self._fn, self.pt, self.iparm)
            self._pattern = None
            perm = None
            if rows is not None:
                perm = np.zeros(n, dtype=np.int32)
                perm[rows] = 1
            self.iparm[35] = 0 if rows is None else 2     # Schur complement, kept factor
            try:
                self._call(11, data, ia, ja, perm=perm)
            except RuntimeError:
                if self.iparm[1] == 2 and self.iparm[23] == 0:
                    raise
                # An MKL without them: its sequential ordering and factorization.
                log.warning("PARDISO refused parallel ordering; using its sequential one")
                _release_native(self._fn, self.pt, self.iparm)
                self.iparm[1], self.iparm[23] = 2, 0
                self._call(11, data, ia, ja, perm=perm)
            self._pattern = (ia, ja, rows, perm)
        ia, ja, rows, perm = self._pattern
        if rows is None:
            self._call(22, data, ia, ja)

            def solve(b):
                b = np.asarray(b, dtype=complex)
                b2 = np.asfortranarray(b.reshape(n, -1))
                x = np.zeros_like(b2, order="F")
                self._call(33, data, ia, ja, b2, x)
                return x.reshape(b.shape)
            return ComplexFactor(solve, "pardiso")

        import scipy.linalg as la
        m = len(rows)
        out = np.zeros(m * m, dtype=complex)
        self._call(22, data, ia, ja, x=out, perm=perm)
        S = out.reshape(m, m)                # symmetric: row or column major alike
        if not np.all(np.isfinite(S)):
            raise RuntimeError("the Schur complement is not finite")
        lu = la.lu_factor(S, check_finite=False)

        def solve_schur(b):
            # Forward and diagonal substitution leave the Schur rows' reduced right-hand
            # side, solved here with S, before the backward substitution.
            b = np.asarray(b, dtype=complex)
            b2 = np.asfortranarray(b.reshape(n, -1))
            y = np.zeros_like(b2, order="F")
            self._call(331, data, ia, ja, b2, y, perm=perm)
            z = np.zeros_like(b2, order="F")
            self._call(332, data, ia, ja, y, z, perm=perm)
            z[rows] = la.lu_solve(lu, y[rows], check_finite=False)
            x = np.zeros_like(b2, order="F")
            self._call(333, data, ia, ja, z, x, perm=perm)
            return x.reshape(b.shape)
        return ComplexFactor(solve_schur, "pardiso", nbytes=2 * S.nbytes, schur=S)


def _same_rows(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return np.array_equal(a, b)


def _symmetric_matvec(upper: sp.csr_matrix, x: np.ndarray) -> np.ndarray:
    """A @ x for the symmetric A whose upper triangle (with the diagonal) is ``upper``;
    ``x`` one vector or a block of them."""
    d = upper.diagonal()
    return upper @ x + upper.T @ x - (d[:, None] if x.ndim == 2 else d) * x


def _check_block(check: Optional[np.ndarray], n: int) -> np.ndarray:
    """The right-hand sides to check a factorization with: ``check`` if it has a column
    that isn't zero, else ones."""
    if check is not None and check.shape[0] == n and np.any(check):
        return check
    return np.ones(n, dtype=complex)


def _worst_residual(upper: sp.csr_matrix, x: np.ndarray, b: np.ndarray) -> float:
    """The relative residual |b - Ax| / |b| of a solve. A block is checked through one
    random combination of its columns (each scaled to unit norm), which is solved by the
    same combination of the solutions: one product with A rather than one per column,
    which on a large model costs as much as the solve."""
    if b.ndim == 2:
        nb = np.linalg.norm(b, axis=0)
        some = nb > 0
        rng = np.random.default_rng(0)
        v = np.zeros(b.shape[1], dtype=complex)
        v[some] = (rng.standard_normal(int(some.sum()))
                   + 1j * rng.standard_normal(int(some.sum()))) / nb[some]
        b, x = b @ v, x @ v
    return float(np.linalg.norm(b - _symmetric_matvec(upper, x)) / np.linalg.norm(b))


class ComplexSeries:
    """Factorizations of a series of complex symmetric matrices that share one sparsity
    pattern, such as an AC model at each frequency of a sweep.

    :meth:`factorize` takes the upper triangle (with the diagonal) of each matrix. Native
    complex PARDISO is used where it loads, with the pattern analyzed once. Each
    factorization is checked; one that fails or comes back inaccurate is redone with
    :func:`factorize_complex` (PARDISO on the real-equivalent system, then SuperLU). A
    factorization is only valid until the next call to :meth:`factorize`.
    """

    def __init__(self, native: bool = True):
        self._native: Optional[_NativePardiso] = None
        self._off = not native               # native PARDISO can't be used here
        self._schur_off = False              # a Schur complement failed here once
        self.checked = None                  # (b, x): the last factor's check solve

    @property
    def schur_ok(self) -> bool:
        """Whether :meth:`factorize` can be asked for a Schur complement here."""
        return not self._schur_off and self._native_solver() is not None

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
                  warn: Optional[Callable[[str], None]] = None,
                  check: Optional[Callable[[], np.ndarray]] = None,
                  schur: Optional[np.ndarray] = None) -> ComplexFactor:
        """``check`` returns, once the matrix is factorized, the first block of right-hand
        sides the caller will solve. That block checks the factorization in place of a
        solve of its own (#215), and is left in :attr:`checked` as ``(b, x)`` with its
        solutions.

        ``schur``: sorted rows to also compute the Schur complement on (the factor's
        :attr:`ComplexFactor.schur`), where :attr:`schur_ok`. If that fails, the matrix is
        factorized without one, and so is every later one: the caller checks which it got."""
        self.checked = None
        upper = sp.csr_matrix(upper)
        upper.sort_indices()
        n = upper.shape[0]
        native = self._native_solver() if n > 0 else None
        if native is not None and schur is not None and len(schur) and not self._schur_off:
            try:
                return self._checked(native.factorize(upper, schur), upper, check)
            except Exception as e:
                self._schur_off = True
                msg = (f"PARDISO's Schur complement failed ({type(e).__name__}: {e}); "
                       "solving for each port instead")
                log.warning(msg)
                if warn is not None:
                    warn(msg)
        if native is not None:
            try:
                factor = native.factorize(upper)
                given = check() if check is not None else None
                b = _check_block(given, n)
                x = factor(b)
                rel = _worst_residual(upper, x, b)
                if np.isfinite(rel) and rel <= CHECK_TOL_COMPLEX:
                    if b is given:
                        self.checked = (b, x)
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

    def _checked(self, factor: ComplexFactor, upper: sp.csr_matrix,
                 check: Optional[Callable[[], np.ndarray]]) -> ComplexFactor:
        """``factor`` once a solve with it checks out (see :meth:`factorize`); raises if
        not."""
        given = check() if check is not None else None
        b = _check_block(given, upper.shape[0])
        x = factor(b)
        rel = _worst_residual(upper, x, b)
        if not (np.isfinite(rel) and rel <= CHECK_TOL_COMPLEX):
            raise RuntimeError(f"relative residual {rel:.1e}")
        if b is given:
            self.checked = (b, x)
        return factor

    def close(self):
        """Release PARDISO's memory now, rather than when this is garbage collected."""
        if self._native is not None:
            self._native._finalizer()
            self._native = None


def rhs_block(n: int, max_cols: int = 64, memory: bool = False) -> int:
    """How many right-hand sides of ``n`` complex unknowns to solve together, at most
    ``max_cols``. With ``memory``, as many as :data:`RHS_MEMORY_SHARE` of the memory free
    holds, if that is more than :data:`RHS_BLOCK_BYTES` does (for one solve at a time, not
    for threads that each take a block)."""
    budget = RHS_BLOCK_BYTES
    if memory:
        free = free_memory()
        if free is not None:
            budget = max(budget, int(RHS_MEMORY_SHARE * free) // 2)   # block + solutions
    return int(np.clip(budget // (16 * max(n, 1)), 1, max(max_cols, 1)))
