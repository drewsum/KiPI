"""Build the KiCad Plugin and Content Manager (PCM) package from the release zip (#7).

    python pcm_package.py <tag> <KiPI-<tag>.zip> <out dir> [--status stable|testing]

Writes two files to the out dir:

* ``KiPI-<tag>-pcm.zip``: what PCM installs. The validator in KiCad's metadata repository
  (gitlab.com/kicad/addons/metadata, ``ci/validate/package.py``) allows only
  ``metadata.json`` (one version, no ``download_sha256``), ``resources/icon.png`` and
  ``plugins/**``. ``plugins/`` holds exactly the files of the release zip's ``KiPI/``
  folder, so a PCM install and a manual one are the same files.
* ``metadata.json``: the same metadata plus the download URL, sha256 and sizes, the
  version entry to add to ``packages/com.github.drewsum.kipi/metadata.json`` in the
  metadata repository. The hash can't be inside the zip it describes, so only this copy
  has it.

The fixed metadata is ``.github/pcm/metadata.json``. Entries keep the release zip's
timestamps (git archive uses the commit time), so the same zip in gives the same sha256 out.

A tag that isn't a PCM version (``major[.minor[.patch]]``, so no ``-rc`` or ``.dev``) gets
no PCM package: a warning, and exit 0, so the release zip is still attached.

Standard library only, so the workflow needs no installs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import struct
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

TEMPLATE = Path(__file__).resolve().parents[1] / "pcm" / "metadata.json"
PREFIX = "KiPI/"                          # the release zip's top folder
ICON = "resources/kipi_64.png"            # inside it
VERSION_RE = re.compile(r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
# The metadata repository's validator limits.
MAX_ICON_PX, MAX_ICON_BYTES = 64, 20480
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PackageError(Exception):
    pass


def pcm_version(tag: str) -> Optional[str]:
    """``v1.2.3`` -> ``1.2.3``; None if PCM wouldn't accept it."""
    v = tag[1:] if tag[:1] in ("v", "V") else tag
    return v if VERSION_RE.match(v) else None


def png_size(data: bytes) -> Tuple[int, int]:
    """Width and height from a PNG's IHDR chunk."""
    if data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise PackageError("the icon is not a PNG")
    return struct.unpack(">II", data[16:24])


def version_entry(template: dict, version: str, status: Optional[str]) -> dict:
    """The template's one version entry, for this version."""
    if len(template.get("versions", [])) != 1:
        raise PackageError("the metadata template needs exactly one version entry")
    entry = {"version": version, **template["versions"][0]}
    if status:
        entry["status"] = status
    return entry


