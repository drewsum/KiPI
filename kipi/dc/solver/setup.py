"""What to solve: sources, loads, series parts and numerical settings.

A problem covers one *extended net*: one or more nets joined by series parts (sense
resistors, ferrites, fuses). Terminals refer to pads by id. A terminal with several
pads ties them to one condition: a source holds all of its pads at its voltage; a load
draws its current split equally across its pads; each side of a series part is one
node (its pads tied together).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from kipi.dc.solver.via_barrel import ViaRule, default_via_rules

# Annealed copper (IACS) at 20 degC, in ohm*mm.
RHO_CU_OHM_MM = 1.724e-5
REF_TEMP_C = 20.0


@dataclass
class VoltageSource:
    name: str
    pad_ids: List[str]
    voltage: float


@dataclass
class CurrentLoad:
    name: str
    pad_ids: List[str]
    current: float          # amps leaving the net at these pads (negative = injected)
    # May be left unconnected (#88): pads with no copper path to a source are left out and
    # the current is shared over the others; with none left, the load draws nothing.
    # Otherwise an unconnected pad stops the solve.
    optional: bool = False


@dataclass
class SeriesLink:
    name: str
    pads_a: List[str]
    pads_b: List[str]
    resistance: float       # ohms; at or below SHORT_OHMS the two sides are merged
    voltage_drop: float = 0.0  # volts, oriented from side A to side B
    # A diode's current from side A to side B at a voltage across it, and its slope (#191):
    # the solve finds the voltage where the copper carries that current. ``resistance``
    # is then only how the part is split between the matrix and the right-hand side; any
    # positive value gives the same answer.
    curve: Optional[Callable[[float], Tuple[float, float]]] = None
    # A multi-pin part's piece joining sides at DC (#193): the pads of each group of its
    # sides shorted together (side A's group first), and the conductance matrix between
    # the groups, siemens, its rows summing to zero. ``pads_a`` and ``pads_b`` are then its
    # first two groups' pads and ``resistance`` the resistance between them, for reports.
    groups: Optional[List[List[str]]] = None
    conductance: Optional[Any] = None


SHORT_OHMS = 1e-9


@dataclass
class SolveSettings:
    max_edge_mm: float = 0.5        # largest triangle edge; planes mesh at roughly this size
    min_angle_deg: float = 28.0     # triangle quality bound (Shewchuk's 'q' switch)
    simplify_mm: float = 0.003      # drop polygon detail finer than this before meshing
    circle_quad_segs: int = 4       # 16 segments per via/drill circle (an 8-gon reads ~0.8% high)
    min_edge_mm: float = 0.02       # smallest element, at plated holes
    grading: float = 0.2            # element size growth per mm away from a hole (0 = off)
    # Plating (and the fill of a filled hole) by hole type and drill size, first match.
    via_rules: List[ViaRule] = field(default_factory=default_via_rules)
    rho_ohm_mm: float = RHO_CU_OHM_MM      # at REF_TEMP_C
    tcr_per_c: float = 0.00393             # copper resistance temperature coefficient
    # Per-layer overrides of rho_ohm_mm, keyed by copper layer name (e.g. "In1.Cu").
    # Layers not listed, and via barrels, use rho_ohm_mm.
    rho_by_layer: Dict[str, float] = field(default_factory=dict)
    # Per-layer overrides of tcr_per_c, and the conductor chosen for a layer in the Stackup
    # window (a kipi.core.model.conductors name), keyed the same way.
    tcr_by_layer: Dict[str, float] = field(default_factory=dict)
    conductor_by_layer: Dict[str, str] = field(default_factory=dict)
    tol: float = 1e-10              # relative residual for the iterative solver
    max_iter: int = 2000
    # Terminal pads with at least large_pad_mm2 of copper solved as copper, their part
    # connected at a plated hole's wall or the central pad_contact_share of an SMD pad
    # (#207); off: every terminal pad is one equipotential.
    large_pads_as_copper: bool = False
    large_pad_mm2: float = 10.0
    pad_contact_share: float = 0.25

    def __post_init__(self):
        # A setup file stores the rules as plain objects.
        self.via_rules = [r if isinstance(r, ViaRule) else ViaRule(**r) for r in self.via_rules]

    def __setstate__(self, state):
        # Results saved before via rules (#89) carry one plating for every hole.
        if "via_rules" not in state:
            state = dict(state)
            plating = state.pop("plating_mm", None)
            rules = default_via_rules()
            if plating is not None:
                for r in rules:
                    r.plating_mm = float(plating)
            state["via_rules"] = rules
        self.__dict__.update(state)

    def pad_mesh_options(self) -> dict:
        """The mesher's large-pad arguments (kipi.core.mesh.mesher.MeshOptions)."""
        return {"large_pad_mm2": self.large_pad_mm2 if self.large_pads_as_copper else None,
                "pad_contact_share": self.pad_contact_share}

    def rho_for(self, layer_name: str) -> float:
        """Resistivity of a copper layer at REF_TEMP_C, in ohm*mm."""
        return self.rho_by_layer.get(layer_name, self.rho_ohm_mm)

    def tcr_for(self, layer_name: str) -> float:
        """Temperature coefficient of resistance of a copper layer, per °C."""
        return self.tcr_by_layer.get(layer_name, self.tcr_per_c)


@dataclass
class NetProblem:
    net: str                                  # display name (the nets joined with " + ")
    sources: List[VoltageSource] = field(default_factory=list)
    loads: List[CurrentLoad] = field(default_factory=list)
    settings: SolveSettings = field(default_factory=SolveSettings)
    series: List[SeriesLink] = field(default_factory=list)
    nets: List[str] = field(default_factory=list)   # copper nets meshed; default [net]
    # Optional copper temperature, a callable mapping (N, 2) KiCad xy in mm to degC.
    # Resistivity becomes rho * (1 + tcr * (T - REF_TEMP_C)). Not saved with setups.
    temperature: Optional[Callable] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not self.nets:
            self.nets = [self.net]

    def __getstate__(self):
        # The temperature is a closure over the thermal grid and cannot be pickled, so a
        # saved (or copied) problem has none; its solution keeps the effect on resistivity
        # (Solution.tri_rho_factor), and results files keep the temperatures themselves.
        state = self.__dict__.copy()
        state["temperature"] = None
        return state

    def terminal_pad_ids(self) -> List[str]:
        ids: List[str] = []
        for t in [*self.sources, *self.loads]:
            ids.extend(t.pad_ids)
        for s in self.series:
            ids.extend(s.pads_a)
            ids.extend(s.pads_b)
            for pads in s.groups or ():             # a multi-pin part's other sides
                ids.extend(pads)
        return list(dict.fromkeys(ids))
