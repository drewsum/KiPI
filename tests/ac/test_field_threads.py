"""The AC field solves without PARDISO, several frequencies at once in threads (#213):
the same impedances as one at a time, as many threads as memory allows, and one at a
time again when a thread fails."""

import numpy as np
import pytest

from kipi.ac.solver import field
from kipi.ac.solver.fem import ACSystem, PortDef
from kipi.core.progress import ProgressReporter
from kipi.core.solver import direct
from kipi.dc.pi.model import ACSettings, SolveSettings
from synthetic import make_board

F = np.logspace(3, 8, 6)


class Said(ProgressReporter):
    def __init__(self):
        super().__init__()
        self.said = []

    def message(self, text, level=0):
        self.said.append(text)


@pytest.fixture
def superlu(monkeypatch):
    monkeypatch.setenv(direct._ENV, "superlu")
    monkeypatch.setattr(direct, "_pardiso_module", lambda: None)


def system():
    ac = ACSettings()
    ac.max_edge_mm = 1.0
    return ACSystem(make_board(), ["+3V3"], ["GND"], [PortDef("P", "probe", ["p1"], ["p3"])],
                    [], SolveSettings(), ac)


def test_threads_solve_what_one_at_a_time_does(superlu, monkeypatch):
    monkeypatch.setattr(field, "threads", lambda *a: 0)
    one = field.field_solves(system(), F, Said())
    monkeypatch.setattr(field, "threads", lambda *a: 3)
    said = Said()
    many = field.field_solves(system(), F, said)
    assert np.array_equal(one, many)
    assert "Solving 5 frequencies 3 at a time (SuperLU, one core each)" in said.said
    expected = [f"  [{k + 1}/6] solving {field.fmt_hz(f)}" for k, f in enumerate(F)]
    assert sorted(t for t in said.said if " solving " in t) == sorted(expected)


def test_a_failing_thread_leaves_the_rest_one_at_a_time(superlu, monkeypatch):
    monkeypatch.setattr(field, "threads", lambda *a: 0)
    one = field.field_solves(system(), F, Said())
    monkeypatch.setattr(field, "threads", lambda *a: 2)
    real, calls = field._solve, []

    def solve(sys_, omega, progress, label):
        calls.append(omega)
        if len(calls) == 2:
            raise MemoryError("out of memory")
        return real(sys_, omega, progress, label)
    monkeypatch.setattr(field, "_solve", solve)
    said = Said()
    assert np.array_equal(one, field.field_solves(system(), F, said))
    assert sum("out of memory" in t for t in said.said) == 1


class Factor:
    def __init__(self, name, nbytes=0):
        self.name, self.nbytes = name, nbytes


class Stats:
    nnz = 1000


class System:
    n_free = 1000
    stats = Stats()


def test_threads_only_for_superlu_and_as_memory_allows(monkeypatch):
    monkeypatch.setattr(field, "spare", lambda: 15)
    monkeypatch.setattr(field, "free_memory", lambda: 64 * 2**30)
    assert field.threads(System(), Factor("pardiso"), 20) == 0
    assert field.threads(System(), Factor("pardiso (real form)"), 20) == 0
    assert field.threads(System(), Factor("superlu"), 20) == 15
    assert field.threads(System(), Factor("superlu"), 3) == 3
    assert field.threads(System(), Factor("superlu"), 1) == 0
    # 64 GB free, half for the threads: a little over 8 GB each leaves room for 3.
    assert field.threads(System(), Factor("superlu", 8 * 2**30), 20) == 3
    assert field.threads(System(), Factor("superlu", 40 * 2**30), 20) == 0
    monkeypatch.setattr(field, "free_memory", lambda: None)
    assert field.threads(System(), Factor("superlu"), 20) == field.UNKNOWN_MEMORY_THREADS


def test_free_memory_reads_something_here():
    free = field.free_memory()
    assert free is None or free > 0


def test_superlu_factors_say_their_size():
    import scipy.sparse as sp
    A = sp.diags([1.0 + 1j, 2.0, 3.0]).tocsc()
    assert direct._superlu_complex(A).nbytes >= 3 * 16


def test_the_first_frequency_is_predicted_from_the_last(superlu, monkeypatch, step_timings):
    """#224: the first frequency reports nothing until done, so its time is predicted."""
    monkeypatch.setattr(field, "threads", lambda *a: 0)
    monkeypatch.setattr("kipi.core.eta.MIN_RECORD_S", 0.0)
    expected = []

    class Expect(Said):
        def expect(self, seconds, upto=1.0):
            expected.append((seconds, upto))

    field.field_solves(system(), F, Expect())
    assert expected == [] and list(step_timings._load()) == ["ac-frequency/superlu"]
    field.field_solves(system(), F, Expect())
    assert len(expected) == 1 and expected[0][0] > 0 and expected[0][1] == 1 / len(F)
