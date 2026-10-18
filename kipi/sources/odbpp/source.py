"""An ODB++ job (folder, ``.zip`` or ``.tgz``) as a
:class:`~kipi.sources.base.BoardSource`."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.base import BoardSource, Capabilities


def job_root(path: Path) -> Path:
    """The job itself for a picked ``matrix/matrix`` file; ``path`` otherwise."""
    if path.name.lower() == "matrix" and path.parent.name.lower() == "matrix":
        return path.parent.parent
    return path


class OdbFileSource(BoardSource):
    """The board in an ODB++ job. Reload reads the job again, e.g. after exporting it
    anew."""
    caps = Capabilities(reload=True)

    def __init__(self, path, step: Optional[str] = None):
        self.path = job_root(Path(path))
        self.step = step                # the board step to read; None = picked
        self.name = self.path.name
        self.description = f"the ODB++ job {self.path}"

    def load(self, progress: ProgressReporter = NULL,
             refill_zones: bool = False) -> BoardSnapshot:
        from kipi.sources.odbpp.reader import read_board
        return read_board(self.path, progress, self.step)

    def files(self) -> List[Path]:
        if self.path.is_dir():                  # an export rewrites the matrix
            return [self.path / "matrix" / "matrix"]
        return [self.path]
