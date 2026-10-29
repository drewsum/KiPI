"""Cores this process may run on, for sizing the threads and worker processes that split
work across them (#226)."""

from __future__ import annotations

import os


def cores() -> int:
    """Logical processors this process may use: those it is limited to where the system
    says (an affinity mask, a container's share), else all of them; at least 1."""
    for count in (getattr(os, "process_cpu_count", None),                # Python 3.13+
                  lambda: len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity")
                  else None,
                  os.cpu_count):
        try:
            n = count() if count is not None else None
        except (OSError, ValueError):
            n = None
        if n:
            return n
    return 1


def spare() -> int:
    """Cores to split work across, leaving one for the window: at least 1."""
    return max(cores() - 1, 1)
