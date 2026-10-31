"""The release workflow's PCM package passes KiCad's metadata repository validator (#7)."""

import importlib.util
import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / ".github" / "scripts" / "pcm_package.py"
_spec = importlib.util.spec_from_file_location("pcm_package", _PATH)
pcm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pcm)

STAMP = (2026, 10, 31, 12, 0, 0)


def png(w, h):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    raw = b"".join(b"\0" + b"\0" * 4 * w for _ in range(h))
    return pcm.PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def release_zip(path, icon=None, extra=()):
    files = {
        "KiPI/plugin.json": json.dumps({"identifier": "com.github.drewsum.kipi"}),
        "KiPI/kipi_entry.py": "print('hi')\n",
        "KiPI/kipi/__init__.py": "__version__ = '1.0.0'\n",
        "KiPI/" + pcm.ICON: icon if icon is not None else png(64, 64),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("KiPI/", STAMP), b"")
        for name, data in [*files.items(), *extra]:
            z.writestr(zipfile.ZipInfo(name, STAMP), data)
    return path


@pytest.mark.parametrize("tag, version", [
    ("v1.0.0", "1.0.0"), ("v0.9.7", "0.9.7"), ("2.1", "2.1"), ("v3", "3"),
    ("v1.0.0-rc1", None), ("v1.0.0.dev0", None), ("v12345.0", None), ("v1.0.0.1", None),
])
def test_tags_become_pcm_versions(tag, version):
    assert pcm.pcm_version(tag) == version


def test_the_package_has_only_what_the_validator_allows(tmp_path):
    out = pcm.package("v1.0.0", release_zip(tmp_path / "KiPI-v1.0.0.zip"), tmp_path / "out",
                      "drewsum/KiPI")
    with zipfile.ZipFile(out["zip"]) as z:
        names = sorted(z.namelist())
        inner = json.loads(z.read("metadata.json"))
    assert names == ["metadata.json", "plugins/kipi/__init__.py", "plugins/kipi_entry.py",
                     "plugins/plugin.json", "plugins/" + pcm.ICON, "resources/icon.png"]
    assert len(inner["versions"]) == 1 and "download_sha256" not in inner["versions"][0]


def test_the_outer_metadata_describes_the_zip(tmp_path):
    out = pcm.package("v1.0.0", release_zip(tmp_path / "KiPI-v1.0.0.zip"), tmp_path / "out",
                      "drewsum/KiPI", status="testing")
    v = json.loads(out["metadata"].read_text())["versions"][0]
    assert v["version"] == "1.0.0" and v["status"] == "testing" and v["runtime"] == "ipc"
    assert v["download_url"] == ("https://github.com/drewsum/KiPI/releases/download/v1.0.0/"
                                 "KiPI-v1.0.0-pcm.zip")
    assert v["download_sha256"] == pcm.sha256(out["zip"])
    assert v["download_size"] == out["zip"].stat().st_size
    assert v["install_size"] == pcm.install_size(out["zip"])
    with zipfile.ZipFile(out["zip"]) as z:
        iv = json.loads(z.read("metadata.json"))["versions"][0]
    assert {k: iv[k] for k in ("status", "kicad_version", "platforms")} == \
        {k: v[k] for k in ("status", "kicad_version", "platforms")}


def test_the_same_release_zip_gives_the_same_hash(tmp_path):
    src = release_zip(tmp_path / "KiPI-v1.0.0.zip")
    a = pcm.package("v1.0.0", src, tmp_path / "a", "drewsum/KiPI")
    b = pcm.package("v1.0.0", src, tmp_path / "b", "drewsum/KiPI")
    assert a["zip"].read_bytes() == b["zip"].read_bytes()


def test_a_prerelease_tag_gets_no_package(tmp_path, capsys):
    assert pcm.package("v1.0.0-rc1", release_zip(tmp_path / "z.zip"), tmp_path / "out",
                       "drewsum/KiPI") is None
    assert "::warning" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_an_oversized_icon_fails(tmp_path):
    with pytest.raises(pcm.PackageError, match="128x128"):
        pcm.package("v1.0.0", release_zip(tmp_path / "z.zip", icon=png(128, 128)),
                    tmp_path / "out", "drewsum/KiPI")


def test_files_outside_the_kipi_folder_fail(tmp_path):
    with pytest.raises(pcm.PackageError, match="outside"):
        pcm.package("v1.0.0", release_zip(tmp_path / "z.zip", extra=[("stray.txt", "x")]),
                    tmp_path / "out", "drewsum/KiPI")


def test_the_template_matches_the_plugin_and_the_schema_rules():
    template = json.loads(pcm.TEMPLATE.read_text(encoding="utf-8"))
    plugin = json.loads((_ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert template["identifier"] == plugin["identifier"]
    assert template["license"] == "GPL-3.0"   # the v1 schema has no "-or-later" form
    assert len(template["description"]) <= 500 and len(template["description_full"]) <= 5000
    (v,) = template["versions"]
    assert v["runtime"] == "ipc" and "version" not in v
    icon = (_ROOT / pcm.ICON).read_bytes()
    assert pcm.png_size(icon) == (64, 64) and len(icon) <= pcm.MAX_ICON_BYTES
