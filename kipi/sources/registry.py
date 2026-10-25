"""The board files KiPI can open, and which source reads each.

The Open dialog's filters come from :data:`FORMATS`; a new reader (CCE) is one more entry
here.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from kipi.sources.base import BoardSource


@dataclass(frozen=True)
class FileFormat:
    name: str                       # for the Open dialog: "KiPI snapshot"
    patterns: Tuple[str, ...]       # file name patterns, lower case: ("*.json.gz", ...)
    open: Callable[[Path], BoardSource]
    # Formats that can also be a folder: whether the folder ``path`` is one.
    folder: Optional[Callable[[Path], bool]] = None

    def matches(self, path) -> bool:
        if Path(path).is_dir():
            return self.folder is not None and self.folder(Path(path))
        name = Path(path).name.lower()
        return any(fnmatch.fnmatch(name, p) for p in self.patterns)


def _kicad_board(path: Path) -> BoardSource:
    from kipi.sources.kicad_pcb.source import KiCadFileSource
    return KiCadFileSource(path)


def _odb(path: Path) -> BoardSource:
    from kipi.sources.odbpp.source import OdbFileSource
    return OdbFileSource(path)


def _odb_folder(path: Path) -> bool:
    return (path / "matrix" / "matrix").is_file()


def _hyp(path: Path) -> BoardSource:
    from kipi.sources.hyp.source import HypFileSource
    return HypFileSource(path)


def _snapshot(path: Path) -> BoardSource:
    from kipi.sources.snapshot_file import SnapshotFileSource
    return SnapshotFileSource(path)


FORMATS: List[FileFormat] = [
    FileFormat("KiCad board", ("*.kicad_pcb",), _kicad_board),
    # An unpacked job is opened by its folder, or by picking its matrix/matrix file.
    FileFormat("ODB++", ("*.zip", "*.tgz", "*.tar.gz", "matrix"), _odb, _odb_folder),
    FileFormat("HyperLynx", ("*.hyp",), _hyp),
    FileFormat("KiPI snapshot", ("*.json.gz", "*.json"), _snapshot),
]


def format_for(path) -> Optional[FileFormat]:
    return next((f for f in FORMATS if f.matches(path)), None)


def open_board(path) -> BoardSource:
    """The source that reads the board file ``path``; ValueError if no reader knows it."""
    fmt = format_for(path)
    if fmt is None:
        known = ", ".join(p for f in FORMATS for p in f.patterns)
        raise ValueError(f"KiPI cannot open {Path(path).name}: it reads {known}")
    return fmt.open(Path(path))


def file_filter() -> str:
    """A Qt file dialog filter: every board KiPI reads, then each format."""
    every = " ".join(p for f in FORMATS for p in f.patterns)
    return ";;".join([f"Boards ({every})"] +
                     [f"{f.name} ({' '.join(f.patterns)})" for f in FORMATS])
