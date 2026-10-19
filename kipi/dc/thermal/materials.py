"""PCB dielectric materials: in-plane thermal conductivity and a typical εr, looked up
from the material name in the KiCad stackup, and the list the Stackup window offers.

The εr values are typical datasheet figures near 1 GHz (Rogers: the process εr). A metal
core has none (0), so the AC solve's Default εr applies to it.

Values are typical in-plane conductivities (W/m K) — what spreads heat sideways —
rounded from manufacturer data. Glass-reinforced laminates conduct better in-plane
than through the thickness (the glass weave), so these are higher than the
through-plane figures datasheets usually quote; those are kept as well, for the heat that
crosses the laminate from one copper layer to the next. Names are matched loosely: KiCad's
built-in materials ("FR4", "Polyimide", "Kapton", "PTFE", "Ceramic", "Al", ...), vendor
part names ("RO4350B", "370HR") and prepreg descriptions ("2116 RC54%") all work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

DEFAULT_K_W_MK = 0.8          # FR4 core, used when a name is not recognized
DEFAULT_K_Z_W_MK = 0.3        # FR4 core through the thickness

DEFAULT_ER = 4.5              # FR4 core, as KiCad's stackup editor gives it

# Coatings over the outer copper (#82): an LPI epoxy solder mask and an epoxy silkscreen ink.
MASK_K_W_MK = 0.25
SILK_K_W_MK = 0.20

# (pattern, description, in-plane k in W/(m K), typical εr, through-plane k in W/(m K));
# first match wins, so specific first. Each description also matches its own pattern.
_TABLE: List[Tuple[str, str, float, float, float]] = [
    (r"alumin(i)?um\s*nitride|\baln\b", "aluminum nitride ceramic", 170.0, 8.8, 170.0),
    (r"^\s*al\s*$|alumin(i)?um(?!\s*(oxide|nitride))|metal\s*core|\bims\b",
     "aluminum (metal core)", 205.0, 0.0, 205.0),
    (r"alumina|al2o3|ceramic", "alumina ceramic (96%)", 24.0, 9.6, 24.0),
    (r"kapton", "Kapton polyimide film", 0.12, 3.4, 0.12),
    (r"polyimide|\bpi\b", "polyimide laminate", 0.30, 4.3, 0.25),
    (r"polyolefin", "polyolefin", 0.25, 2.3, 0.25),
    (r"ro\s*4350", "Rogers RO4350B", 0.69, 3.48, 0.69),
    (r"ro\s*4003", "Rogers RO4003C", 0.71, 3.38, 0.71),
    (r"ro\s*3003", "Rogers RO3003", 0.50, 3.0, 0.50),
    (r"ro\s*3\d\d\d", "Rogers RO3000 series", 0.50, 3.0, 0.50),
    (r"duroid|5880|5870", "Rogers RT/duroid (PTFE)", 0.20, 2.2, 0.20),
    (r"ptfe|teflon", "PTFE", 0.25, 2.1, 0.25),
    (r"\b(1027|1035|1037|106|1067|1078|1080|1086|2113|2116|2313|3313|3070|1501|1506|1652|7628)\b"
     r"|prepreg|\brc\s*\d+", "FR4 prepreg", 0.6, 4.2, 0.25),
    (r"fr[-\s]?4|fr408|370\s*hr|is\s*4\d\d|isola|\bepoxy|tu[-\s]?\d|core", "FR4 core", 0.8,
     DEFAULT_ER, DEFAULT_K_Z_W_MK),
]
# The order the Stackup window lists the materials in: the usual ones first.
_CATALOG_ORDER = ["FR4 core", "FR4 prepreg", "polyimide laminate", "Kapton polyimide film",
                  "Rogers RO4350B", "Rogers RO4003C", "Rogers RO3003", "Rogers RO3000 series",
                  "Rogers RT/duroid (PTFE)", "PTFE", "polyolefin", "alumina ceramic (96%)",
                  "aluminum nitride ceramic", "aluminum (metal core)"]


@dataclass
class Dielectric:
    material: str        # as named in the stackup
    matched: str         # what it was taken to be
    k_w_mk: float
    assumed: bool        # True when the name was not recognized
    er: float = 0.0      # typical εr of what it was taken to be (0: none, as for a metal)
    k_z_w_mk: float = 0.0    # through the thickness; 0 = the same as in-plane

    @property
    def k_through(self) -> float:
        kz = getattr(self, "k_z_w_mk", 0.0)          # results saved before it
        return kz if kz > 0 else self.k_w_mk


def lookup(material: str) -> Dielectric:
    name = (material or "").strip()
    low = name.lower()
    for pattern, desc, k, er, kz in _TABLE:
        if re.search(pattern, low):
            return Dielectric(name, desc, k, False, er, kz)
    return Dielectric(name, "unrecognized, assumed FR4", DEFAULT_K_W_MK, True, DEFAULT_ER,
                      DEFAULT_K_Z_W_MK)


def catalog() -> List[Dielectric]:
    """The materials KiPI knows, to choose from: name, in-plane k and typical εr."""
    by_name = {desc: (k, er, kz) for _, desc, k, er, kz in _TABLE}
    return [Dielectric(n, n, by_name[n][0], False, by_name[n][1], by_name[n][2])
            for n in _CATALOG_ORDER]


def laminate_conductance(stackup):
    """In-plane thermal sheet conductance of the dielectrics, in W/K per square.

    Returns (conductance, [(material, thickness_mm, Dielectric), ...]). A dielectric with
    its own conductivity (set in the Stackup window) uses that instead of its material's.
    """
    rows = []
    g = 0.0
    dielectrics = list(stackup.dielectrics)
    if not dielectrics:   # no dielectric data: the board between copper as one FR4 layer
        from kipi.core.model.board import DielectricLayer
        t = max(stackup.board_thickness_mm - sum(c.thickness_mm for c in stackup.copper), 0.0)
        dielectrics = [DielectricLayer(t, "FR4")]
    for d in dielectrics:
        info = _material(d)
        g += info.k_w_mk * 1e-3 * d.thickness_mm          # W/(mm K) * mm
        rows.append((d.material, d.thickness_mm, info))
    return g, rows


def _material(d) -> Dielectric:
    """A dielectric's material. A conductivity set in the Stackup window is in-plane; the
    through-plane one keeps the material's ratio of the two (FR4: 0.3 / 0.8)."""
    typical = lookup(d.material)
    k = float(getattr(d, "k_w_mk", 0.0))
    if k > 0:
        return Dielectric(d.material, "set in the Stackup window", k, False,
                          k_z_w_mk=k * typical.k_through / typical.k_w_mk)
    return typical