def build_zip(src: Path, dest: Path, inner: dict) -> None:
    """The PCM zip: ``metadata.json``, ``resources/icon.png`` and the release zip's
    ``KiPI/`` folder as ``plugins/``."""
    with zipfile.ZipFile(src) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        stray = [i.filename for i in infos if not i.filename.startswith(PREFIX)]
        if stray:
            raise PackageError(f"files outside {PREFIX} in {src.name}: {stray[:5]}")
        names = {i.filename for i in infos}
        if PREFIX + ICON not in names:
            raise PackageError(f"{ICON} is missing from {src.name}")
        stamp = max(i.date_time for i in infos)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
            def put(name: str, data: bytes, date_time, mode: int = 0o100644) -> None:
                info = zipfile.ZipInfo(name, date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = mode << 16
                out.writestr(info, data)

            put("metadata.json", (json.dumps(inner, indent=4) + "\n").encode(), stamp)
            put("resources/icon.png", z.read(PREFIX + ICON), stamp)
            for i in infos:
                mode = (i.external_attr >> 16) or 0o100644
                put("plugins/" + i.filename[len(PREFIX):], z.read(i), i.date_time, mode)


def check(zip_path: Path, outer: dict) -> List[str]:
    """What the metadata repository's validator would reject, as far as it can be told
    without downloading: an empty list when the package passes."""
    problems = []
    v = outer["versions"][0]
    size = zip_path.stat().st_size
    if size > MAX_DOWNLOAD_BYTES:
        problems.append(f"the zip is {size} bytes, over the {MAX_DOWNLOAD_BYTES} byte limit")
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None:
            problems.append("the zip has a CRC error")
        files = [i for i in z.infolist() if not i.is_dir()]
        for i in files:
            p = PurePosixPath(i.filename)
            if i.filename not in ("metadata.json", "resources/icon.png") and p.parts[0] != "plugins":
                problems.append(f"extra file {i.filename}")
        if not any(i.filename == "plugins/plugin.json" for i in files):
            problems.append("no plugins/plugin.json")
        icon = z.read("resources/icon.png")
        w, h = png_size(icon)
        if w > MAX_ICON_PX or h > MAX_ICON_PX:
            problems.append(f"the icon is {w}x{h} px, over {MAX_ICON_PX}x{MAX_ICON_PX}")
        if len(icon) > MAX_ICON_BYTES:
            problems.append(f"the icon is {len(icon)} bytes, over {MAX_ICON_BYTES}")
        inner = json.loads(z.read("metadata.json"))
        plugin = json.loads(z.read("plugins/plugin.json"))
    if inner.get("identifier") != outer.get("identifier"):
        problems.append("the identifiers of the two metadata copies differ")
    if plugin.get("identifier") != outer.get("identifier"):
        problems.append("plugin.json's identifier differs from the metadata's")
    if len(inner.get("versions", [])) != 1:
        problems.append("the zip's metadata must have exactly one version")
    else:
        iv = inner["versions"][0]
        if "download_sha256" in iv:
            problems.append("the zip's metadata can't have download_sha256")
        for field in ("version", "status", "kicad_version", "kicad_version_max", "platforms"):
            if iv.get(field) != v.get(field):
                problems.append(f"the two metadata copies have different {field}")
    if v.get("download_sha256") != sha256(zip_path):
        problems.append("download_sha256 doesn't match the zip")
    if v.get("download_size") != size:
        problems.append("download_size doesn't match the zip")
    if v.get("install_size") != install_size(zip_path):
        problems.append("install_size doesn't match the zip")
    return problems


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_size(path: Path) -> int:
    """Unpacked bytes, counted as the validator counts them."""
    with zipfile.ZipFile(path) as z:
        return sum(i.file_size for i in z.infolist() if not i.is_dir())


def package(tag: str, src: Path, out_dir: Path, repo: str, status: Optional[str] = None,
            template_path: Path = TEMPLATE) -> Optional[Dict[str, Path]]:
    """Build the PCM zip and the metadata repository's ``metadata.json``. None (and a
    warning) when the tag isn't a PCM version."""
    version = pcm_version(tag)
    if version is None:
        print(f"::warning title=No PCM package::{tag} is not a PCM version (major[.minor"
              "[.patch]], at most 4.4.6 digits): only the release zip is attached.")
        return None
    template = json.loads(template_path.read_text(encoding="utf-8"))
    entry = version_entry(template, version, status)
    inner = {**copy.deepcopy(template), "versions": [entry]}

    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"KiPI-{tag}-pcm.zip"
    zip_path = out_dir / name
    build_zip(src, zip_path, inner)

    outer = copy.deepcopy(inner)
    outer["versions"][0].update({
        "download_url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
        "download_sha256": sha256(zip_path),
        "download_size": zip_path.stat().st_size,
        "install_size": install_size(zip_path),
    })
    problems = check(zip_path, outer)
    if problems:
        raise PackageError("the PCM package would fail validation:\n  " + "\n  ".join(problems))
    meta_path = out_dir / "metadata.json"
    meta_path.write_text(json.dumps(outer, indent=4) + "\n", encoding="utf-8")
    return {"zip": zip_path, "metadata": meta_path}


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tag")
    ap.add_argument("zip", type=Path, help="the release zip, KiPI-<tag>.zip")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--status", choices=("stable", "testing", "development"),
                    help="overrides the template's (a prerelease is 'testing')")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "drewsum/KiPI"))
    args = ap.parse_args(argv[1:])
    try:
        out = package(args.tag, args.zip, args.out_dir, args.repo, args.status)
    except PackageError as e:
        print(f"::error title=PCM package::{e}")
        return 1
    if out:
        v = json.loads(out["metadata"].read_text(encoding="utf-8"))["versions"][0]
        print(f"{out['zip'].name}: version {v['version']} ({v['status']}), "
              f"{v['download_size']} bytes, {v['install_size']} unpacked, sha256 {v['download_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
