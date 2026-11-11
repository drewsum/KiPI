# Copyright (c) 2026 Tomachie LLC.
"""Limits on a headless solve: a memory cap fails an allocation instead of paging, and the
thread counts reach the BLAS pools. Each in a child process, which the cap stays with."""

import subprocess
import sys
import textwrap

import pytest


def _child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                          capture_output=True, text=True, timeout=300)


@pytest.mark.skipif(sys.platform != "win32", reason="the Job Object cap is Windows'")
def test_an_allocation_over_the_memory_cap_fails():
    out = _child("""
        from kipi.core import limits
        limits.apply(threads=1, memory_gb=0.5, low_priority=True)
        import numpy as np
        try:
            np.ones(2 ** 28)            # 2 GB of float64
        except MemoryError:
            print("refused")
        print(round(limits.peak_memory_gb() or 0, 2))
    """)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split()
    assert lines[0] == "refused" and float(lines[1]) < 0.5


def test_the_thread_counts_are_set_before_numpy_loads():
    out = _child("""
        from kipi.core import limits
        limits.apply(threads=2, low_priority=False)
        import os
        print(sorted({os.environ[v] for v in limits.THREAD_VARIABLES}))
    """)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['2']"
