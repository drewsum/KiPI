"""The environment KiCad made for KiPI's plugin, and what KiPI still has to add to it (#7).

KiCad gives each IPC plugin a virtual environment, named after its identifier, in its
cache folder: ``<cache>/kicad/<version>/python-environments/com.github.drewsum.kipi``,
where the cache is ``%LOCALAPPDATA%`` (Windows), ``~/Library/Caches`` (macOS) or
``$XDG_CACHE_HOME`` (Linux, ``~/.cache``), and ``KICAD_CACHE_HOME`` takes its place (see
KiCad's ``PATHS::GetUserCachePath`` and ``PYTHON_MANAGER::GetPythonEnvironment``).

KiCad installs only ``requirements.txt`` (kicad-python) into it, on every plugin reload,
and never removes anything, so KiPI adds the rest of its packages
(``requirements-standalone.txt``) itself, the first time it starts:
:mod:`kipi.launcher.plugin_setup` in the plugin, the launcher standalone. A KiPI that
lives in KiCad's plugin folders (installed by the Plugin and Content Manager, or copied
into ``plugins``) uses this environment standalone too, rather than a second one.

An environment set up before KiPI did this itself (KiCad installed everything) has every
package but no setup record: :func:`adopt` records it, so it is never set up again.

Standard library only: it runs before KiPI's packages are there. ``python -m
kipi.launcher.kicad_env check <requirements>`` prints the requirements this Python does not
meet, as JSON, for the launcher to ask an environment it is not running in.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from kipi.launcher.paths import KIPI_ROOT, env_python
from kipi.launcher.state import State, load, save

IDENTIFIER = "com.github.drewsum.kipi"     # plugin.json's; PCM's folder replaces dots
PCM_FOLDER = IDENTIFIER.replace(".", "_")
ADOPTED = "kicad"                          # State.tool for an environment KiCad filled
_VERSION_DIR = re.compile(r"^\d+\.\d+$")


def kicad_version(root: Path = KIPI_ROOT) -> Optional[str]:
    """The KiCad version whose plugin folders hold this KiPI ("10.0"), or None when KiPI
    is somewhere else: ``KiCad/<version>/3rdparty/plugins/<PCM folder>`` (the Plugin and
    Content Manager) or ``KiCad/<version>/plugins/<folder>`` (a manual install)."""
    parts = Path(root).parts
    if len(parts) >= 4 and parts[-2] == "plugins" and parts[-3] == "3rdparty" \
            and _VERSION_DIR.match(parts[-4]):
        return parts[-4]
    if len(parts) >= 3 and parts[-2] == "plugins" and _VERSION_DIR.match(parts[-3]):
        return parts[-3]
    return None


def cache_dir(environ: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
              home: Optional[Path] = None) -> Path:
    """KiCad's cache folder, above the version folders."""
    env = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = home or Path.home()
    if env.get("KICAD_CACHE_HOME"):
        base = Path(env["KICAD_CACHE_HOME"])
    elif platform == "win32":
        base = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else \
            home / "AppData" / "Local"
    elif platform == "darwin":
        base = home / "Library" / "Caches"
    else:
        base = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache"
    return base / ("KiCad" if platform == "win32" else "kicad")


def plugin_env(version: str, **kw) -> Path:
    """KiPI's plugin environment for KiCad ``version`` (it may not exist yet)."""
    return cache_dir(**kw) / version / "python-environments" / IDENTIFIER


