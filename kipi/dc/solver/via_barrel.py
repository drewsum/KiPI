"""How much copper a plated hole carries, from the board's via rules.

A drill size is the hole made in the laminate. Plating grows inward from that wall, so a
barrel is a copper tube whose OUTSIDE diameter is the drill and whose wall is the plating
thickness. A filled hole carries its core as well when the fill conducts:

* non-conductive epoxy (the usual fill): the copper wall only;
* plated copper (via-in-pad, stacked microvias): the whole hole is copper;
* conductive (silver-loaded) epoxy: the core at the resistivity the user enters.

A surface finish (ENIG, ENEPIG, immersion silver, HASL, OSP) coats exposed copper only and
is a few micrometers of a poorer conductor; it is not modeled.

Plating differs by via type and size and between fabricators, so it is a table of rules,
first match wins. The defaults are IPC-6012 Class 2 minimum average plating; set the
values your fabricator states.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

VIA_KINDS = ("through", "blind", "buried", "blind_buried", "micro", "pth", "any")
FILLS = ("nonconductive", "copper", "conductive")
FILL_WORDS = {"nonconductive": "non-conductive epoxy", "copper": "copper",
              "conductive": "conductive epoxy"}


@dataclass
class ViaRule:
    kind: str                         # one of VIA_KINDS; "pth" = a component's plated hole
    plating_mm: float
    drill_min_mm: float = 0.0         # this rule covers drills from min up to max (inclusive)
    drill_max_mm: Optional[float] = None
    fill: str = "nonconductive"       # what a FILLED hole of this rule is filled with
    fill_rho_ohm_mm: Optional[float] = None   # conductive fill only: its resistivity

    def covers(self, kind: str, drill_mm: float) -> bool:
        return ((self.kind == "any" or self.kind == kind) and drill_mm >= self.drill_min_mm
                and (self.drill_max_mm is None or drill_mm <= self.drill_max_mm))

    def label(self) -> str:
        size = (f"{self.drill_min_mm:g}-{self.drill_max_mm:g} mm" if self.drill_max_mm is not None
                else (f">= {self.drill_min_mm:g} mm" if self.drill_min_mm else "any drill"))
        return (f"{self.kind} {size}: {self.plating_mm * 1000:g} um, "
                f"if filled: {FILL_WORDS.get(self.fill, self.fill)}")


def default_via_rules() -> List[ViaRule]:
    """IPC-6012 Class 2 minimum average plating per hole type."""
    return [ViaRule("micro", 0.012), ViaRule("buried", 0.013), ViaRule("blind", 0.020),
            ViaRule("blind_buried", 0.020), ViaRule("through", 0.020), ViaRule("pth", 0.020),
            ViaRule("any", 0.020)]


def rule_errors(rules: List[ViaRule]) -> List[str]:
    errors = []
    for i, r in enumerate(rules, 1):
        if r.kind not in VIA_KINDS:
            errors.append(f"Via rule {i}: unknown hole type {r.kind!r}")
        if r.plating_mm <= 0:
            errors.append(f"Via rule {i}: plating must be more than 0")
        if r.fill not in FILLS:
            errors.append(f"Via rule {i}: unknown fill {r.fill!r}")
        if r.fill == "conductive" and not (r.fill_rho_ohm_mm and r.fill_rho_ohm_mm > 0):
            errors.append(f"Via rule {i}: a conductive fill needs its resistivity")
        if r.drill_max_mm is not None and r.drill_max_mm < r.drill_min_mm:
            errors.append(f"Via rule {i}: drill range maximum is below its minimum")
    return errors


def rule_for(rules: List[ViaRule], kind: str, drill_mm: float) -> Optional[ViaRule]:
    return next((r for r in rules if r.covers(kind, drill_mm)), None)


def barrel_copper_area(rule: ViaRule, drill_mm: float, filled: Optional[bool],
                       rho_cu_ohm_mm: float) -> float:
    """Cross-section (mm^2) of copper that conducts like the barrel, the fill's share
    expressed as the copper area that would conduct the same."""
    tp = rule.plating_mm
    full = math.pi * drill_mm * drill_mm / 4.0
    if drill_mm <= 2 * tp:
        return full                       # plated shut
    wall = math.pi * (drill_mm * tp - tp * tp)
    if not filled or rule.fill == "nonconductive":
        return wall
    core = full - wall
    if rule.fill == "copper":
        return wall + core
    return wall + core * rho_cu_ohm_mm / rule.fill_rho_ohm_mm