def laminate_layers(stackup) -> Tuple[List[float], List[float]]:
    """The laminate as the layered heat model sees it, copper layers top to bottom:
    the in-plane sheet conductance (W/K per square) each copper layer carries, and the
    through-plane thermal resistance (K mm²/W) of each gap between neighboring copper
    layers.

    Each dielectric's in-plane conductance is shared equally by the copper on its two
    faces (all of it to the one copper layer it touches, if only one). Thickness of a gap
    that no dielectric accounts for, as when the stackup has none, is taken as FR4 core.
    """
    cu = stackup.copper
    n = max(len(cu), 1)
    index = {c.id: i for i, c in enumerate(cu)}
    g = [0.0] * n
    r = [0.0] * (n - 1)
    seen = [0.0] * (n - 1)
    for d in stackup.dielectrics:
        info = _material(d)
        gd = info.k_w_mk * 1e-3 * d.thickness_mm
        i, j = index.get(d.above_layer), index.get(d.below_layer)
        if i is not None and j is not None:
            g[i] += gd / 2
            g[j] += gd / 2
            if j == i + 1:
                r[i] += d.thickness_mm / (info.k_through * 1e-3)
                seen[i] += d.thickness_mm
        elif i is not None or j is not None:
            g[i if i is not None else j] += gd
        else:
            g = [x + gd / n for x in g]
    if not cu:            # no copper at all: the board as one FR4 sheet
        g[0] += DEFAULT_K_W_MK * 1e-3 * stackup.board_thickness_mm
    for i in range(n - 1):
        rest = max(cu[i].z_bottom_mm - cu[i + 1].z_top_mm - seen[i], 0.0)
        if rest > 1e-6:
            r[i] += rest / (DEFAULT_K_Z_W_MK * 1e-3)
            g[i] += DEFAULT_K_W_MK * 1e-3 * rest / 2
            g[i + 1] += DEFAULT_K_W_MK * 1e-3 * rest / 2
    return g, r


def coating_k(coating) -> float:
    """A solder mask's or silkscreen's thermal conductivity, W/(m K)."""
    from kipi.core.model.board import MASK
    return MASK_K_W_MK if coating.kind == MASK else SILK_K_W_MK


def coating_resistance(stackup, side: str) -> float:
    """Thermal resistance (K mm²/W) of the solder mask and silkscreen on ``side`` ("top"
    or "bottom"), in series between the outer copper and the air. The silkscreen is taken
    to cover the whole face, the most it can hold heat in."""
    return sum(c.thickness_mm / (coating_k(c) * 1e-3)
               for c in getattr(stackup, "coatings", []) if c.side == side)


def coating_conductance(stackup, side: str) -> float:
    """In-plane sheet conductance (W/K per square) the coatings on ``side`` add to the
    outer copper layer under them."""
    return sum(coating_k(c) * 1e-3 * c.thickness_mm
               for c in getattr(stackup, "coatings", []) if c.side == side)
