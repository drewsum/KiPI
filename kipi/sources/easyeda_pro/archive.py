"""Open an EasyEDA Pro ``.epro2`` project archive.

The archive is a ZIP holding ``project2.json`` and the project's ``.epru`` document
stream. Everything is read in place: nothing is extracted, so a hostile archive cannot
write outside itself.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

#: A project stream is a few hundred kilobytes; this only stops a decompression bomb.
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10_000


class EasyedaError(ValueError):
    """The archive cannot be read as an EasyEDA Pro project."""


@dataclass
class ProjectFiles:
    """What the archive holds, before the document stream is parsed."""

    path: Path
    project: Dict[str, Any] = field(default_factory=dict)
    stream_name: str = ""
    stream_text: str = ""

    @property
    def title(self) -> str:
        title = str(self.project.get("title") or "").strip()
        return title or self.path.stem

    @property
    def editor_version(self) -> str:
        return str(self.project.get("editorVersion") or "").strip()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


def open_project(path) -> ProjectFiles:
    """Read ``project2.json`` and the ``.epru`` stream from an ``.epro2`` archive."""
    path = Path(path)
    if not path.is_file():
        raise EasyedaError(f"{path.name} does not exist.")
    if not zipfile.is_zipfile(path):
        raise EasyedaError(
            f"{path.name} is not an EasyEDA Pro .epro2 archive: it is not a ZIP file.")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise EasyedaError(f"{path.name} cannot be opened as a ZIP archive: {exc}") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise EasyedaError(
                f"{path.name} has more than {MAX_MEMBERS} files; it is not a project "
                "archive KiPI reads.")
        for info in infos:
            if _is_encrypted(info):
                raise EasyedaError(
                    f"{path.name} is password-protected; save it in EasyEDA Pro without a "
                    "password and open it again.")
            if info.file_size > MAX_MEMBER_BYTES:
                raise EasyedaError(
                    f"{path.name} contains a file larger than "
                    f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB; it is not a project KiPI reads.")

        names = [info.filename for info in infos]
        lower = {name.lower(): name for name in names}
        streams = [name for name in names if name.lower().endswith(".epru")]
        if not streams:
            if any(name.lower().endswith((".epcb", ".esch")) for name in names):
                raise EasyedaError(
                    f"{path.name} is an older EasyEDA Pro .epro project; open it in "
                    "EasyEDA Pro 4 and save it as .epro2.")
            raise EasyedaError(
                f"{path.name} is not an EasyEDA Pro project: it has no .epru file.")
        # The project stream is normally named after the project; otherwise take the first.
        stream_name = next(
            (name for name in streams if Path(name).stem.lower() == path.stem.lower()),
            streams[0])

        try:
            stream_text = _decode(archive.read(stream_name))
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise EasyedaError(f"{path.name} cannot be read: {exc}") from exc

        project: Dict[str, Any] = {}
        meta_name = lower.get("project2.json")
        if meta_name is not None:
            try:
                value = json.loads(_decode(archive.read(meta_name)))
                if isinstance(value, dict):
                    project = value
            except (OSError, ValueError):
                project = {}

    return ProjectFiles(path=path, project=project,
                        stream_name=stream_name, stream_text=stream_text)
