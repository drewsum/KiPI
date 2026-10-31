"""The dependency lists agree: requirements.txt (what KiCad installs into the plugin's venv)
is pyproject.toml's "kicad" extra (kicad-python) alone, and KiPI installs its dependencies
(requirements-standalone.txt) on the first click (#7)."""

from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")      # Python 3.11+; the lists are the same anywhere

ROOT = Path(__file__).resolve().parents[2]


def _requirements(path: Path):
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return {line.replace(" ", "") for line in lines if line}


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_kicad_installs_only_what_kipi_needs_to_start():
    kicad = {d.replace(" ", "") for d in _pyproject()["optional-dependencies"]["kicad"]}
    assert _requirements(ROOT / "requirements.txt") == kicad


def test_standalone_needs_no_kicad():
    project = _pyproject()
    assert not any(d.startswith("kicad-python") for d in project["dependencies"])
    assert any(d.startswith("kicad-python") for d in project["optional-dependencies"]["kicad"])


def test_dev_tools_match():
    dev = {d.replace(" ", "") for d in _pyproject()["optional-dependencies"]["dev"]}
    assert _requirements(ROOT / "requirements-dev.txt") == dev


def test_the_launcher_installs_what_standalone_needs():
    standalone = {d.replace(" ", "") for d in _pyproject()["dependencies"]}
    assert _requirements(ROOT / "requirements-standalone.txt") == standalone


def test_python_m_kipi_checks_every_standalone_package():
    """kipi/__main__.py names the module each requirement is imported under."""
    from kipi.__main__ import _MODULES
    import_names = {"pyside6-essentials": "pyside6", "pyyaml": "yaml"}
    wanted = set()
    for line in _requirements(ROOT / "requirements-standalone.txt"):
        name = line
        for sep in "<>=!~;[":
            name = name.split(sep, 1)[0]
        wanted.add(import_names.get(name.lower(), name.lower()))
    assert {m.lower() for m in _MODULES} == wanted
