"""Reading files written by earlier KiPI versions.

Results files are pickles, which name the module of every object they hold. KiPI 0.7.0
moved its modules into ``kipi.core`` (shared) and ``kipi.dc`` (DC analysis); the table
below maps the old module paths to the new ones so results saved before still open.
"""

from __future__ import annotations

import pickle
from typing import Tuple

# Old module path (or prefix) -> new one, most specific first.
_MOVED_MODULES = (
    ("kipi.solver.direct", "kipi.core.solver.direct"),
    ("kipi.viz.results", "kipi.dc.viz.results"),
    ("kipi.viz", "kipi.core.viz"),
    ("kipi.gui.pi_panel", "kipi.dc.gui.pi_panel"),
    ("kipi.progress", "kipi.core.progress"),
    ("kipi.model", "kipi.core.model"),
    ("kipi.kicad_io", "kipi.core.kicad_io"),
    ("kipi.geometry", "kipi.core.geometry"),
    ("kipi.mesh", "kipi.core.mesh"),
    ("kipi.solver", "kipi.dc.solver"),
    ("kipi.pi", "kipi.dc.pi"),
    ("kipi.thermal", "kipi.dc.thermal"),
    ("kipi.report", "kipi.dc.report"),
)
# Classes that moved to another module or were renamed: (old module, old name) -> new.
_MOVED_CLASSES = {
    ("kipi.solver.fem", "_Factor"): ("kipi.core.solver.spd", "SpdSolver"),
}


def moved(module: str, name: str) -> Tuple[str, str]:
    """Where ``module.name`` of an earlier KiPI lives now (unchanged if it did not move)."""
    if (module, name) in _MOVED_CLASSES:
        return _MOVED_CLASSES[(module, name)]
    for old, new in _MOVED_MODULES:
        if module == old or module.startswith(old + "."):
            return new + module[len(old):], name
    return module, name


class Unpickler(pickle.Unpickler):
    """An unpickler that finds classes at their current module paths."""

    def find_class(self, module, name):
        return super().find_class(*moved(module, name))
