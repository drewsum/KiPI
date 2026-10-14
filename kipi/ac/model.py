"""The AC part of the user's setup: capacitor models, probe points and sweep settings.

These live in the same setup file as the DC power tree (:class:`kipi.dc.pi.model.PISetup`),
which also holds the VRMs and series links that both analyses share. Nothing here
imports the DC model, so the setup can import this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:                       # PinRef lives in the setup model, which imports us
    from kipi.dc.pi.model import PinRef


@dataclass
class CapModel:
    """An RLC model shared by one or more capacitors (a group of identical parts)."""
    name: str
    capacitance_f: float = 0.0          # all three are required (> 0) before solving
    esl_h: float = 0.0
    esr_ohm: float = 0.0
    refs: List[str] = field(default_factory=list)   # footprint references using this model

    def impedance(self, omega):
        """Complex impedance at angular frequency ``omega`` (scalar or array)."""
        return self.esr_ohm + 1j * omega * self.esl_h + 1.0 / (1j * omega * self.capacitance_f)

    def srf_hz(self) -> Optional[float]:
        """Self-resonant frequency of the part alone (no mounting)."""
        import math
        if self.capacitance_f > 0 and self.esl_h > 0:
            return 1.0 / (2 * math.pi * math.sqrt(self.capacitance_f * self.esl_h))
        return None


@dataclass
class Probe:
    """Where the PDN impedance is looked into: a device's supply pins and ground pins."""
    name: str
    rail: "PinRef"
    ground: "PinRef"
    target_ohm: Optional[float] = None    # target impedance, flat up to target_hz
    target_hz: Optional[float] = None


@dataclass
class ACSettings:
    # Sweep
    f_start_hz: float = 1e3
    f_stop_hz: float = 300e6
    points_per_decade: int = 250
    # Field (copper) solves per decade; the sweep in between is interpolated. 0 = solve the
    # copper at every sweep point (exact, slow).
    field_points_per_decade: int = 20
    # Mesh: coarser than DC by default; refined toward plated holes, where via inductance
    # is decided.
    max_edge_mm: float = 1.0
    grading: float = 0.3
    min_edge_mm: float = 0.03
    # Dielectric
    loss_tangent: float = 0.02
    default_er: float = 4.3               # where the stackup has no dielectric constant
    # Last rail (VRM name) and ground net chosen for a solve.
    rail: str = ""
    ground: str = ""

    def frequencies(self):
        import numpy as np
        decades = np.log10(self.f_stop_hz / self.f_start_hz)
        n = max(2, int(round(decades * self.points_per_decade)) + 1)
        return np.logspace(np.log10(self.f_start_hz), np.log10(self.f_stop_hz), n)

    def field_frequencies(self):
        import numpy as np
        if self.field_points_per_decade <= 0 or \
                self.field_points_per_decade >= self.points_per_decade:
            return self.frequencies()
        decades = np.log10(self.f_stop_hz / self.f_start_hz)
        n = max(4, int(np.ceil(decades * self.field_points_per_decade)) + 1)
        return np.logspace(np.log10(self.f_start_hz), np.log10(self.f_stop_hz), n)


def caps_dict(caps: List[CapModel]) -> Dict[str, CapModel]:
    """Footprint reference -> its model."""
    return {ref: m for m in caps for ref in m.refs}
