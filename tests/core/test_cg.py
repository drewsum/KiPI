"""The AMG-CG path of the linear solver: tolerance, warm starts and stalls."""
import numpy as np
import pytest
import scipy.sparse as sp

pytest.importorskip("pyamg")

from kipi.core.solver import spd  # noqa: E402
from kipi.core.solver.spd import CG_ACCEPT_FACTOR, SpdSolver  # noqa: E402
from kipi.dc.solver.setup import SolveSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _amg_only(monkeypatch):
    """These tests are about the iterative path, whatever size the direct solver takes."""
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS", 0)
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO", 0)
    monkeypatch.setattr(spd, "PARDISO_MEMORY_SHARE", 0)


def _poisson(n=200):
    """Grounded 2D Laplacian (Dirichlet on the boundary): SPD, n*n unknowns."""
    t = sp.diags_array([-1.0, 2.0, -1.0], offsets=[-1, 0, 1], shape=(n, n))
    eye = sp.identity(n)
    return (sp.kron(t, eye) + sp.kron(eye, t)).tocsr()


def _rel(A, x, b):
    return np.linalg.norm(b - A @ x) / np.linalg.norm(b)


def test_warm_start_meets_the_tolerance_without_a_direct_fallback():
    A = _poisson()
    b = np.random.default_rng(0).standard_normal(A.shape[0])
    s = SolveSettings()
    stats = {}
    f = SpdSolver(A, s, _quiet(), stats)
    f.solve(b, _quiet())
    # A slightly different right-hand side, as in a later power-tree pass. pyamg's CG
    # tolerance is relative to ||b||; scaling it by the warm-start residual stopped CG
    # almost at once and forced a direct solve every pass.
    b2 = b * 1.001
    x2 = f.solve(b2, _quiet())
    assert stats["method"] == "amg-cg"
    assert stats["iterations"] > 0
    assert _rel(A, x2, b2) <= s.tol * CG_ACCEPT_FACTOR


def test_unreachable_tolerance_stops_early_instead_of_running_to_max_iter():
    A = _poisson()
    b = np.random.default_rng(1).standard_normal(A.shape[0])
    s = SolveSettings(tol=1e-30, max_iter=2000)    # below what double precision can reach
    stats = {}
    f = SpdSolver(A, s, _quiet(), stats)
    x = f.solve(b, _quiet())
    assert stats["iterations"] < 200
    assert stats["method"].startswith("direct (")   # could not get within 100x of tol
    assert stats["method"].endswith("fallback)")
    assert _rel(A, x, b) < 1e-10
    # The fallback factorization is kept for the next right-hand side.
    x2 = f.solve(2 * b, _quiet())
    assert np.allclose(x2, 2 * x)


def _quiet():
    from kipi.core.progress import NULL
    return NULL
