"""Time stamps in the names of the files a solve writes (reports, saved results), so a new
solve does not overwrite the last one and the names sort in the order they were made."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

# Sorts by time and is safe in file names on every platform (no ':').
TIMESTAMP_FORMAT = "%Y-%m-%d_%H%M%S"


def timestamp(when: Optional[datetime] = None) -> str:
    """``when`` (default: now, local time) as '2026-10-15_143012'."""
    return (when or datetime.now()).strftime(TIMESTAMP_FORMAT)


def stamped(stem: str, when: Optional[datetime] = None) -> str:
    """``stem`` with a time stamp appended: 'board' -> 'board_2026-10-15_143012'."""
    return f"{stem}_{timestamp(when)}"
