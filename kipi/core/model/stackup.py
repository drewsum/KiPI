"""Stackups: built from the layers a board file lists (or a default one when the file
does not give a complete stackup), and edited in KiPI.

The Stackup window lets a user try other copper and dielectric thicknesses, and other
dielectric constants (#61). Each edit makes a new :class:`Stackup` with the layers
re-stacked from the bottom up, so every z, the board thickness and everything the solvers
and views read follow the new thicknesses. The solder masks and silkscreens outside the
board (#82) can be made thicker or thinner too; they move nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple, Union

from kipi.core.model.board import (
    MASK, SILK, Coating, CopperLayer, DielectricLayer, Stackup,
)
from kipi.core.progress import NULL, ProgressReporter

DEFAULT_BOARD_THICKNESS_MM = 1.6
DEFAULT_OUTER_CU_MM = 0.035
DEFAULT_INNER_CU_MM = 0.0175
DEFAULT_MASK_MM = 0.01      # KiCad's default solder mask
DEFAULT_SILK_MM = 0.015     # a typical silkscreen ink; KiCad gives silkscreens no thickness

#: Said of a board whose own stackup could not be used (BoardSnapshot.assumed).
DEFAULT_STACKUP_NOTE = (f"Stackup: the board's own is incomplete, so a default "
                        f"{DEFAULT_BOARD_THICKNESS_MM} mm stackup is used (1 oz outer and "
                        "1/2 oz inner copper, FR4 between)")

# One stackup layer, top to bottom: ("cu", copper layer id, thickness_mm, "", 0.0) or
# ("diel", -1, thickness_mm, material, epsilon_r).
LayerSpec = Tuple[str, int, float, str, float]


def combined_epsilon_r(sublayers) -> float:
    """The εr of a dielectric made of ``sublayers`` ((thickness mm, εr) pairs), as one layer.

    The sublayers are stacked between the same two coppers, so they combine in series:
    t / Σ(tᵢ/εᵣᵢ). Sublayers with no εr (0) are left out; 0 when none has one. Without
    thicknesses, the first sublayer's εr is used.
    """
    known = [(t, er) for t, er in sublayers if er > 0]
    if not known:
        return 0.0
    total = sum(t for t, _ in known)
    if total <= 0:
        return known[0][1]
    return total / sum(t / er for t, er in known)


def default_entries(cu_ids: Sequence[int]) -> List[LayerSpec]:
    """A :data:`DEFAULT_BOARD_THICKNESS_MM` board: 1 oz outer and 1/2 oz inner copper,
    with the rest split evenly into FR4 dielectrics."""
    n = len(cu_ids)
    cu_t = [DEFAULT_OUTER_CU_MM if i in (0, n - 1) else DEFAULT_INNER_CU_MM for i in range(n)]
    diel_t = (DEFAULT_BOARD_THICKNESS_MM - sum(cu_t)) / max(1, n - 1)
    entries: List[LayerSpec] = []
    for i, lid in enumerate(cu_ids):
        entries.append(("cu", lid, cu_t[i], "", 0.0))
        if i < n - 1:
            entries.append(("diel", -1, diel_t, "FR4", 0.0))   # εr: AC Default εr
    return entries


def default_coatings() -> List[Coating]:
    """KiCad's default: a solder mask and a silkscreen on both sides."""
    return sort_coatings([Coating(kind, side, DEFAULT_MASK_MM if kind == MASK
                                  else DEFAULT_SILK_MM)
                          for side in ("top", "bottom") for kind in (MASK, SILK)])


def sort_coatings(coatings: Sequence[Coating]) -> List[Coating]:
    """``coatings`` top to bottom, as :attr:`Stackup.coatings` keeps them, one of each
    kind per side (the first given)."""
    out: List[Coating] = []
    for side, kinds in (("top", (SILK, MASK)), ("bottom", (MASK, SILK))):
        for kind in kinds:
            c = next((c for c in coatings if c.side == side and c.kind == kind), None)
            if c is not None:
                out.append(c)
    return out


# KiCad's named solder mask and silkscreen colors (its stackup editor's list).
_KICAD_COLORS = {
    "green": (20, 51, 36), "light green": (91, 168, 12), "saturated green": (13, 104, 11),
    "red": (181, 19, 21), "light red": (210, 40, 14), "red/orange": (239, 53, 41),
    "blue": (2, 59, 162), "light blue 1": (54, 79, 116), "light blue 2": (61, 85, 130),
    "green/blue": (21, 70, 80), "black": (11, 11, 11), "white": (245, 245, 245),
    "purple": (32, 2, 53), "light purple": (119, 31, 91), "yellow": (194, 195, 0),
}


def kicad_color(text: str) -> str:
    """A color as KiCad's stackup gives it (a name like "Green", or "#RRGGBBAA") as
    "#rrggbb"; "" when it names none."""
    t = (text or "").strip()
    if t.startswith("#") and len(t) >= 7:
        try:
            int(t[1:7], 16)
        except ValueError:
            return ""
        return "#" + t[1:7].lower()
    rgb = _KICAD_COLORS.get(t.lower())
    return "#%02x%02x%02x" % rgb if rgb else ""


