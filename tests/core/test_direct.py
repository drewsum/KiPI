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
    monkeypatch.setattr(direct, "_mkl", None)
    monkeypatch.setattr(direct, "_native_problem", None)


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


def _stiff_sheet(n=40, contrast=1e12):
    """A sheet held at one corner whose couplings grow 1e12 across it, like a board's
    copper with vias and series links far stiffer than the planes: an exact solve leaves a
    relative residual above 1e-8 from rounding alone."""
    t = sp.diags_array([-1.0, 2.0, -1.0], offsets=[-1, 0, 1], shape=(n, n)).tolil()
    t[0, 0] = t[n - 1, n - 1] = 1.0
    eye = sp.identity(n)
    A = (sp.kron(t, eye) + sp.kron(eye, t)).tolil()
    A[0, 0] += 1.0
    d = sp.diags_array(np.sqrt(np.geomspace(1.0, contrast, n * n)))
    return (d @ A.tocsr() @ d).tocsr()


def test_an_exact_solve_of_a_stiff_system_is_kept(monkeypatch):
    # #163: PARDISO's result is judged by its backward error, not the relative residual,
    # which rounding pushes past 1e-8 on a big board though the solve is exact (SuperLU's
    # is no better).
    from scipy.sparse.linalg import splu

    class Exact:                       # PARDISO stood in for by an exact factorization
        def __init__(self, *a, **k):
            pass

        def factorize(self, upper):
            full = upper + sp.triu(upper, 1).T
            self.lu = splu(sp.csc_matrix(full), permc_spec="COLAMD")

        def solve(self, upper, b):
            return self.lu.solve(b)

        def free_memory(self, everything=False):
            pass

    class FakeModule:
        PyPardisoSolver = Exact
    monkeypatch.setattr(direct, "_pardiso", FakeModule)
    monkeypatch.setattr(direct, "_pardiso_problem", None)
    A = _stiff_sheet()
    b = np.ones(A.shape[0])
    warnings = []
    f = direct.factorize(A, warnings.append)
    x = f(b)
    assert _rel(A, x, b) > 1e-8                     # what the old check refused
    assert direct.backward_error(A, x, b) < 1e-15
    assert f.name == "pardiso" and not warnings


def test_backward_error_flags_a_wrong_solution():
    A = _poisson()
    b = np.ones(A.shape[0])
    assert direct.backward_error(A, np.zeros_like(b), b) == 1.0
    x = direct._superlu(A)(b)
    assert direct.backward_error(A, x, b) < 1e-15
    assert direct.backward_error(A, x * (1 + 1e-6), b) > direct.CHECK_TOL


def test_pardiso_when_installed():
    if direct.backend() != "pardiso":
        pytest.skip("PARDISO not available here")
    A = _poisson()
    b = np.random.default_rng(1).standard_normal(A.shape[0])
    f = direct.factorize(A)
    assert f.name == "pardiso"
    assert _rel(A, f(b), b) < 1e-12


# --- complex symmetric series (AC sweeps) ----------------------------------------------

def _ac_like(omega, n=40):
    """Complex symmetric, shaped like an AC model: a conductance network plus jωC to
    ground on every node."""
    A = _poisson(n).astype(complex)
    return (A + sp.identity(A.shape[0]) * (1j * omega * 1e-3)).tocsr()


def _upper(A):
    return sp.triu(A, format="csr")


def _complex_rhs(n, k=3):
    rng = np.random.default_rng(4)
    return rng.standard_normal((n, k)) + 1j * rng.standard_normal((n, k))


def test_complex_series_solves_each_matrix():
    series = direct.ComplexSeries()
    for omega in (1e3, 1e6, 1e9):
        A = _ac_like(omega)
        b = _complex_rhs(A.shape[0])
        f = series.factorize(_upper(A))
        if direct.backend() == "pardiso":
            assert f.name == "pardiso"
        assert _rel(A, f(b), b) < 1e-10
    series.close()


def test_complex_series_handles_a_new_pattern():
    series = direct.ComplexSeries()
    series.factorize(_upper(_ac_like(1e6, 30)))
    A = _ac_like(1e6, 31)
    b = _complex_rhs(A.shape[0])
    assert _rel(A, series.factorize(_upper(A))(b), b) < 1e-10


def test_complex_series_without_pardiso(no_pardiso):
    A = _ac_like(1e6)
    b = _complex_rhs(A.shape[0])
    f = direct.ComplexSeries().factorize(_upper(A))
    assert f.name == "superlu"
    assert _rel(A, f(b), b) < 1e-10


def test_complex_series_falls_back_when_native_fails(monkeypatch):
    def broken(self, upper):
        raise RuntimeError("PARDISO error -4")
    monkeypatch.setattr(direct._NativePardiso, "factorize", broken)
    warnings = []
    A = _ac_like(1e6)
    b = _complex_rhs(A.shape[0])
    f = direct.ComplexSeries().factorize(_upper(A), warnings.append)
    assert f.name in ("pardiso (real form)", "superlu")
    assert _rel(A, f(b), b) < 1e-10
    if direct.backend() == "pardiso":
        assert warnings and "Complex PARDISO" in warnings[0]


