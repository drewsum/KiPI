"""Default copper layer colors, and the key each copper layer's color is stored under;
and the colors of solder masks and silkscreens.

Colors are keyed like a KiCad color theme's ``board.copper`` (``f``, ``in1`` .. ``in30``,
``b``), so a KiCad theme can replace any of them; boards from any source use the same
keys, by their :mod:`~kipi.core.model.layers` ids.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from kipi.core.model import layers

RGB = Tuple[int, int, int]

#: KiCad's built-in default theme ("KiCad Default").
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


def layer_key(layer_id: int, index: int, n_layers: int) -> str:
    """The color key of a copper layer: ``f``, ``b`` or ``in<n>``.

    The outer layers go by their place in the stackup; inner layers by their layer id
    (In3.Cu is ``in3`` even on a board that skips In2.Cu), else by their place."""
    if index == 0:
        return "f"
    if index == n_layers - 1:
        return "b"
    if layers.is_inner(layer_id):
        return f"in{layer_id - layers.F_CU}"
    return f"in{index}"


DEFAULT_MASK: RGB = (20, 81, 36)      # a green solder mask
DEFAULT_SILK: RGB = (240, 240, 240)


def coating_rgb(coating) -> Tuple[float, float, float]:
    """A solder mask's or silkscreen's color, 0..1: the board's, else green or white."""
    from kipi.core.model.board import MASK
    c = coating.color or ""
    if c.startswith("#") and len(c) == 7:
        rgb = tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    else:
        rgb = DEFAULT_MASK if coating.kind == MASK else DEFAULT_SILK
    return tuple(v / 255 for v in rgb)
