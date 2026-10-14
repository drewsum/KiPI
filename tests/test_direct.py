"""Direct-solver backends and the fallbacks between them, and reusing a solver as the
preconditioner of a slightly different matrix (the electro-thermal loop)."""
import numpy as np
import pytest
import scipy.sparse as sp

from kipi.core.progress import NULL
from kipi.core.solver import direct, spd
from kipi.dc.solver.setup import SolveSettings


def _poisson(n=60, scale=None):
    """Grounded 2D Laplacian, SPD; ``scale`` multiplies each unknown's couplings (a
    conductance change, like copper warming up)."""
    t = sp.diags_array([-1.0, 2.0, -1.0], offsets=[-1, 0, 1], shape=(n, n))
    eye = sp.identity(n)
    A = (sp.kron(t, eye) + sp.kron(eye, t)).tocsr()
    if scale is not None:
        d = sp.diags_array(np.sqrt(scale))
        A = (d @ A @ d).tocsr()
    return A


def _rel(A, x, b):
    return np.linalg.norm(b - A @ x) / np.linalg.norm(b)


@pytest.fixture
def no_pardiso(monkeypatch):
    monkeypatch.setattr(direct, "_pardiso", None)
    monkeypatch.setattr(direct, "_pardiso_problem", "test")


def test_superlu_fallback_when_pardiso_is_unavailable(no_pardiso):
    A = _poisson()
    b = np.random.default_rng(0).standard_normal(A.shape[0])
    f = direct.factorize(A)
    assert f.name == "superlu"
    assert direct.backend() == "superlu"
    assert "unavailable" in direct.backend_note()
    assert _rel(A, f(b), b) < 1e-12


def test_missing_pardiso_is_detected_once(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fail(name, *a, **k):
        if name == "pypardiso":
            raise ImportError("No module named 'pypardiso'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(direct, "_pardiso", None)
    monkeypatch.setattr(direct, "_pardiso_problem", None)
    monkeypatch.setattr(builtins, "__import__", fail)
    assert direct.backend() == "superlu"
    assert "pypardiso" in direct.backend_note()


def test_environment_variable_forces_superlu(monkeypatch):
    monkeypatch.setattr(direct, "_pardiso", None)
    monkeypatch.setattr(direct, "_pardiso_problem", None)
    monkeypatch.setenv("KIPI_DIRECT_SOLVER", "superlu")
    assert direct.backend() == "superlu"


def test_failed_pardiso_factorization_falls_back_to_superlu(monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            pass

        def factorize(self, A):
            raise RuntimeError("PARDISO error -4")

    class FakeModule:
        PyPardisoSolver = Broken
    monkeypatch.setattr(direct, "_pardiso", FakeModule)
    monkeypatch.setattr(direct, "_pardiso_problem", None)
    warnings = []
    A = _poisson()
    b = np.ones(A.shape[0])
    f = direct.factorize(A, warnings.append)
    assert f.name == "superlu"
    assert warnings and "PARDISO" in warnings[0]
    assert _rel(A, f(b), b) < 1e-12


def test_inaccurate_pardiso_result_falls_back_to_superlu(monkeypatch):
    class Wrong:
        def __init__(self, *a, **k):
            pass

        def factorize(self, A):
            pass

        def solve(self, A, b):
            return np.zeros_like(b)

        def free_memory(self, everything=False):
            pass

    class FakeModule:
        PyPardisoSolver = Wrong
    monkeypatch.setattr(direct, "_pardiso", FakeModule)
    monkeypatch.setattr(direct, "_pardiso_problem", None)
    assert direct.factorize(_poisson()).name == "superlu"


def test_pardiso_when_installed():
    if direct.backend() != "pardiso":
        pytest.skip("PARDISO not available here")
    A = _poisson()
    b = np.random.default_rng(1).standard_normal(A.shape[0])
    f = direct.factorize(A)
    assert f.name == "pardiso"
    assert _rel(A, f(b), b) < 1e-12


@pytest.fixture
def amg_only(monkeypatch):
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS", 0)
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO", 0)


def _hot_spot(n):
    """Up to 30% lower conductance in a hot spot."""
    return 1 / (1 + 0.3 * np.exp(-np.linspace(-3, 3, n) ** 2))


def test_previous_amg_hierarchy_preconditions_a_warmer_matrix(amg_only):
    s = SolveSettings()
    A0 = _poisson()
    n = A0.shape[0]
    b = np.random.default_rng(2).standard_normal(n)
    first = spd.SpdSolver(A0, s, NULL, {})
    first.solve(b, NULL)
    A1 = _poisson(scale=_hot_spot(n))
    stats = {}
    second = spd.SpdSolver(A1, s, NULL, stats, previous=first)
    assert second.reused
    x = second.solve(b, NULL)
    assert stats["method"] == "amg-cg (reused hierarchy)"
    assert stats["iterations"] > 0
    assert _rel(A1, x, b) <= s.tol * spd.CG_ACCEPT_FACTOR


def test_direct_factorization_is_not_reused():
    s = SolveSettings()
    A0 = _poisson()
    first = spd.SpdSolver(A0, s, NULL, {})
    assert first.lu is not None
    A1 = _poisson(scale=_hot_spot(A0.shape[0]))
    second = spd.SpdSolver(A1, s, NULL, {}, previous=first)
    assert not second.reused and second.lu is not None
    b = np.ones(A0.shape[0])
    assert _rel(A1, second.solve(b, NULL), b) < 1e-12


def test_reused_hierarchy_that_fails_sets_up_afresh(amg_only, monkeypatch):
    monkeypatch.setattr(spd, "CG_REUSED_MAX_ITERATIONS", 2)
    s = SolveSettings()
    A0 = _poisson()
    first = spd.SpdSolver(A0, s, NULL, {})
    A1 = _poisson(scale=np.random.default_rng(3).uniform(0.2, 1.0, A0.shape[0]))
    b = np.ones(A0.shape[0])
    stats = {}
    second = spd.SpdSolver(A1, s, NULL, stats, previous=first)
    x = second.solve(b, NULL)
    assert not second.reused
    assert _rel(A1, x, b) < 1e-10
    # Later right-hand sides use the new setup.
    assert _rel(A1, second.solve(2 * b, NULL), 2 * b) < 1e-9


def test_different_size_is_not_reused(amg_only):
    s = SolveSettings()
    first = spd.SpdSolver(_poisson(30), s, NULL, {})
    second = spd.SpdSolver(_poisson(31), s, NULL, {}, previous=first)
    assert not second.reused
