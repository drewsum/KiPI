"""Copper temperature as the solvers read it.

A temperature is a callable mapping KiCad xy (an (N, 2) array, mm) to °C. One from an
electro-thermal solve (:mod:`kipi.dc.thermal`) also knows each copper layer: its
``on_layers(xy, layers)`` gives the temperature of each point on its own layer, and
calling it plainly gives the hottest layer at each point. A plain callable is the same on
every layer. :func:`at` reads either.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


def at(temperature, xy, layers=None) -> np.ndarray:
    """°C at points ``xy`` on copper layer ids ``layers`` (one per point, or one for all;
    None: the hottest layer at each point)."""
    xy = np.atleast_2d(np.asarray(xy, dtype=float))
    by_layer = getattr(temperature, "on_layers", None)
    if by_layer is None or layers is None:
        return np.asarray(temperature(xy), dtype=float)
    return np.asarray(by_layer(xy, layers), dtype=float)


def of_pads(temperature, pads) -> Optional[float]:
    """A part's temperature, °C: the mean of the copper's at its ``pads`` (board pads),
    each on its own copper layer (the hottest there, for a pad on several). None without
    a temperature, pads or a finite reading."""
    pads = list(pads)
    if temperature is None or not pads:
        return None
    layers = [next(iter(p.layers)) if len(p.layers) == 1 else -1 for p in pads]
    t = np.broadcast_to(at(temperature, [p.position for p in pads], np.array(layers)),
                        (len(pads),))
    t = t[np.isfinite(t)]
    return float(t.mean()) if t.size else None