def coating_of(layer_name: str, layer_type: str, thickness_mm: float = 0.0,
               material: str = "", color: str = "") -> Optional[Coating]:
    """The coating a KiCad stackup layer is, from its name ("F.Mask", "B.SilkS") or type
    ("Top Solder Mask"), or None for any other layer. A mask without a thickness gets
    KiCad's default; a silkscreen, which KiCad gives none, a typical ink's."""
    text = f"{layer_name} {layer_type}".lower()
    if "mask" in text and "paste" not in text:
        kind = MASK
    elif "silk" in text:
        kind = SILK
    else:
        return None
    name = layer_name.lower()
    side = ("bottom" if name.startswith("b.") or "bottom" in text
            else "top")
    if thickness_mm <= 0:
        thickness_mm = DEFAULT_MASK_MM if kind == MASK else DEFAULT_SILK_MM
    material = "" if material.lower() in ("not specified", "not_specified") else material
    return Coating(kind, side, thickness_mm, material, color)


def build_stackup(entries: Sequence[LayerSpec], cu_ids: Sequence[int], names: Dict[int, str],
                  progress: ProgressReporter = NULL,
                  assumed: Optional[List[str]] = None,
                  coatings: Optional[Sequence[Coating]] = None) -> Stackup:
    """The stackup of ``entries`` (top to bottom), or the default stackup for ``cu_ids``
    when ``entries`` does not hold every copper layer (said in ``assumed``). ``names`` maps
    layer id to the name shown for it. ``coatings`` are the solder masks and silkscreens
    the board gives; a default stackup has KiCad's default ones."""
    from_board = sum(1 for e in entries if e[0] == "cu") == len(cu_ids) and len(cu_ids) > 0
    if not from_board:
        progress.warn("Stackup incomplete; synthesizing a default "
                      f"{DEFAULT_BOARD_THICKNESS_MM} mm stackup")
        if assumed is not None:
            assumed.append(DEFAULT_STACKUP_NOTE)
        entries = default_entries(cu_ids)
        coatings = default_coatings()

    total = sum(e[2] for e in entries)
    z = total
    copper: List[CopperLayer] = []
    dielectrics: List[DielectricLayer] = []
    last_cu = -1
    pending_diel: List[DielectricLayer] = []
    for kind, lid, t, mat, er in entries:
        z -= t
        if kind == "cu":
            copper.append(CopperLayer(id=lid, name=names.get(lid, str(lid)), index=len(copper),
                                      thickness_mm=t, z_bottom_mm=z))
            for d in pending_diel:
                d.below_layer = lid
            pending_diel = []
            last_cu = lid
        else:
            d = DielectricLayer(thickness_mm=t, material=mat, epsilon_r=er, above_layer=last_cu)
            dielectrics.append(d)
            pending_diel.append(d)

    progress.info(f"Stackup: {len(copper)} copper layers, {total:.3f} mm total" +
                  ("" if from_board else " (default)"))
    for c in copper:
        progress.info(f"  {c.name:<8} t={c.thickness_mm * 1000:.1f} um  z={c.z_mid_mm:.3f} mm")
    return Stackup(copper=copper, dielectrics=dielectrics, board_thickness_mm=total,
                   from_board=from_board, coatings=sort_coatings(coatings or []))


# --- Edited stackups ---------------------------------------------------------------------

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


def coating_thicknesses(stackup: Stackup) -> List[float]:
    """Each solder mask's and silkscreen's thickness in mm, top to bottom."""
    return [c.thickness_mm for c in stackup.coatings]


def dielectric_er(stackup: Stackup) -> List[float]:
    """Each dielectric's εr (0 where the stackup gives none), top to bottom."""
    return [e.epsilon_r for e in stack_order(stackup) if isinstance(e, DielectricLayer)]


def dielectric_k(stackup: Stackup) -> List[float]:
    """The dielectrics' thermal conductivities set in KiPI (0: their material's), top to
    bottom."""
    return [e.k_w_mk for e in stack_order(stackup) if isinstance(e, DielectricLayer)]


def dielectric_materials(stackup: Stackup) -> List[str]:
    """Each dielectric's material name, top to bottom."""
    return [e.material for e in stack_order(stackup) if isinstance(e, DielectricLayer)]


