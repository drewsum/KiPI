"""Parts' 3D models, read through KiCad's own command-line tool.

A footprint's models are files named with KiCad path variables
(``${KICAD10_3DMODEL_DIR}/...``), relative to the project, or embedded in the board, and
KiCad 10's library ships them as STEP only, which VTK cannot read. ``kicad-cli pcb export
glb`` resolves all of that the way KiCad's own 3D viewer does, places each model on its
footprint and writes triangles. KiPI asks it for just the parts it wants, without the
board, and reads the result with :func:`kipi.core.viz.glb.read_glb`.

kicad-cli reads the board file on disk, so a part moved since the board was last saved
is drawn where it was saved. Everything here is best effort: with no kicad-cli, no board
file, or a model KiCad cannot find, the parts are simply drawn without models.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter

# Set to the kicad-cli program to use, when KiPI cannot find it.
CLI_ENV = "KIPI_KICAD_CLI"
TIMEOUT_S = 300.0
FLATPAK_ID = "org.kicad.KiCad"
_FILTER = object()          # stands for "--component-filter <refs>" in the option sets
# Tried in order: an older kicad-cli that does not know an option is asked without it.
# Without the filter, every part is exported and the wanted ones are picked out.
_OPTION_SETS = (("--no-board-body", "--user-origin", "0x0mm", _FILTER),
                ("--no-board-body", _FILTER),
                ("--no-board-body",))
_MISSING_RE = re.compile(r"Could not add 3D model for (\S+?)\.?\s*\r?\n\s*([^\r\n]*)")


class ModelError(RuntimeError):
    pass


# --- Finding kicad-cli -----------------------------------------------------------------

def _version_key(path: Path):
    return [int(p) if p.isdigit() else -1 for p in re.split(r"[.\-]", path.name)]


def _installed_locations() -> List[Path]:
    """Where KiCad's installers put kicad-cli, newest version first."""
    if sys.platform == "win32":
        roots = {os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432")}
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.add(str(Path(local) / "Programs"))
        found = []
        for root in filter(None, roots):
            versions = sorted((d for d in Path(root, "KiCad").glob("*") if d.is_dir()),
                              key=_version_key, reverse=True)
            found += [d / "bin" / "kicad-cli.exe" for d in versions]
        return found
    if sys.platform == "darwin":
        apps = sorted(Path("/Applications").glob("KiCad*/KiCad.app"), reverse=True)
        return [a / "Contents" / "MacOS" / "kicad-cli" for a in apps]
    return [Path(p) for p in ("/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
                              "/snap/bin/kicad.kicad-cli")]


def find_kicad_cli(kicad=None) -> Optional[List[str]]:
    """The command that runs kicad-cli, or None if there is none.

    Looks, in order, at :data:`CLI_ENV`, the KiCad KiPI is connected to (``kicad``), the
    PATH, the usual install folders, and KiCad's Flatpak.
    """
    candidates = [os.environ.get(CLI_ENV, "").strip()]
    if kicad is not None:
        try:
            candidates.append(kicad.get_kicad_binary_path("kicad-cli"))
        except Exception:  # noqa: BLE001 - KiCad before 9 cannot say; look elsewhere
            pass
    candidates.append(shutil.which("kicad-cli") or "")
    candidates += [str(p) for p in _installed_locations()]
    for c in candidates:
        if c and Path(c).is_file():
            return [c]
    flatpak = shutil.which("flatpak")
    if flatpak and (Path.home() / ".var" / "app" / FLATPAK_ID).is_dir():
        return [flatpak, "run", "--command=kicad-cli", FLATPAK_ID]
    return None


def board_file(snap: BoardSnapshot) -> Optional[Path]:
    """The board's .kicad_pcb file, if it can be found on disk."""
    name = Path(snap.board_name or "").name
    if not name.endswith(".kicad_pcb"):
        return None
    dirs = [snap.project_dir]
    if snap.file:
        dirs.append(str(Path(snap.file).parent))       # a snapshot saved next to its board
    for d in filter(None, dirs):
        path = Path(d) / name
        if path.is_file():
            return path
    return None


# --- Exporting ---------------------------------------------------------------------------

def _run(cmd: List[str], progress: ProgressReporter, timeout_s: float):
    """``(exit code, output)``; stops kicad-cli if the task is canceled."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW: no console flash
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **kwargs)
    waited = 0.0
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                waited += 0.2
                progress.check_cancel()
                if waited > timeout_s:
                    raise ModelError(f"kicad-cli took longer than {timeout_s:.0f} s") from None
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    return proc.returncode, out.decode("utf-8", "replace")


def _plain_ref(ref: str) -> bool:
    """A reference kicad-cli's filter takes literally (it splits on commas, and * and ?
    are wildcards)."""
    return bool(ref) and not any(c in ref for c in ",*? \t")


def export_glb(cli: List[str], board: Path, refs: List[str], out: Path,
               progress: ProgressReporter = NULL, timeout_s: float = TIMEOUT_S) -> str:
    """Export the models of ``refs`` to ``out``; returns what kicad-cli printed."""
    text = ""
    for options in _OPTION_SETS:
        args: List[str] = []
        for opt in options:
            args += ["--component-filter", ",".join(refs)] if opt is _FILTER else [opt]
        code, text = _run([*cli, "pcb", "export", "glb", *args, "-f", "-o", str(out),
                           str(board)], progress, timeout_s)
        if code == 0 and out.is_file():
            return text
        if "unknown argument" not in text.lower():
            break
    raise ModelError(f"kicad-cli could not export the 3D models: {text.strip()[-400:]}")


def missing_models(output: str) -> Dict[str, str]:
    """Parts kicad-cli could not add a model for, with its reason ("File not found: ...")."""
    return {ref: reason.strip() for ref, reason in _MISSING_RE.findall(output)}


# --- Meshes --------------------------------------------------------------------------------

def to_mesh(part):
    """A :class:`~kipi.core.viz.glb.Part` as a mesh in the 3D view's frame.

    glTF has Y up in meters, and KiCad writes board X along X and board Y along Z. The view
    uses millimeters with board Y flipped so Z is up: ``(x, -y, z)``. Each triangle keeps
    its color in the ``rgb`` cell array.
    """
    import pyvista as pv
    pts = part.points[:, [0, 2, 1]] * np.array([1000.0, -1000.0, 1000.0])
    faces = np.empty((len(part.tris), 4), dtype=np.int64)
    faces[:, 0] = 3
    faces[:, 1:] = part.tris
    mesh = pv.PolyData(pts, faces.ravel())
    mesh.cell_data["rgb"] = part.rgb
    if part.normals is not None:
        mesh.point_data["Normals"] = part.normals[:, [0, 2, 1]] * np.array([1.0, -1.0, 1.0])
        mesh.point_data.active_normals_name = "Normals"
    return mesh


def _out_dir(cli: List[str]) -> Optional[str]:
    """Where kicad-cli writes: the temp folder, except for a Flatpak KiCad, which has a
    /tmp of its own; it writes in the user's cache folder, which both can see."""
    if FLATPAK_ID in cli:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kipi"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            return str(cache)
        except OSError:
            pass
    return None


