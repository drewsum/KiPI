"""Parts' 3D models guessed from KiCad's model library, for boards with no KiCad board
file to read them from (an ODB++ job, a snapshot whose board is not beside it), or parts
whose models KiCad cannot find (#130). Only when the user asks: Guess models.

Each part's footprint (or ODB++ package) name is looked up in the library KiCad installs
(``Capacitor_SMD.3dshapes/C_0402_1005Metric.step``): by name, by a name the part's ends
with (ODB++ from KiCad names parts ``Capacitor_SMD_C_0402_1005Metric``), by the name it
starts with (a ``_HandSolder`` variant), as a chip resistor, capacitor or inductor by its
size code (``CAPC1005X55N``, ``R0603``), and last by the closest name. KiPI then writes a
board holding just those parts, each with its model, where it sits and turned as it is,
and has kicad-cli export it like any other (:mod:`kipi.sources.kicad_ipc.models3d`). A
model not found by the whole name is dropped if it is far larger or smaller than the
part's pads, and the log names each one kept. Parts with no likely model are drawn
without one.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from kipi.core.model.board import BoardSnapshot, Footprint
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.base import ModelProvider
from kipi.sources.kicad_ipc import models3d

MODEL_EXTS = (".step", ".stp", ".wrl")
MIN_SUFFIX = 6              # shortest library name a part's name may end with
CLOSE_CUTOFF = 0.8          # difflib ratio for the closest name
# A guessed model is dropped if its outline's diagonal is more than SIZE_MAX times the
# pads' plus SIZE_SLACK_MM, or less than SIZE_MIN times the pads'.
SIZE_MAX, SIZE_SLACK_MM, SIZE_MIN = 2.0, 1.0, 0.3
# Chip sizes: imperial code -> metric code, as KiCad names them (C_0402_1005Metric).
CHIP_SIZES = {"0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012",
              "1206": "3216", "1210": "3225", "1812": "4532", "2010": "5025", "2512": "6332"}
_METRIC = {m: i for i, m in CHIP_SIZES.items()}
# IPC-7351 names give the metric size (CAPC1005X55N); other names the imperial (C0402).
_CHIP_RE = re.compile(r"^(?:(CAPC|RESC|INDC)|(CAP|RES|IND|C|R|L))[A-Z]*?[_\-]?(\d{4})(?!\d)",
                      re.IGNORECASE)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


# --- The library ---------------------------------------------------------------------

def library_dirs(cli: Optional[List[str]]) -> List[Path]:
    """Where KiCad's 3D model library may be: its path variables, then beside the
    kicad-cli KiPI uses, then the usual install folders. Only folders that exist."""
    found: List[Path] = []
    names = sorted((k for k in os.environ if re.fullmatch(r"KICAD\d*_3DMODEL_DIR", k)),
                   key=lambda k: int(re.sub(r"\D", "", k) or 0), reverse=True)
    found += [Path(os.environ[k]) for k in names if os.environ[k].strip()]
    if cli and models3d.FLATPAK_ID in cli:
        for root in (Path.home() / ".local/share/flatpak", Path("/var/lib/flatpak")):
            found.append(root / "app" / models3d.FLATPAK_ID / "current/active/files"
                         / "share/kicad/3dmodels")
    elif cli:
        exe = Path(cli[0])
        found.append(exe.parent.parent / "share" / "kicad" / "3dmodels")   # Windows, Linux
        found.append(exe.parent.parent / "SharedSupport" / "3dmodels")     # macOS
    found += [Path(p) for p in ("/usr/share/kicad/3dmodels", "/usr/local/share/kicad/3dmodels",
                                "/snap/kicad/current/usr/share/kicad/3dmodels")]
    out = []
    for d in found:
        try:
            if d.is_dir() and d not in out:
                out.append(d)
        except OSError:
            pass
    return out


@dataclass
class Library:
    """The model files of one library folder, by normalized name (see :func:`_norm`)."""
    root: Path
    models: Dict[str, Path]

    @classmethod
    def read(cls, root: Path) -> "Library":
        models: Dict[str, Path] = {}
        # STEP first: KiCad 10 ships only STEP, older libraries both, and the same name.
        for ext in MODEL_EXTS:
            for f in sorted(root.glob(f"*.3dshapes/*{ext}")):
                models.setdefault(_norm(f.stem), f)
        return cls(root, models)

    def match(self, name: str) -> Tuple[Optional[Path], str]:
        """The model for a footprint or package ``name``, and how it was found: "name",
        "start of name", "size code" or "closest name" ("" when there is none)."""
        name = name.split(":")[-1].strip()      # "Capacitor_SMD:C_0402_1005Metric"
        key = _norm(name)
        if not key:
            return None, ""
        if key in self.models:
            return self.models[key], "name"
        for i in range(1, len(key) - MIN_SUFFIX + 1):
            if key[i:] in self.models:
                return self.models[key[i:]], "name"
        # KiCad's variants of a footprint (_HandSolder, _ThermalVias) use its model
        for i in range(len(key) - 1, MIN_SUFFIX - 1, -1):
            if key[:i] in self.models:
                return self.models[key[:i]], "start of name"
        chip = _chip_model(name)
        if chip and _norm(chip) in self.models:
            return self.models[_norm(chip)], "size code"
        close = difflib.get_close_matches(key, self.models.keys(), n=1, cutoff=CLOSE_CUTOFF)
        if close:
            return self.models[close[0]], "closest name"
        return None, ""


def _chip_model(name: str) -> Optional[str]:
    """KiCad's model name for a chip resistor, capacitor or inductor named by its size."""
    m = _CHIP_RE.match(name)
    if not m:
        return None
    kind = (m.group(1) or m.group(2))[0].upper()
    code = m.group(3)
    if m.group(1):
        imperial, metric = _METRIC.get(code), code
    else:
        imperial, metric = code, CHIP_SIZES.get(code)
    if imperial is None or metric is None:
        return None
    return f"{kind}_{imperial}_{metric}Metric"


