"""An ODB++ job as a set of text files, from a folder, a ``.zip`` or a ``.tgz``.

Files are read in place, never unpacked to disk. Names are matched without regard to
case: ODB++ names its folders in lower case, but tools differ, and so do file systems.

* A file saved with Unix compress (``features.Z``, ``data.Z``: Valor / Mentor's
  "compress" option) is read as the file without the ``.Z``.
* A ``.tgz`` can only be read front to back: a file earlier in it than the last one read
  means decompressing from the start again, which made large jobs take many minutes.
  Once the step to read is known (:meth:`Job.focus`), its files are read in one pass in
  archive order and kept.
* An archive holding no job but one archive that does (an OrCAD ``_ODBGBR.zip`` around
  its ``.tgz``) is read from the inner archive.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from kipi.sources.odbpp import lzw

ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz", ".tar")
MAX_FILE_BYTES = 1 << 30        # one ODB++ file larger than this is not a board
MAX_INNER_BYTES = 2 << 30       # an archive inside the archive larger than this is not read
COMPRESSED = ".z"
#: Folders every step's reading needs besides the step itself.
SHARED = ("matrix/", "misc/", "fonts/", "symbols/")


class OdbError(ValueError):
    """The file is not an ODB++ job KiPI can read; the message says why, for the user."""


def job_name(path: Path) -> str:
    """The job's name: the archive or folder name without its archive suffix."""
    name = path.name
    if path.is_file() and name.lower() == "matrix":           # picked matrix/matrix
        return path.parent.parent.name
    for suffix in ARCHIVE_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def _is_matrix(n: str) -> bool:
    n = n.lower()
    tail = "matrix/matrix"
    return (n.endswith(tail) or n.endswith(tail + COMPRESSED)) and \
        (n in (tail, tail + COMPRESSED) or n[:n.rfind(tail)].endswith("/"))


def _top(n: str) -> str:
    return n[:n.lower().rfind("matrix/matrix")]


class Job:
    """The files of one ODB++ job, by path relative to the job's top folder."""

    def __init__(self, path):
        self.path = Path(path)
        self._zip: Optional[zipfile.ZipFile] = None
        self._tar: Optional[tarfile.TarFile] = None
        self._dir: Optional[Path] = None
        self._cache: Dict[str, bytes] = {}
        p = self.path
        try:
            if p.is_dir():
                self._dir = p
                names = [f.relative_to(p).as_posix() for f in p.rglob("*") if f.is_file()]
            elif p.is_file() and p.name.lower() == "matrix" and p.parent.name.lower() == "matrix":
                self._dir = p.parent.parent
                names = [f.relative_to(self._dir).as_posix()
                         for f in self._dir.rglob("*") if f.is_file()]
            else:
                names = self._open_archive(p, p.name)
                if not any(map(_is_matrix, names)):
                    names = self._open_inner(names) or names
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
            raise OdbError(f"{p.name} could not be opened as an ODB++ job ({e})") from e

        # The job's top folder is the one holding matrix/matrix; an archive often wraps
        # it in a folder of its own.
        tops = [_top(n) for n in names if _is_matrix(n)]
        if not tops:
            self.close()
            raise OdbError(f"{p.name} is not an ODB++ job: it has no matrix/matrix file")
        self.prefix = min(tops, key=len)
        self._names: Dict[str, str] = {}
        for n in names:
            if n.lower().startswith(self.prefix.lower()):
                self._names[n[len(self.prefix):].lower()] = n

    # ------------------------------------------------------------------ opening
    def _open_archive(self, src, label: str) -> List[str]:
        """Open ``src`` (a path, or a file object) as a zip or a tar; its file names."""
        if zipfile.is_zipfile(src):
            if hasattr(src, "seek"):
                src.seek(0)
            self._zip = zipfile.ZipFile(src)
            return [i.filename for i in self._zip.infolist() if not i.is_dir()]
        if hasattr(src, "seek"):
            src.seek(0)
        try:
            self._tar = tarfile.open(fileobj=src) if hasattr(src, "read") else tarfile.open(src)
        except tarfile.TarError:
            raise OdbError(f"{label} is not an ODB++ job (a folder, .zip or .tgz)") from None
        return [m.name for m in self._tar.getmembers() if m.isfile()]

    def _open_inner(self, names: List[str]) -> Optional[List[str]]:
        """The one archive inside this one, opened in its place, when it holds the job."""
        inner = [n for n in names if n.lower().endswith(ARCHIVE_SUFFIXES)]
        if len(inner) != 1:
            return None
        data = self._raw(inner[0], MAX_INNER_BYTES)
        self.close()
        return self._open_archive(io.BytesIO(data), inner[0])

    # ------------------------------------------------------------------ reading
    def _raw(self, name: str, limit: int = MAX_FILE_BYTES) -> bytes:
        """The bytes of the archive member or file ``name`` (its full name)."""
        if name in self._cache:
            return self._cache[name]
        if self._dir is not None:
            return (self._dir / name).read_bytes()
        if self._zip is not None:
            if self._zip.getinfo(name).file_size > limit:
                raise OdbError(f"{name} is too large to be part of a board")
            return self._zip.read(name)
        member = self._tar.getmember(name)
        if member.size > limit:
            raise OdbError(f"{name} is too large to be part of a board")
        f = self._tar.extractfile(member)
        return f.read() if f is not None else b""

    def _name(self, rel: str) -> Optional[str]:
        rel = rel.lower()
        return self._names.get(rel) or self._names.get(rel + COMPRESSED)

    def focus(self, step: str) -> None:
        """Read the step's files (and the job's shared ones) in one pass in archive order,
        so later reads do not decompress a ``.tgz`` from the start again and again."""
        if self._tar is None:
            return
        wanted = SHARED + (f"steps/{step.lower()}/",)
        keep = {full for rel, full in self._names.items() if rel.startswith(wanted)}
        for member in self._tar.getmembers():          # archive order
            if member.name in keep and member.name not in self._cache:
                if member.size > MAX_FILE_BYTES:
                    continue
                f = self._tar.extractfile(member)
                self._cache[member.name] = f.read() if f is not None else b""

    def exists(self, rel: str) -> bool:
        return self._name(rel) is not None

    def list(self, folder: str) -> List[str]:
        """The names directly in ``folder`` (files and folders), lower case; a compressed
        file is listed without its ``.z``."""
        folder = folder.lower().rstrip("/") + "/"
        out = set()
        for n in self._names:
            if n.startswith(folder):
                part = n[len(folder):].split("/", 1)
                name = part[0]
                if len(part) == 1 and name.endswith(COMPRESSED):
                    name = name[:-len(COMPRESSED)]
                out.add(name)
        return sorted(out)

    def read(self, rel: str) -> Optional[str]:
        """The text of the file ``rel``, or None if the job has no such file."""
        name = self._name(rel)
        if name is None:
            return None
        data = self._raw(name)
        if name.lower().endswith(COMPRESSED) and lzw.is_compressed(data):
            try:
                data = lzw.decompress(data)
            except lzw.LzwError as e:
                raise OdbError(f"{rel}.Z could not be decompressed ({e})") from e
        return data.decode("utf-8", errors="replace")

    def names(self) -> Iterable[str]:
        return self._names.keys()

    def close(self):
        for a in (self._zip, self._tar):
            if a is not None:
                a.close()
        self._zip = self._tar = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
