"""Copper temperature as the solvers read it.

A temperature is a callable mapping KiCad xy (an (N, 2) array, mm) to °C. One from an
electro-thermal solve (:mod:`kipi.dc.thermal`) also knows each copper layer: its
``on_layers(xy, layers)`` gives the temperature of each point on its own layer, and
calling it plainly gives the hottest layer at each point. A plain callable is the same on
every layer. :func:`at` reads either.
"""

from __future__ import annotations

import numpy as np


def at(temperature, xy, layers=None) -> np.ndarray:
    """°C at points ``xy`` on copper layer ids ``layers`` (one per point, or one for all;
    None: the hottest layer at each point)."""
    xy = np.atleast_2d(np.asarray(xy, dtype=float))
    by_layer = getattr(temperature, "on_layers", None)
    if by_layer is None or layers is None:
        return np.asarray(temperature(xy), dtype=float)
    return np.asarray(by_layer(xy, layers), dtype=float)
