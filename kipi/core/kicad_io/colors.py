"""Copper layer colors from the PCB editor's color theme.

KiCad's API does not expose the color theme, so it is read from KiCad's settings folder:
``pcbnew.json`` names the theme (``appearance.color_theme``) and ``colors/<theme>.json``
holds its colors. Anything missing (no settings folder, a built-in theme, a layer the theme
leaves out, a file that does not parse) falls back to KiCad's built-in default theme, so the
colors are always KiCad's even when the user's theme cannot be read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

RGB = Tuple[int, int, int]

#: KiCad's built-in default theme ("KiCad Default"), keyed like a theme file's
#: ``board.copper``: ``f``, ``in1`` .. ``in30``, ``b``.
_INNER_DEFAULT: List[RGB] = [
    (127, 200, 127), (206, 125, 44), (79, 203, 203), (219, 98, 139), (167, 165, 198),
    (40, 204, 217), (232, 178, 167), (242, 237, 161), (141, 203, 129), (237, 124, 51),
    (91, 195, 235), (247, 111, 142)]
DEFAULT_COPPER: Dict[str, RGB] = {"f": (200, 52, 52), "b": (77, 127, 196)}
DEFAULT_COPPER.update({f"in{i}": _INNER_DEFAULT[i - 1] for i in range(1, 13)})
# From In13 on, KiCad repeats In5..In12's seven colors after In12's.
_REPEAT = [(167, 165, 198), (40, 204, 217), (232, 178, 167), (242, 237, 161),
           (237, 124, 51), (91, 195, 235), (247, 111, 142)]
DEFAULT_COPPER.update({f"in{i}": _REPEAT[(i - 13) % 7] for i in range(13, 31)})

_BUILTIN = ("", "_builtin_default", "_builtin_classic")
_COLOR = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)")

# KiCad's BoardLayer enum (the API's): F.Cu, In1.Cu .. In30.Cu, B.Cu.
_BL_F_CU, _BL_IN1_CU, _BL_IN30_CU = 3, 4, 33


def layer_key(layer_id: int, index: int, n_layers: int) -> str:
    """The theme key of a copper layer: ``f``, ``b`` or ``in<n>``.

    The outer layers go by their place in the stackup; inner layers by their KiCad layer
    (In3.Cu is ``in3`` even on a board that skips In2.Cu), else by their place."""
    if index == 0:
        return "f"
    if index == n_layers - 1:
        return "b"
    if _BL_IN1_CU <= layer_id <= _BL_IN30_CU:
        return f"in{layer_id - _BL_F_CU}"
    return f"in{index}"


def parse_color(text) -> Optional[RGB]:
    m = _COLOR.match(str(text).strip())
    if m is None:
        return None
    return tuple(min(255, max(0, int(round(float(v))))) for v in m.groups())


def _config_roots() -> List[Path]:
    """KiCad's settings folders (``kicad/`` holding a folder per version), most likely
    first. ``KICAD_CONFIG_HOME`` is KiCad's own override."""
    roots: List[Path] = []
    env = os.environ.get("KICAD_CONFIG_HOME")
    if env:
        roots.append(Path(env))
    home = Path.home()
    if sys.platform == "win32":
        if os.environ.get("APPDATA"):
            roots.append(Path(os.environ["APPDATA"]) / "kicad")
    elif sys.platform == "darwin":
        roots.append(home / "Library" / "Preferences" / "kicad")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        roots.append((Path(xdg) if xdg else home / ".config") / "kicad")
        roots.append(home / ".var" / "app" / "org.kicad.KiCad" / "config" / "kicad")
    return roots


def _version_key(name: str):
    try:
        return tuple(int(p) for p in name.split("."))
    except ValueError:
        return (-1,)


def settings_dirs(kicad_version: str = "") -> List[Path]:
    """Existing per-version settings folders with a ``pcbnew.json``: the running KiCad's
    version first, then the newest."""
    m = re.match(r"\D*(\d+)\.(\d+)", kicad_version or "")
    wanted = [f"{m.group(1)}.{m.group(2)}", f"{m.group(1)}.0"] if m else []
    found = []
    for rank, root in enumerate(_config_roots()):
        try:
            found += [(rank, d) for d in root.iterdir() if (d / "pcbnew.json").is_file()]
        except OSError:
            continue

    def order(item):
        rank, d = item
        mine = wanted.index(d.name) if d.name in wanted else len(wanted)
        return (mine, tuple(-x for x in _version_key(d.name)), rank)

    return [d for _, d in sorted(found, key=order)]


def _theme_file(folder: Path, name: str) -> Optional[Path]:
    colors = folder / "colors"
    direct = colors / f"{name}.json"
    if direct.is_file():
        return direct
    try:
        for f in sorted(colors.glob("*.json")):
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("meta", {}).get("name") == name:
                    return f
            except (OSError, ValueError, AttributeError):
                continue
    except OSError:
        pass
    return None


def copper_theme(kicad_version: str = "") -> Tuple[Dict[str, RGB], str]:
    """The copper colors of the PCB editor's theme, and where they came from.

    Never raises: anything that cannot be read leaves KiCad's default colors."""
    colors = dict(DEFAULT_COPPER)
    try:
        for folder in settings_dirs(kicad_version):
            try:
                settings = json.loads((folder / "pcbnew.json").read_text(encoding="utf-8"))
                name = (settings.get("appearance") or {}).get("color_theme") or ""
            except (OSError, ValueError, AttributeError):
                continue
            if name in _BUILTIN:
                return colors, "KiCad default theme"
            path = _theme_file(folder, name)
            if path is None:
                return colors, f"KiCad default theme (theme {name!r} not found)"
            theme = json.loads(path.read_text(encoding="utf-8"))
            copper = (theme.get("board") or {}).get("copper") or {}
            for key, text in copper.items():
                rgb = parse_color(text)
                if rgb is not None and key in colors:
                    colors[key] = rgb
            return colors, f"KiCad theme {name!r}"
    except Exception:  # noqa: BLE001 - colors must never stop a board loading
        log.warning("Could not read KiCad's color theme", exc_info=True)
        return dict(DEFAULT_COPPER), "KiCad default theme (theme unreadable)"
    return colors, "KiCad default theme (no KiCad settings found)"
