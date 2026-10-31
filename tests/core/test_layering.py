"""Which parts of KiPI may import which.

KiPI runs as a KiCad plugin and on its own, reading boards from KiCad or from files.
Only :mod:`kipi.sources` knows where a board came from: the solvers, reports and views
work on a :class:`~kipi.core.model.board.BoardSnapshot` alone, and only the KiCad
source talks to kipy. These checks read every module's imports, including the ones
inside functions, so a KiCad-only import cannot creep back into shared code.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "kipi"

# Modules (as paths relative to the repository) that may import the KiCad source: the
# entry points, which pick where boards come from and hand that to the window.
KICAD_SOURCE_USERS = {
    "kipi_entry.py",
    "kipi/__main__.py",
}


def _modules():
    files = sorted(PACKAGE.rglob("*.py")) + [ROOT / "kipi_entry.py"]
    return [f for f in files if "__pycache__" not in f.parts]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _imports(path: Path):
    """Every module ``path`` imports, as absolute dotted names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[:len(package.split(".")) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            yield module
            for alias in node.names:     # ``from kipi.sources import kicad_ipc``
                yield f"{module}.{alias.name}"


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _violations(rule):
    found = []
    for path in _modules():
        rel = _rel(path)
        for module in _imports(path):
            if rule(rel, module):
                found.append(f"{rel} imports {module}")
    return sorted(set(found))


def test_the_scan_sees_imports():
    """Guard the guard: the scan finds the imports these rules are about."""
    seen = {m for p in _modules() for m in _imports(p)}
    assert "kipy" in seen or any(_under(m, "kipy") for m in seen)
    assert any(_under(m, "kipi.sources.kicad_ipc") for m in seen)


def test_only_the_kicad_source_imports_kipy():
    def rule(rel, module):
        return _under(module, "kipy") and not rel.startswith("kipi/sources/kicad_ipc/")
    assert _violations(rule) == []


@pytest.mark.parametrize("area", ["kipi/core/", "kipi/dc/", "kipi/ac/"])
def test_analysis_code_does_not_know_where_boards_come_from(area):
    def rule(rel, module):
        return rel.startswith(area) and _under(module, "kipi.sources")
    assert _violations(rule) == []


def test_core_stands_alone():
    """Shared code never reaches up into an analysis or the application shell."""
    def rule(rel, module):
        return rel.startswith("kipi/core/") and any(
            _under(module, p) for p in ("kipi.dc", "kipi.ac", "kipi.gui"))
    assert _violations(rule) == []


def test_only_entry_points_pick_the_kicad_source():
    def rule(rel, module):
        return (_under(module, "kipi.sources.kicad_ipc")
                and not rel.startswith("kipi/sources/")
                and rel not in KICAD_SOURCE_USERS)
    assert _violations(rule) == []


def test_the_gui_sees_only_source_interfaces():
    """The window knows sources by their interface, and opens files through the registry
    of formats; it never names a reader itself."""
    def rule(rel, module):
        return (rel.startswith("kipi/gui/") and _under(module, "kipi.sources")
                and module != "kipi.sources"
                and not any(_under(module, m) for m in ("kipi.sources.base",
                                                        "kipi.sources.registry")))
    assert _violations(rule) == []


def test_the_launcher_needs_only_the_standard_library():
    """The launcher runs before KiPI's dependencies are installed."""
    import sys
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        pytest.skip("needs Python 3.10's list of standard modules")

    def rule(rel, module):
        if not (rel.startswith("kipi/launcher/") or rel == "kipi/__init__.py"):
            return False
        top = module.split(".")[0]
        # kipi_entry: started by KiCad as its plugin, the launcher hands over to it. wx:
        # KiCad's own Python has it (no Tk on Windows); only ui_wx, tried and skipped (#7).
        return not (top in stdlib or module == "kipi" or _under(module, "kipi.launcher")
                    or module in ("kipi.__version__", "kipi_entry")
                    or (rel == "kipi/launcher/ui_wx.py" and top == "wx"))
    assert _violations(rule) == []