def restack(stackup: Stackup, new_mm: Sequence[float], er: Optional[Sequence[float]] = None,
            edited: bool = True, materials: Optional[Sequence[str]] = None,
            k: Optional[Sequence[float]] = None,
            coating_mm: Optional[Sequence[float]] = None) -> Stackup:
    """``stackup`` with the layers' thicknesses set to ``new_mm`` (in :func:`stack_order`),
    restacked from z = 0 at the bottom, and the dielectrics' εr, material names and thermal
    conductivities set to ``er``, ``materials`` and ``k`` (top to bottom) when given, and
    the coatings' thicknesses to ``coating_mm``."""
    order = stack_order(stackup)
    if len(new_mm) != len(order):
        raise ValueError(f"{len(new_mm)} thicknesses for {len(order)} layers")
    n_diel = len(order) - len(stackup.copper)
    ers = list(dielectric_er(stackup) if er is None else er)
    mats = list(dielectric_materials(stackup) if materials is None else materials)
    ks = list(dielectric_k(stackup) if k is None else k)
    if len(ers) != n_diel or len(mats) != n_diel or len(ks) != n_diel:
        raise ValueError(f"{len(ers)} εr values, {len(mats)} materials and {len(ks)} k values "
                         f"for {n_diel} dielectrics")
    coat = list(coating_thicknesses(stackup) if coating_mm is None else coating_mm)
    if len(coat) != len(stackup.coatings):
        raise ValueError(f"{len(coat)} thicknesses for {len(stackup.coatings)} coatings")
    ers.reverse()
    mats.reverse()
    ks.reverse()
    z = sum(new_mm)
    copper: List[CopperLayer] = []
    dielectrics: List[DielectricLayer] = []
    for e, t in zip(order, new_mm):
        z -= t
        if isinstance(e, CopperLayer):
            copper.append(replace(e, thickness_mm=float(t), z_bottom_mm=z))
        else:
            dielectrics.append(replace(e, thickness_mm=float(t), epsilon_r=float(ers.pop()),
                                       material=str(mats.pop()), k_w_mk=float(ks.pop())))
    coatings = [replace(c, thickness_mm=float(t)) for c, t in zip(stackup.coatings, coat)]
    return Stackup(copper=copper, dielectrics=dielectrics, board_thickness_mm=sum(new_mm),
                   from_board=stackup.from_board, edited=edited, coatings=coatings)


def to_setup(stackup: Stackup) -> Dict:
    """The stackup as kept in the setup file: copper thickness by layer name, dielectric
    thickness, εr, material and thermal conductivity in order, top to bottom, and the
    coatings' thicknesses by name."""
    return {"copper_mm": {c.name: c.thickness_mm for c in stackup.copper},
            "dielectric_mm": [d.thickness_mm for d in stack_order(stackup)
                              if isinstance(d, DielectricLayer)],
            "dielectric_er": dielectric_er(stackup),
            "dielectric_material": dielectric_materials(stackup),
            "dielectric_k": dielectric_k(stackup),
            "coating_mm": {c.name: c.thickness_mm for c in stackup.coatings}}


def from_setup(stackup: Stackup, saved: Optional[Dict]) -> Tuple[Optional[Stackup], str]:
    """``stackup`` with the thicknesses, εr, materials and k ``saved`` by :func:`to_setup`,
    and "" — or None and the reason when they no longer fit the board (a layer added,
    removed or renamed). A setup saved without εr, materials, k or coatings keeps the
    board's, as does a coating the setup does not name."""
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
    er = [float(x) for x in er] if er is not None and len(er) == n_diel else None
    mats = saved.get("dielectric_material")
    mats = [str(m) for m in mats] if mats is not None and len(mats) == n_diel else None
    k = saved.get("dielectric_k")
    k = [float(x) for x in k] if k is not None and len(k) == n_diel else None
    coat = saved.get("coating_mm") or {}
    coat_mm = [float(coat.get(c.name, c.thickness_mm)) for c in stackup.coatings]
    if not all(t > 0 for t in coat_mm):
        return None, "a saved thickness is not above zero"
    return restack(stackup, values, er, materials=mats, k=k, coating_mm=coat_mm), ""


def with_dielectric_k(stackup: Stackup, k_w_mk: float) -> Stackup:
    """``stackup`` with every dielectric's thermal conductivity set to ``k_w_mk``: the
    board-wide Laminate k of setups from before 0.8.0 (#90)."""
    return restack(stackup, thicknesses(stackup), materials=dielectric_materials(stackup),
                   k=[k_w_mk] * len(dielectric_k(stackup)))


def summary(stackup: Stackup) -> str:
    """One line for reports: each layer's thickness, top to bottom, and the total."""
    parts = []
    for e in stack_order(stackup):
        if isinstance(e, CopperLayer):
            parts.append(f"{e.name} {e.thickness_mm * 1000:.4g} µm")
        else:
            er = f" εr {e.epsilon_r:g}" if e.epsilon_r > 0 else ""
            parts.append(f"{e.material or 'dielectric'} {e.thickness_mm * 1000:.4g} µm{er}")
    coat = [(c.side, f"{c.name.lower()} {c.thickness_mm * 1000:.4g} µm")
            for c in stackup.coatings]
    parts = ([t for side, t in coat if side == "top"] + parts
             + [t for side, t in coat if side == "bottom"])
    text = ", ".join(parts) + f"; {stackup.board_thickness_mm:.3f} mm in all"
    if stackup.edited:
        text += " (edited in KiPI)"
    elif not stackup.from_board:
        text += " (default stackup: KiCad gave none)"
    return text
