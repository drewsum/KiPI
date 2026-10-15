"""Edited stackups: the same layers as the board's, with other thicknesses and εr (#61).

The Stackup window lets a user try other copper and dielectric thicknesses, and other
dielectric constants. Each edit makes
a new :class:`Stackup` with the layers re-stacked from the bottom up, so every z, the
board thickness and everything the solvers and views read follow the new thicknesses.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple, Union

from kipi.core.model.board import CopperLayer, DielectricLayer, Stackup

Entry = Union[CopperLayer, DielectricLayer]


def stack_order(stackup: Stackup) -> List[Entry]:
    """Every copper and dielectric layer, top to bottom."""
    out: List[Entry] = [d for d in stackup.dielectrics if d.above_layer == -1]
    for cu in stackup.copper:
        out.append(cu)
        out += [d for d in stackup.dielectrics if d.above_layer == cu.id]
    return out


def thicknesses(stackup: Stackup) -> List[float]:
    """Each layer's thickness in mm, in :func:`stack_order`."""
    return [e.thickness_mm for e in stack_order(stackup)]


def dielectric_er(stackup: Stackup) -> List[float]:
    """Each dielectric's εr (0 where the stackup gives none), top to bottom."""
    return [e.epsilon_r for e in stack_order(stackup) if isinstance(e, DielectricLayer)]


def restack(stackup: Stackup, new_mm: Sequence[float], er: Optional[Sequence[float]] = None,
            edited: bool = True) -> Stackup:
    """``stackup`` with the layers' thicknesses set to ``new_mm`` (in :func:`stack_order`),
    restacked from z = 0 at the bottom, and the dielectrics' εr set to ``er`` (top to
    bottom) when given."""
    order = stack_order(stackup)
    if len(new_mm) != len(order):
        raise ValueError(f"{len(new_mm)} thicknesses for {len(order)} layers")
    ers = list(dielectric_er(stackup) if er is None else er)
    if len(ers) != len(order) - len(stackup.copper):
        raise ValueError(f"{len(ers)} εr values for {len(order) - len(stackup.copper)} "
                         "dielectrics")
    ers.reverse()
    z = sum(new_mm)
    copper: List[CopperLayer] = []
    dielectrics: List[DielectricLayer] = []
    for e, t in zip(order, new_mm):
        z -= t
        if isinstance(e, CopperLayer):
            copper.append(replace(e, thickness_mm=float(t), z_bottom_mm=z))
        else:
            dielectrics.append(replace(e, thickness_mm=float(t), epsilon_r=float(ers.pop())))
    return Stackup(copper=copper, dielectrics=dielectrics, board_thickness_mm=sum(new_mm),
                   from_board=stackup.from_board, edited=edited)


def to_setup(stackup: Stackup) -> Dict:
    """The stackup as kept in the setup file: copper thickness by layer name, dielectric
    thickness and εr in order, top to bottom."""
    return {"copper_mm": {c.name: c.thickness_mm for c in stackup.copper},
            "dielectric_mm": [d.thickness_mm for d in stack_order(stackup)
                              if isinstance(d, DielectricLayer)],
            "dielectric_er": dielectric_er(stackup)}


def from_setup(stackup: Stackup, saved: Optional[Dict]) -> Tuple[Optional[Stackup], str]:
    """``stackup`` with the thicknesses and εr ``saved`` by :func:`to_setup`, and "" — or
    None and the reason when they no longer fit the board (a layer added, removed or
    renamed). A setup saved without εr keeps the board's."""
    if not saved:
        return None, ""
    cu = saved.get("copper_mm") or {}
    diel = list(saved.get("dielectric_mm") or [])
    order = stack_order(stackup)
    names = [e.name for e in order if isinstance(e, CopperLayer)]
    n_diel = len(order) - len(names)
    if sorted(cu) != sorted(names) or len(diel) != n_diel:
        return None, (f"the board now has {len(names)} copper and {n_diel} dielectric "
                      f"layers; the setup's stackup has {len(cu)} and {len(diel)}")
    values = []
    it = iter(diel)
    for e in order:
        t = float(cu[e.name]) if isinstance(e, CopperLayer) else float(next(it))
        if not t > 0:
            return None, "a saved thickness is not above zero"
        values.append(t)
    er = saved.get("dielectric_er")
    if er is None or len(er) != n_diel:
        er = None
    return restack(stackup, values, [float(x) for x in er] if er is not None else None), ""


def summary(stackup: Stackup) -> str:
    """One line for reports: each layer's thickness, top to bottom, and the total."""
    parts = []
    for e in stack_order(stackup):
        if isinstance(e, CopperLayer):
            parts.append(f"{e.name} {e.thickness_mm * 1000:.4g} µm")
        else:
            er = f" εr {e.epsilon_r:g}" if e.epsilon_r > 0 else ""
            parts.append(f"{e.material or 'dielectric'} {e.thickness_mm * 1000:.4g} µm{er}")
    text = ", ".join(parts) + f"; {stackup.board_thickness_mm:.3f} mm in all"
    if stackup.edited:
        text += " (edited in KiPI)"
    elif not stackup.from_board:
        text += " (default stackup: KiCad gave none)"
    return text
