"""Conductors a copper layer can be made of, for the Stackup window (#61).

Each has its resistivity at 20 °C, temperature coefficient of resistance and thermal
conductivity: typical handbook values for the bulk metal, and IPC-4562's maximum
resistivity for electrodeposited and rolled-annealed copper foil. A layer with no conductor
chosen uses the setup's board-wide Resistivity and TCR, and copper's thermal conductivity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

UOHM_CM = 1e-5               # 1 µΩ·cm in Ω·mm, the unit the solver works in
K_CU_W_MK = 390.0            # copper, as the board thermal model takes it


@dataclass(frozen=True)
class Conductor:
    name: str
    rho_uohm_cm: float       # resistivity at 20 °C
    tcr_per_c: float         # temperature coefficient of resistance
    k_w_mk: float            # thermal conductivity

    @property
    def rho_ohm_mm(self) -> float:
        return self.rho_uohm_cm * UOHM_CM


_CONDUCTORS = [
    Conductor("Copper, annealed (IACS)", 1.724, 0.00393, K_CU_W_MK),
    Conductor("Copper, electrodeposited foil", 1.78, 0.0039, K_CU_W_MK),
    Conductor("Copper, rolled-annealed foil", 1.74, 0.00393, K_CU_W_MK),
    Conductor("Silver", 1.59, 0.0038, 429.0),
    Conductor("Gold", 2.44, 0.0034, 318.0),
    Conductor("Aluminum", 2.65, 0.00429, 237.0),
    Conductor("Nickel", 6.99, 0.0060, 91.0),
    Conductor("Tin", 10.9, 0.0045, 67.0),
]


def conductors() -> List[Conductor]:
    """The conductors KiPI knows, in the order the Stackup window lists them."""
    return list(_CONDUCTORS)


def find(name: str) -> Optional[Conductor]:
    for c in _CONDUCTORS:
        if c.name == name:
            return c
    return None


def layer_k_w_mk(conductor_by_layer: Dict[str, str], layer_name: str) -> float:
    """Thermal conductivity of a copper layer: its chosen conductor's, else copper's."""
    c = find(conductor_by_layer.get(layer_name, ""))
    return c.k_w_mk if c is not None else K_CU_W_MK