# --- The board kicad-cli exports -------------------------------------------------------

def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def footprint_angle(fp: Footprint, pads_by_id) -> float:
    """The footprint's rotation; for snapshots that did not record it, its pads' most
    common one (a footprint's pads are mostly unturned in it)."""
    if fp.angle_deg is not None:
        return float(fp.angle_deg)
    angles = [round(pads_by_id[i].angle_deg, 3) for i in fp.pad_ids
              if i in pads_by_id and pads_by_id[i].angle_deg is not None]
    return float(Counter(angles).most_common(1)[0][0]) if angles else 0.0


def board_text(parts: List[Tuple[Footprint, float, str]], thickness_mm: float) -> str:
    """A KiCad board holding ``parts`` (footprint, rotation, model path) and nothing else."""
    lines = ["(kicad_pcb", "\t(version 20240108)", '\t(generator "kipi")',
             f"\t(general (thickness {thickness_mm:.4f}))",
             '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal))']
    for fp, angle, model in parts:
        layer = "B.Cu" if fp.side == "bottom" else "F.Cu"
        x, y = fp.position
        lines += [f"\t(footprint {_quote(fp.reference)}",
                  f'\t\t(layer "{layer}")',
                  f"\t\t(at {x:.4f} {y:.4f} {angle:.4f})",
                  f'\t\t(property "Reference" {_quote(fp.reference)} (at 0 0 0) '
                  f'(layer "{layer}"))',
                  f"\t\t(model {_quote(model)})",
                  "\t)"]
    lines.append(")")
    return "\n".join(lines) + "\n"


def _cli_path(path: Path, lib: Library, cli: List[str]) -> str:
    """``path`` as kicad-cli sees it: a Flatpak KiCad has its library under /app."""
    if models3d.FLATPAK_ID in cli:
        return "/app/share/kicad/3dmodels/" + path.relative_to(lib.root).as_posix()
    return path.as_posix()


def _pads_diagonal(fp: Footprint, pads_by_id) -> Optional[float]:
    pts = [p for i in fp.pad_ids if i in pads_by_id
           for polys in pads_by_id[i].shapes.values() for poly in polys for p in poly.outline]
    pts += [pads_by_id[i].position for i in fp.pad_ids if i in pads_by_id]
    if not pts:
        return None
    a = np.asarray(pts, dtype=float)
    return float(np.hypot(*(a.max(axis=0) - a.min(axis=0))))


def _fits(mesh, fp: Footprint, pads_by_id) -> bool:
    """Whether a guessed model is about the size of the part's pads."""
    pads = _pads_diagonal(fp, pads_by_id)
    if not pads:
        return True
    b = mesh.bounds
    model = float(np.hypot(b[1] - b[0], b[3] - b[2]))
    return SIZE_MIN * pads <= model <= SIZE_MAX * pads + SIZE_SLACK_MM


class GuessedModels(ModelProvider):
    """Part models picked from KiCad's library by footprint name, placed by kicad-cli."""
    label = "kicad-cli (models guessed by footprint name)"

    def __init__(self, cli: List[str], library: Library, snap: BoardSnapshot):
        self.cli = cli
        self.library = library
        self.snap = snap

    def load(self, refs: Iterable[str],
             progress: ProgressReporter = NULL) -> Dict[str, Optional[object]]:
        refs = sorted(set(refs))
        result: Dict[str, Optional[object]] = {r: None for r in refs}
        fps = {f.reference: f for f in self.snap.footprints}
        pads_by_id = {p.id: p for p in self.snap.pads}
        parts, how, names = [], {}, {}
        matched: Dict[str, Tuple[Optional[Path], str]] = {}
        for ref in refs:
            fp = fps.get(ref)
            if fp is None or not fp.footprint or not models3d._plain_ref(ref):
                continue
            if fp.footprint not in matched:
                matched[fp.footprint] = self.library.match(fp.footprint)
            path, why = matched[fp.footprint]
            if path is None:
                continue
            parts.append((fp, footprint_angle(fp, pads_by_id),
                          _cli_path(path, self.library, self.cli)))
            how[ref], names[ref] = why, path.stem
        if not parts:
            progress.info("3D models: no part's footprint name is in KiCad's model library")
            return result
        with tempfile.TemporaryDirectory(prefix="kipi-guess-",
                                         dir=models3d._out_dir(self.cli)) as tmp:
            board = Path(tmp) / "parts.kicad_pcb"
            board.write_text(board_text(parts, self.snap.stackup.board_thickness_mm),
                             encoding="utf-8")
            found = models3d.load_models(self.cli, board, [fp.reference for fp, _, _ in parts],
                                         progress)
        for ref, mesh in found.items():
            if mesh is None:
                continue
            if how[ref] != "name":
                fp = fps[ref]
                if not _fits(mesh, fp, pads_by_id):
                    progress.info(f"3D model for {ref} ({fp.footprint}): {names[ref]}, "
                                  f"guessed by {how[ref]}, does not fit its pads; left out")
                    continue
                progress.info(f"3D model for {ref} ({fp.footprint}): guessed {names[ref]} "
                              f"by {how[ref]}")
            result[ref] = mesh
        return result


def guessed_models(cli: List[str], snap: BoardSnapshot) -> Optional[GuessedModels]:
    """A provider guessing ``snap``'s models from KiCad's library, or None if KiPI cannot
    find the library."""
    for root in library_dirs(cli):
        lib = Library.read(root)
        if lib.models:
            return GuessedModels(cli, lib, snap)
    return None