# --- A smaller board for kicad-cli (#212) ------------------------------------------------

#: Boards smaller than this are exported as they are: kicad-cli reads them in well under a
#: second.
SLIM_MIN_BYTES = 4 * 2**20
#: Top-level board items the model export doesn't need: the tracks, vias and zones are most
#: of a large board's file, and kicad-cli reads them all before exporting a single part.
#: Groups and generated items (tuning patterns) refer to tracks.
SLIM_DROP = ("segment", "arc", "via", "zone", "group", "generated")
# A top-level item starts a line at one level of indentation: a tab, or before KiCad 7 two
# spaces. Items inside footprints are indented further.
_TOP_ITEM = re.compile(r"^(?:\t|  )\(([a-z_]+)", re.M)
_MODEL_PATH = re.compile(r'\(model\s+"((?:[^"\\]|\\.)*)"')


def slim_board(board: Path, folder: Path) -> Optional[Path]:
    """A copy of ``board`` in ``folder`` without its :data:`SLIM_DROP` items, which
    kicad-cli exports parts' models from in a fraction of the time; None to export from
    ``board`` itself (a small board, a layout this does not know, or model paths the copy
    could not resolve as the board does)."""
    try:
        if board.stat().st_size < SLIM_MIN_BYTES:
            return None
        with open(board, encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    end = text.rfind("\n)")                  # the board's closing parenthesis
    if not text.startswith("(kicad_pcb") or end < 0:
        return None
    starts = [(m.start(), m.group(1)) for m in _TOP_ITEM.finditer(text, 0, end)]
    parts, at = [], 0
    for k, (start, kind) in enumerate(starts):
        if kind in SLIM_DROP:
            parts.append(text[at:start])
            at = starts[k + 1][0] if k + 1 < len(starts) else end + 1
    if not parts:
        return None
    parts.append(text[at:])
    slim = "".join(parts)
    # Model paths name the project's folder as ${KIPRJMOD}, the copy's folder now, so it
    # is written out. A bare relative path is looked for from the project's folder too:
    # then the board is used as it is.
    for path in _MODEL_PATH.findall(slim):
        if not (path.startswith(("${", "$(", "/", "\\", "kicad-embed://"))
                or re.match(r"[A-Za-z]:[/\\]", path)):
            return None
    project = board.parent.resolve().as_posix()
    slim = slim.replace("${KIPRJMOD}", project).replace("$(KIPRJMOD)", project)
    out = folder / board.name
    try:
        with open(out, "w", encoding="utf-8", newline="") as f:
            f.write(slim)
        pro = board.with_suffix(".kicad_pro")       # the project's text variables
        if pro.is_file():
            shutil.copyfile(pro, out.with_suffix(".kicad_pro"))
    except OSError:
        return None
    return out


def _export_and_read(cli, board, refs, progress):
    from kipi.core.viz.glb import read_glb
    with tempfile.TemporaryDirectory(prefix="kipi-models-", dir=_out_dir(cli)) as tmp:
        out = Path(tmp) / "models.glb"
        slim = slim_board(board, Path(tmp))
        text = None
        if slim is not None:
            try:
                text = export_glb(cli, slim, refs, out, progress)
            except ModelError as e:
                progress.debug(f"3D models: the board without its tracks and zones did "
                               f"not export ({e}); exporting from the board itself")
        if text is None:
            text = export_glb(cli, board, refs, out, progress)
        return read_glb(out.read_bytes(), names=refs), text


def load_models(cli: List[str], board: Path, refs: Iterable[str],
                progress: ProgressReporter = NULL) -> Dict[str, Optional[object]]:
    """Each of ``refs`` with its model mesh (see :func:`to_mesh`), or None when it has no
    model KiCad can find."""
    refs = sorted(set(refs))
    plain = [r for r in refs if _plain_ref(r)]
    result: Dict[str, Optional[object]] = {r: None for r in refs}
    if not plain:
        return result
    parts, text = _export_and_read(cli, board, plain, progress)
    if parts.keys() <= {""} and parts and len(plain) > 1:
        # The parts came back unnamed (a kicad-cli that names nodes some other way):
        # ask for them one at a time instead.
        for ref in plain:
            progress.check_cancel()
            one, more = _export_and_read(cli, board, [ref], progress)
            if one:
                parts[ref] = next(iter(one.values()))
            text += more
    elif len(plain) == 1 and plain[0] not in parts and "" in parts:
        parts[plain[0]] = parts[""]
    for ref in plain:
        if ref in parts:
            result[ref] = to_mesh(parts[ref])
    missing = missing_models(text)
    for ref, reason in sorted(missing.items()):
        if ref in result:
            progress.info(f"No 3D model for {ref}: {reason}")
    return result
