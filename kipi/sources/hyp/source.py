"""A HyperLynx board file (``.hyp``) as a :class:`~kipi.sources.base.BoardSource`."""

from __future__ import annotations

from pathlib import Path
from typing import List

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.base import BoardSource, Capabilities


class HypFileSource(BoardSource):
    """The board in a ``.hyp`` file. Reload reads the file again, e.g. after exporting it
    anew."""
    caps = Capabilities(reload=True)

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.description = f"the HyperLynx file {self.path}"

    def load(self, progress: ProgressReporter = NULL,
             refill_zones: bool = False) -> BoardSnapshot:
        from kipi.sources.hyp.reader import read_board
        return read_board(self.path, progress)

    def files(self) -> List[Path]:
        return [self.path]
