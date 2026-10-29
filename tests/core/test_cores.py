"""Threads and worker processes sized by the cores this process may use and the memory
free, with no fixed most (#226)."""

from kipi.core import cores, workers


def test_cores_are_those_this_process_may_use(monkeypatch):
    monkeypatch.setattr(cores.os, "process_cpu_count", lambda: 6, raising=False)
    monkeypatch.setattr(cores.os, "cpu_count", lambda: 20)
    assert cores.cores() == 6 and cores.spare() == 5


def test_cores_fall_back_to_all_of_them(monkeypatch):
    monkeypatch.setattr(cores.os, "process_cpu_count", lambda: None, raising=False)
    monkeypatch.delattr(cores.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(cores.os, "cpu_count", lambda: 20)
    assert cores.cores() == 20 and cores.spare() == 19

    def refused(*a):
        raise OSError("no affinity here")
    monkeypatch.setattr(cores.os, "sched_getaffinity", refused, raising=False)
    assert cores.cores() == 20
    monkeypatch.setattr(cores.os, "cpu_count", lambda: None)
    assert cores.cores() == 1 and cores.spare() == 1


def test_workers_take_every_spare_core(monkeypatch):
    monkeypatch.setattr(workers, "spare", lambda: 31)
    assert workers.workers(100) == 31
    assert workers.workers(5) == 5
    assert workers.workers(1) == 0


def test_workers_fit_in_half_the_memory_free(monkeypatch):
    mb = 2**20
    monkeypatch.setattr(workers, "free_memory", lambda: 64 * 2**30)
    # 256 MB + 16 x 21 MB = 592 MB a worker: 32 GB holds 55.
    assert workers.fit(31, 21 * mb) == 31
    assert workers.fit(100, 21 * mb) == 32 * 1024 // 592
    monkeypatch.setattr(workers, "free_memory", lambda: 2 * 2**30)
    assert workers.fit(31, 21 * mb) == 2                 # a pool, however small
    assert workers.fit(31, 2 * mb) == 1024 // 288
    monkeypatch.setattr(workers, "free_memory", lambda: None)
    assert workers.fit(31, 21 * mb) == workers.UNKNOWN_MEMORY_WORKERS
    assert workers.fit(3, 21 * mb) == 3