def test_inaccurate_native_result_falls_back(monkeypatch):
    def wrong(self, upper):
        return direct.ComplexFactor(lambda b: np.zeros_like(b), "pardiso")
    monkeypatch.setattr(direct._NativePardiso, "factorize", wrong)
    A = _ac_like(1e6)
    b = _complex_rhs(A.shape[0])
    f = direct.ComplexSeries().factorize(_upper(A))
    assert f.name != "pardiso"
    assert _rel(A, f(b), b) < 1e-10


def test_rhs_block_fits_the_budget():
    assert direct.rhs_block(10) == 64
    assert direct.rhs_block(2_000_000) == 16
    assert direct.rhs_block(10**9) == 1
    assert direct.rhs_block(10, max_cols=5) == 5


def test_rhs_block_grows_with_free_memory(monkeypatch):
    # #215: a quarter of the memory free for one block and its solutions, when that is
    # more than the fixed budget; never below it, and only when asked.
    n = 4_600_000
    monkeypatch.setattr(direct, "free_memory", lambda: 32 * 2**30)
    assert direct.rhs_block(n) == 7
    assert direct.rhs_block(n, max_cols=45, memory=True) == 45
    assert direct.rhs_block(n, max_cols=64, memory=True) == int(4 * 2**30 // (16 * n))
    monkeypatch.setattr(direct, "free_memory", lambda: 2**30)
    assert direct.rhs_block(n, max_cols=45, memory=True) == 7
    monkeypatch.setattr(direct, "free_memory", lambda: None)
    assert direct.rhs_block(n, max_cols=45, memory=True) == 7


class _ExactNative:
    """Native complex PARDISO stood in for by SuperLU on the full matrix, the Schur
    complement on ``schur`` rows from the inverse's block there."""
    def __init__(self, wrong=False, schur_fails=False):
        self.wrong = wrong
        self.schur_fails = schur_fails
        self.calls = []

    def factorize(self, upper, schur=None):
        from scipy.sparse.linalg import splu
        self.calls.append(schur)
        lu = splu(sp.csc_matrix(upper + sp.triu(upper, 1).T), permc_spec="COLAMD")
        if self.wrong:
            return direct.ComplexFactor(lambda b: np.zeros_like(b), "pardiso")
        S = None
        if schur is not None:
            if self.schur_fails:
                raise RuntimeError("PARDISO error -1 (phase 22)")
            e = np.zeros((upper.shape[0], len(schur)), complex)
            e[schur, np.arange(len(schur))] = 1
            S = np.linalg.inv(lu.solve(e)[schur])
        return direct.ComplexFactor(lu.solve, "pardiso", schur=S)


def _schur_reference(A, rows):
    Ad = A.toarray()
    rest = np.setdiff1d(np.arange(A.shape[0]), rows)
    return (Ad[np.ix_(rows, rows)]
            - Ad[np.ix_(rows, rest)] @ np.linalg.solve(Ad[np.ix_(rest, rest)],
                                                       Ad[np.ix_(rest, rows)]))


def test_schur_complement_from_the_series(monkeypatch):
    # #225: asked for one, the factor carries the Schur complement on those rows.
    series = direct.ComplexSeries()
    native = _ExactNative()
    monkeypatch.setattr(series, "_native_solver", lambda: native)
    A = _ac_like(1e6, 12)
    rows = np.array([3, 40, 77, 100])
    assert series.schur_ok
    f = series.factorize(_upper(A), schur=rows)
    assert np.allclose(f.schur, _schur_reference(A, rows), rtol=1e-10, atol=0)
    b = _complex_rhs(A.shape[0])
    assert _rel(A, f(b), b) < 1e-10


def test_a_failing_schur_complement_is_not_asked_for_again(monkeypatch):
    series = direct.ComplexSeries()
    native = _ExactNative(schur_fails=True)
    monkeypatch.setattr(series, "_native_solver", lambda: native)
    A = _ac_like(1e6, 12)
    warnings = []
    f = series.factorize(_upper(A), warnings.append, schur=np.array([1, 2]))
    assert f.name == "pardiso" and f.schur is None
    assert len(warnings) == 1 and "Schur complement failed" in warnings[0]
    assert not series.schur_ok
    f = series.factorize(_upper(A), warnings.append, schur=np.array([1, 2]))
    assert f.schur is None and len(warnings) == 1
    assert native.calls == [native.calls[0], None, None]


def test_native_schur_complement_when_installed():
    if direct.backend() != "pardiso":
        pytest.skip("PARDISO not available here")
    series = direct.ComplexSeries()
    if not series.schur_ok:
        pytest.skip("native complex PARDISO not available here")
    rows = np.array([0, 17, 250, 999, 1500])
    for omega in (1e3, 1e8):                 # a new matrix on the analysis already done
        A = _ac_like(omega)
        f = series.factorize(_upper(A), schur=rows)
        assert f.schur is not None
        assert np.allclose(f.schur, _schur_reference(A, rows), rtol=1e-8, atol=0)
        b = _complex_rhs(A.shape[0])
        assert _rel(A, f(b), b) < 1e-8      # a full solve through the partial phases
    f = series.factorize(_upper(A))          # and without, analyzed again
    assert f.schur is None and _rel(A, f(b), b) < 1e-8
    series.close()


def test_first_block_checks_the_factorization(monkeypatch):
    # #215: the caller's first block checks the factorization in place of a solve of its
    # own, and its solutions are handed back.
    series = direct.ComplexSeries()
    monkeypatch.setattr(series, "_native_solver", lambda: _ExactNative())
    A = _ac_like(1e6)
    b = _complex_rhs(A.shape[0])
    f = series.factorize(_upper(A), check=lambda: b)
    assert f.name == "pardiso"
    got, x = series.checked
    assert got is b and _rel(A, x, b) < 1e-10
    # A block of zeros can't check anything: ones are used, and nothing handed back.
    f = series.factorize(_upper(A), check=lambda: np.zeros_like(b))
    assert f.name == "pardiso" and series.checked is None


def test_a_wrong_factor_fails_the_block_check(monkeypatch):
    series = direct.ComplexSeries()
    monkeypatch.setattr(series, "_native_solver", lambda: _ExactNative(wrong=True))
    A = _ac_like(1e6)
    b = _complex_rhs(A.shape[0])
    warnings = []
    f = series.factorize(_upper(A), warnings.append, check=lambda: b)
    assert f.name != "pardiso" and series.checked is None
    assert warnings and _rel(A, f(b), b) < 1e-10


@pytest.fixture
def amg_only(monkeypatch):
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS", 0)
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO", 0)
    monkeypatch.setattr(spd, "PARDISO_MEMORY_SHARE", 0)


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


# --- PARDISO past its usual size limit while memory allows (#214) ---------------------

class _ExactPardiso:
    """PARDISO stood in for by an exact factorization of the upper triangle."""
    def __init__(self, *a, **k):
        pass

    def factorize(self, upper):
        from scipy.sparse.linalg import splu
        self.lu = splu(sp.csc_matrix(upper + sp.triu(upper, 1).T), permc_spec="COLAMD")

    def solve(self, upper, b):
        return self.lu.solve(b)

    def free_memory(self, everything=False):
        pass


@pytest.fixture
def big(monkeypatch):
    """The 3600-unknown Poisson system counts as past PARDISO's usual limit."""
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS", 100)
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO", 1000)

    class FakeModule:
        PyPardisoSolver = _ExactPardiso
    monkeypatch.setattr(direct, "_pardiso", FakeModule)
    monkeypatch.setattr(direct, "_pardiso_problem", None)


def test_pardiso_limit_grows_with_free_memory(big, monkeypatch):
    monkeypatch.setattr(spd, "free_memory", lambda: 64 * 2**30)
    assert spd.direct_solve_max_unknowns() == int(0.5 * 64 * 2**30 // 2048)
    monkeypatch.setattr(spd, "free_memory", lambda: 2**20)       # nearly none free
    assert spd.direct_solve_max_unknowns() == 1000
    monkeypatch.setattr(spd, "free_memory", lambda: None)
    assert spd.direct_solve_max_unknowns() == 1000


def test_superlu_keeps_its_limit_whatever_the_memory(no_pardiso, monkeypatch):
    monkeypatch.setattr(spd, "free_memory", lambda: 64 * 2**30)
    assert spd.direct_solve_max_unknowns() == spd.DIRECT_SOLVE_MAX_UNKNOWNS


def test_large_system_that_fits_is_factorized(big, monkeypatch):
    monkeypatch.setattr(spd, "free_memory", lambda: 2**30)
    A = _poisson()
    stats = {}
    s = spd.SpdSolver(A, SolveSettings(), NULL, stats)
    assert stats["method"] == "direct (pardiso)" and s.M is None
    b = np.ones(A.shape[0])
    assert _rel(A, s.solve(b, NULL), b) < 1e-12


def test_large_system_pardiso_fails_on_goes_to_amg_not_superlu(big, monkeypatch):
    monkeypatch.setattr(spd, "free_memory", lambda: 2**30)

    def out_of_memory(self, upper):
        raise MemoryError("no room")
    monkeypatch.setattr(_ExactPardiso, "factorize", out_of_memory)
    called = []
    monkeypatch.setattr(direct, "_superlu", lambda A: called.append(A))

    class Warned(type(NULL)):
        def __init__(self):
            super().__init__()
            self.said = []

        def warn(self, text):
            self.said.append(text)
    p = Warned()
    A = _poisson()
    stats = {}
    s = spd.SpdSolver(A, SolveSettings(), p, stats)
    assert stats["method"] == "amg-cg" and s.lu is None and not called
    assert any("no room" in w and "AMG-CG" in w for w in p.said)
    b = np.ones(A.shape[0])
    assert _rel(A, s.solve(b, NULL), b) < 1e-8


def test_factorize_without_superlu_raises_when_pardiso_is_missing(no_pardiso):
    with pytest.raises(direct.DirectError):
        direct.factorize(_poisson(), superlu=False)
