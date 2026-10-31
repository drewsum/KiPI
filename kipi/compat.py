"""Reading files written by earlier KiPI versions.

Results files before 0.8.0 are pickles, which name the module of every object they hold
(0.8.0 writes JSON and arrays instead, see :mod:`kipi.core.results_io`). KiPI 0.7.0
moved its modules into ``kipi.core`` (shared) and ``kipi.dc`` (DC analysis), and 0.8.0
moved the parts of ``kipi.core.kicad_io`` that are not KiCad-specific into the rest of
``kipi.core`` and the KiCad reader itself to ``kipi.sources.kicad_ipc``; the table below
maps the old module paths to the new ones so results saved before still open.
"""

from __future__ import annotations

import pickle
from typing import Tuple

# Old module path (or prefix) -> new one, most specific first.
_MOVED_MODULES = (
    ("kipi.core.kicad_io.snapshot", "kipi.core.model.snapshot_io"),
    ("kipi.kicad_io.snapshot", "kipi.core.model.snapshot_io"),
    ("kipi.core.kicad_io", "kipi.sources.kicad_ipc"),
    ("kipi.solver.direct", "kipi.core.solver.direct"),
    ("kipi.viz.results", "kipi.dc.viz.results"),
    ("kipi.viz", "kipi.core.viz"),
    ("kipi.gui.pi_panel", "kipi.dc.gui.pi_panel"),
    ("kipi.progress", "kipi.core.progress"),
    ("kipi.model", "kipi.core.model"),
    ("kipi.kicad_io", "kipi.sources.kicad_ipc"),
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


# What a results pickle may name besides KiPI's own classes (#99): NumPy arrays, the
# pyvista/VTK meshes and k-d trees of the 3D views, and a few builtin containers.
_LEGACY_GLOBALS = {
    ("builtins", n) for n in ("set", "frozenset", "complex", "list", "dict", "tuple", "slice",
                              "range", "bytearray", "bytes", "int", "float", "str", "bool")
} | {
    ("collections", "defaultdict"), ("collections", "OrderedDict"),
    ("copyreg", "_reconstructor"), ("numpy", "dtype"), ("numpy", "ndarray"),
    ("vtkmodules.util.pickle_support", "unserialize_VTK_data_object"),
} | {
    (f"numpy.{core}.{m}", n) for core in ("core", "_core")
    for m, n in (("multiarray", "_reconstruct"), ("multiarray", "scalar"),
                 ("numeric", "_frombuffer"))
}
# Packages whose classes (not functions) a results pickle may name.
_LEGACY_PACKAGES = ("kipi.core.", "kipi.dc.", "kipi.ac.", "kipi.noise.", "pyvista.",
                    "scipy.spatial.")
_PATHS = ("Path", "PosixPath", "WindowsPath", "PurePath", "PurePosixPath", "PureWindowsPath")


class Unpickler(pickle.Unpickler):
    """Reads the results pickles of KiPI 0.7 and earlier: finds classes at their current
    module paths, and refuses anything a results file does not hold, such as functions that
    could run commands. Loading a pickle is still not safe, only less unsafe; KiPI asks
    before it opens one."""

    def find_class(self, module, name):
        module, name = moved(module, name)
        # Python 3.13 and later pickle paths as pathlib._local.
        if module in ("pathlib", "pathlib._local") and name in _PATHS:
            from pathlib import Path        # a Windows path saved on Windows opens elsewhere
            return Path
        if (module, name) in _LEGACY_GLOBALS:
            return super().find_class(module, name)
        if module.startswith(_LEGACY_PACKAGES) and not name.startswith("_"):
            obj = super().find_class(module, name)
            if isinstance(obj, type):
                return obj
        raise pickle.UnpicklingError(f"Results file names {module}.{name}, which KiPI "
                                     "results do not hold; it was not opened")