def base_python_missing(env: Path) -> Optional[str]:
    """Why ``env`` cannot run (its Python, or the one it was made from, is gone), or None.
    Uninstalling or moving KiCad takes the base Python of its environments with it."""
    if not env_python(env).is_file():
        return f"{env} has no Python"
    try:
        cfg = (env / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"{env} is not a virtual environment"
    for line in cfg.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home" and value.strip() and not Path(value.strip()).is_dir():
            return f"the Python it was made from ({value.strip()}) is gone"
    return None


def find(root: Path = KIPI_ROOT, **kw) -> Tuple[Optional[Path], str]:
    """KiCad's environment for this KiPI, if KiPI is in KiCad's plugin folders and KiCad
    has made it; else None and why not (for the log)."""
    version = kicad_version(root)
    if version is None:
        return None, "KiPI is not in a KiCad plugin folder"
    env = plugin_env(version, **kw)
    if not env.is_dir():
        return None, f"KiCad {version} has not made KiPI's plugin environment ({env})"
    problem = base_python_missing(env)
    if problem:
        return None, problem
    return env, ""


# --- What is missing ----------------------------------------------------------------------

def _version_tuple(text: str) -> Tuple[int, ...]:
    out = []
    for piece in text.split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        out.append(int(m.group()))
    return tuple(out)


def _parse(line: str) -> Tuple[str, str, str]:
    """``name>=1.2`` -> ("name", ">=", "1.2"); an operator other than >= or == is kept but
    not checked (any installed version passes)."""
    line = line.split(";", 1)[0].strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*([<>=!~]=?)?\s*([^,\s]*)", line)
    if not m:
        return line, "", ""
    return m.group(1), m.group(2) or "", m.group(3) or ""


def unmet(requirements: Path, version_of=None) -> List[str]:
    """The requirements this Python's packages do not meet (missing, or older than asked)."""
    from importlib import metadata
    if version_of is None:
        def version_of(name):
            try:
                return metadata.version(name)
            except metadata.PackageNotFoundError:
                return None
    from kipi.launcher.deps import requirement_lines
    out = []
    for line in requirement_lines(requirements):
        name, op, wanted = _parse(line)
        have = version_of(name)
        if have is None:
            out.append(line)
        elif op in (">=", "==") and wanted and _version_tuple(have) < _version_tuple(wanted):
            out.append(f"{line} (has {have})")
    return out


def unmet_in(env: Path, requirements: Path, timeout: float = 60.0) -> Optional[List[str]]:
    """:func:`unmet`, asked of ``env``'s own Python; None if it could not be asked."""
    from kipi.launcher.system import child_kwargs
    environ = dict(os.environ)
    environ["PYTHONPATH"] = str(KIPI_ROOT)
    environ.pop("PYTHONHOME", None)
    try:
        out = subprocess.run([str(env_python(env)), "-m", "kipi.launcher.kicad_env", "check",
                              str(requirements)], capture_output=True, text=True,
                             timeout=timeout, env=environ, **child_kwargs())
        return json.loads(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def adopt(env: Path, requirements_hash: str, python: str) -> State:
    """Record an environment that already has everything as set up (keeping its PARDISO
    state), so KiPI does not offer to install what is there."""
    from kipi import __version__
    previous = load(env)
    pardiso = previous.pardiso if previous else _has(env, "pypardiso")
    st = State(requirements_hash=requirements_hash, pardiso=pardiso, python=python,
               kipi_version=__version__, installed=time.strftime("%Y-%m-%dT%H:%M:%S"),
               tool=previous.tool if previous else ADOPTED)
    save(env, st)
    return st


def _has(env: Path, package: str) -> bool:
    """Whether ``package`` is installed in ``env`` (its dist-info folder is there)."""
    norm = package.lower().replace("-", "_")
    for site in list(env.glob("Lib/site-packages")) + list(env.glob("lib/python*/site-packages")):
        if any(p.name.lower().startswith(norm + "-") for p in site.glob("*.dist-info")):
            return True
    return False


def pip_environ(env: Path, base: Optional[Mapping[str, str]] = None) -> dict:
    """The environment KiCad runs the plugin's pip with: the venv named, and no
    PYTHONHOME or PYTHONPATH (KiCad's own Python sets them for its scripting)."""
    environ = dict(os.environ if base is None else base)
    environ.pop("PYTHONHOME", None)
    environ.pop("PYTHONPATH", None)
    environ["VIRTUAL_ENV"] = str(env)
    return environ


def main(argv: Sequence[str]) -> int:
    if len(argv) == 3 and argv[1] == "check":
        print(json.dumps(unmet(Path(argv[2]))))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
