"""Results of a power-tree solve: per net group, per load, per VRM."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kipi.dc.solver import NetResult

NOT_CONVERGED = "NOT CONVERGED"
OUT_OF_REGULATION = "VRM OUT OF REGULATION"
DISCONNECTED = "DISCONNECTED"       # no copper path to its VRM: not solved (#88)


@dataclass
class GroupResult:
    name: str
    nets: List[str]
    kind: str                 # "rail" or "ground"
    driver: str               # VRM name (rail) or reference VRM name(s) (ground)
    result: NetResult
    iterations: int = 1
    converged: bool = True


BOOST_EPS = 1e-12          # volts; smaller deviations read as a (zero) drop, not a boost


def drop_or_boost(dv: float):
    """Split ``dv`` = nominal - actual into (magnitude, "drop" | "boost").

    A load can sit above its VRM's set point: a remote-sensed VRM raises its output pins
    to hold a sense point further down the copper, so pins nearer the VRM see a boost.
    """
    if math.isnan(dv):                 # nothing solved, as for a disconnected load (#88)
        return dv, ""
    return (-dv, "boost") if dv < -BOOST_EPS else (max(dv, 0.0), "drop")


def fmt_drop(dv: float, fmt: str = ".3f", scale: float = 1e3, unit: str = " mV") -> str:
    """``dv`` (volts, nominal - actual) as e.g. "1.234 mV drop" or "0.567 mV boost"."""
    mag, kind = drop_or_boost(dv)
    return f"{mag * scale:{fmt}}{unit} {kind}" if kind else ""


@dataclass
class LoadReport:
    name: str
    group: str
    current: float
    nominal: float            # set point of the VRM driving the group
    v_supply: float           # mean over the supply pins
    v_supply_min: float
    v_ground: Optional[float]  # mean over the ground pins (None if not modeled)
    v_effective: float        # supply - ground
    drop: float               # nominal - effective, volts (negative: a boost)
    tolerance: str                       # as set, for tables: "±5 %", "3.1 to 3.5 V"
    passed: Optional[bool]               # judged on the worst pin
    v_worst: float = 0.0                 # whichever of v_low / v_high is further from nominal
    # (name, A, V mean, V min, V max) per pin group
    pin_groups: List[tuple] = field(default_factory=list)
    v_low: Optional[float] = None        # lowest supply pin minus highest ground pin
    v_high: Optional[float] = None       # highest supply pin minus lowest ground pin
    status: str = ""                     # why the load was not judged, or was failed outright
    limit_low: Optional[float] = None    # the tolerance in volts, either end None if not set
    limit_high: Optional[float] = None
    load_nominal: Optional[float] = None  # load's own nominal, or the VRM set point

    def __setstate__(self, state):
        # Results files saved before boosts were reported: only the lowest pin is known,
        # and pin groups have no highest pin. Before asymmetric tolerances: +/- one %.
        t = state.pop("tolerance_pct", None)
        if t is not None and "tolerance" not in state:
            nom = state.get("nominal", 0.0)
            state.update(tolerance=f"±{t:g} %", limit_low=nom - abs(nom) * t / 100,
                         limit_high=nom + abs(nom) * t / 100)
        state = {"v_low": state.get("v_worst"), "v_high": None, "status": "",
                 "tolerance": "", "limit_low": None, "limit_high": None,
                 "load_nominal": state.get("nominal"), **state}
        state["pin_groups"] = [tuple(g) + (None,) * (5 - len(g))
                               for g in state.get("pin_groups", [])]
        self.__dict__.update(state)

    @property
    def drop_pct(self) -> float:
        return 100.0 * self.drop / abs(self.nominal) if self.nominal else 0.0

    @property
    def drop_worst(self) -> float:
        """Load nominal minus its worst pin, volts (negative: above nominal)."""
        nominal = self.load_nominal if self.load_nominal is not None else self.nominal
        return nominal - self.v_worst

    @property
    def drop_worst_pct(self) -> float:
        nominal = self.load_nominal if self.load_nominal is not None else self.nominal
        return 100.0 * self.drop_worst / abs(nominal) if nominal else 0.0

    @property
    def verdict(self) -> str:
        if self.status:
            return self.status
        return "" if self.passed is None else ("PASS" if self.passed else "FAIL")


@dataclass
class PhaseReport:
    index: int                # 1-based
    v_out: float              # mean over the phase's output pins
    i_out: float
    v_in: Optional[float]
    i_in: Optional[float]

    @property
    def p_out(self) -> float:
        return self.v_out * self.i_out

    @property
    def p_in(self) -> Optional[float]:
        return None if self.v_in is None else self.v_in * self.i_in

    @property
    def p_loss(self) -> Optional[float]:
        return None if self.p_in is None else self.p_in - self.p_out


@dataclass
class VrmReport:
    name: str
    kind: str
    group: str
    v_set: float
    v_out: float              # at the output pins (after sense correction); multi-phase:
                              # the mean of the phases
    v_sense: Optional[float]
    i_out: float
    v_in: Optional[float]
    i_in: Optional[float]
    p_out: float
    p_in: Optional[float]
    v_ground_sense: Optional[float] = None   # at the ground sense pins, if any
    phases: List[PhaseReport] = field(default_factory=list)   # multi-phase VRMs only
    current_limit_a: Optional[float] = None   # total output current limit, if one is set
    passed: Optional[bool] = None             # |I out| within the limit
    regulation: str = ""                     # why the VRM cannot hold its set point
    load_line_mohm: float = 0.0               # load line (#94), 0 = none
    v_droop: float = 0.0                      # load line x I out: how far v_target is
                                              # below v_set

    def __setstate__(self, state):
        # Results files saved before multi-phase VRMs, current limits, regulation
        # checks or load lines lack those fields.
        self.__dict__.update({"phases": [], "current_limit_a": None, "passed": None,
                              "regulation": "", "load_line_mohm": 0.0,
                              "v_droop": 0.0, **state})

    @property
    def v_target(self) -> float:
        """The voltage the VRM regulates to: its set point less the load-line droop."""
        return self.v_set - self.v_droop

    @property
    def p_loss(self) -> Optional[float]:
        return None if self.p_in is None else self.p_in - self.p_out


@dataclass
class TreeResult:
    groups: Dict[str, GroupResult] = field(default_factory=dict)
    loads: List[LoadReport] = field(default_factory=list)
    vrms: List[VrmReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    solve_s: float = 0.0
    thermal: Optional[object] = None          # kipi.dc.thermal.cosim.ThermalResult, if run
    report_path: Optional[object] = None      # HTML report written for this solve, if any
