"""PCB dielectric materials: in-plane thermal conductivity and a typical εr, looked up
from the material name in the KiCad stackup, and the list the Stackup window offers.

The εr values are typical datasheet figures near 1 GHz (Rogers: the process εr). A metal
core has none (0), so the AC solve's Default εr applies to it.

Values are typical in-plane conductivities (W/m K) — what spreads heat sideways —
rounded from manufacturer data. Glass-reinforced laminates conduct better in-plane
than through the thickness (the glass weave), so these are higher than the
through-plane figures datasheets usually quote. Names are matched loosely: KiCad's
built-in materials ("FR4", "Polyimide", "Kapton", "PTFE", "Ceramic", "Al", ...), vendor
part names ("RO4350B", "370HR") and prepreg descriptions ("2116 RC54%") all work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

DEFAULT_K_W_MK = 0.8          # FR4 core, used when a name is not recognized

DEFAULT_ER = 4.5              # FR4 core, as KiCad's stackup editor gives it

# (pattern, description, in-plane k in W/(m K), typical εr); first match wins, so specific
# first. Each description also matches its own pattern.
_TABLE: List[Tuple[str, str, float, float]] = [
    (r"alumin(i)?um\s*nitride|\baln\b", "aluminum nitride ceramic", 170.0, 8.8),
    (r"^\s*al\s*$|alumin(i)?um(?!\s*(oxide|nitride))|metal\s*core|\bims\b",
     "aluminum (metal core)", 205.0, 0.0),
    (r"alumina|al2o3|ceramic", "alumina ceramic (96%)", 24.0, 9.6),
    (r"kapton", "Kapton polyimide film", 0.12, 3.4),
    (r"polyimide|\bpi\b", "polyimide laminate", 0.30, 4.3),
    (r"polyolefin", "polyolefin", 0.25, 2.3),
    (r"ro\s*4350", "Rogers RO4350B", 0.69, 3.48),
    (r"ro\s*4003", "Rogers RO4003C", 0.71, 3.38),
    (r"ro\s*3003", "Rogers RO3003", 0.50, 3.0),
    (r"ro\s*3\d\d\d", "Rogers RO3000 series", 0.50, 3.0),
    (r"duroid|5880|5870", "Rogers RT/duroid (PTFE)", 0.20, 2.2),
    (r"ptfe|teflon", "PTFE", 0.25, 2.1),
    (r"\b(1027|1035|1037|106|1067|1078|1080|1086|2113|2116|2313|3313|3070|1501|1506|1652|7628)\b"
     r"|prepreg|\brc\s*\d+", "FR4 prepreg", 0.6, 4.2),
    (r"fr[-\s]?4|fr408|370\s*hr|is\s*4\d\d|isola|\bepoxy|tu[-\s]?\d|core", "FR4 core", 0.8,
     DEFAULT_ER),
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


def lookup(material: str) -> Dielectric:
    name = (material or "").strip()
    low = name.lower()
    for pattern, desc, k, er in _TABLE:
        if re.search(pattern, low):
            return Dielectric(name, desc, k, False, er)
    return Dielectric(name, "unrecognized, assumed FR4", DEFAULT_K_W_MK, True, DEFAULT_ER)


def catalog() -> List[Dielectric]:
    """The materials KiPI knows, to choose from: name, in-plane k and typical εr."""
    by_name = {desc: (k, er) for _, desc, k, er in _TABLE}
    return [Dielectric(n, n, by_name[n][0], False, by_name[n][1]) for n in _CATALOG_ORDER]


def laminate_conductance(stackup, override_k_w_mk: Optional[float] = None):
    """In-plane thermal sheet conductance of the dielectrics, in W/K per square.

    Returns (conductance, [(material, thickness_mm, Dielectric), ...]). With an
    override, every dielectric uses that conductivity instead.
    """
    rows = []
    g = 0.0
    dielectrics = list(stackup.dielectrics)
    if not dielectrics:   # no dielectric data: the board between copper as one FR4 layer
        from kipi.core.model.board import DielectricLayer
        t = max(stackup.board_thickness_mm - sum(c.thickness_mm for c in stackup.copper), 0.0)
        dielectrics = [DielectricLayer(t, "FR4")]
    for d in dielectrics:
        info = lookup(d.material)
        if override_k_w_mk:
            info = Dielectric(d.material, "user override", float(override_k_w_mk), False)
        g += info.k_w_mk * 1e-3 * d.thickness_mm          # W/(mm K) * mm
        rows.append((d.material, d.thickness_mm, info))
    return g, rows
