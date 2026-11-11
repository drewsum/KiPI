# Portions Copyright (c) 2026 Tomachie LLC.
"""Results of a power-tree solve: per net group, per load, per VRM."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kipi.dc.solver import NetResult

NOT_CONVERGED = "NOT CONVERGED"
OUT_OF_REGULATION = "VRM OUT OF REGULATION"
# An LDO above its dropout but below the input its datasheet states its accuracy from: it
# regulates, but its output is not guaranteed to be inside its accuracy.
ACCURACY_NOT_GUARANTEED = "VRM ACCURACY NOT GUARANTEED"
DISCONNECTED = "DISCONNECTED"       # no copper path to its VRM: not solved (#88)
INCOMPLETE = "INCOMPLETE"           # no longer given: results files saved before headroom


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

    A load can sit above its VRM's target voltage: a remote-sensed VRM raises its output pins
    to hold a sense point further down the copper, so pins nearer the VRM see a boost.
    """
    if math.isnan(dv):                 # nothing solved, as for a disconnected load (#88)
        return dv, ""
    return (-dv, "boost") if dv < -BOOST_EPS else (max(dv, 0.0), "drop")


def fmt_amps(amps: float) -> str:
    """Amps for a sentence: "17.6 A" from 1 A up, "20 mA" below it, "350 µA" below 1 mA."""
    a = abs(amps)
    if a >= 1:
        return f"{amps:.3g} A"
    if a >= 1e-3:
        return f"{amps * 1e3:.3g} mA"
    if a >= 1e-6:
        return f"{amps * 1e6:.3g} µA"
    if a >= 1e-9:
        return f"{amps * 1e9:.3g} nA"
    return "0 A"


def fmt_drop(dv: float, fmt: str = ".3f", scale: float = 1e3, unit: str = " mV") -> str:
    """``dv`` (volts, nominal - actual) as e.g. "1.234 mV drop" or "0.567 mV boost"."""
    mag, kind = drop_or_boost(dv)
    return f"{mag * scale:{fmt}}{unit} {kind}" if kind else ""


@dataclass
class LoadReport:
    name: str
    group: str
    current: float
    nominal: float            # target voltage of the VRM driving the group
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
    basis: str = ""                      # the current solved: typ, max, typ (no max)
    copper_margin: Optional[float] = None  # the load's pre-layout copper margin, volts
    # Amps the rail's loads whose current is not known may add together before this load
    # leaves its tolerance (RailHeadroom); None without a low limit or without such loads.
    unknown_room_a: Optional[float] = None
    # True when a load on the same rail has a current not known: an answer "in range" then
    # holds at the known currents (the Headroom says what the rest may add).
    unknowns_on_rail: bool = False
    # The return: the load's ground pins (mean) above its feeding VRM's own ground pins
    # (mean), volts - the ground copper this load's current crosses back to its source,
    # on the same footing as a tool that measures from the source's return. None when
    # either has no ground pins. v_ground is measured from the board's 0 V reference.
    return_drop: Optional[float] = None

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
                 "copper_margin": None, "unknown_room_a": None, "unknowns_on_rail": False,
                 "return_drop": None,
                 **state}
        state["pin_groups"] = [tuple(g) + (None,) * (5 - len(g))
                               for g in state.get("pin_groups", [])]
        self.__dict__.update(state)

    @property
    def drop_pct(self) -> float:
        return 100.0 * self.drop / abs(self.nominal) if self.nominal else 0.0

    @property
    def drop_worst(self) -> float:
        """Nominal minus the worst pin, volts (negative: a boost)."""
        return self.nominal - self.v_worst

    @property
    def drop_worst_pct(self) -> float:
        return 100.0 * self.drop_worst / abs(self.nominal) if self.nominal else 0.0

    @property
    def margin_low(self) -> Optional[float]:
        """Volts the lowest pin is above the tolerance's low end (negative: below it);
        None without a low end."""
        if self.limit_low is None or self.v_low is None or self.v_low != self.v_low:
            return None
        return self.v_low - self.limit_low

    @property
    def verdict(self) -> str:
        if self.status:
            return self.status
        return "" if self.passed is None else ("PASS" if self.passed else "FAIL")

    @property
    def margin_high(self) -> Optional[float]:
        """Volts the highest pin is below the tolerance's high end (negative: above it);
        None without a high end."""
        if self.limit_high is None or self.v_high is None or self.v_high != self.v_high:
            return None
        return self.limit_high - self.v_high

    @property
    def answer(self) -> str:
        """The result in words: what was judged, against what, and how much room is left.
        "In range" means every supply pin, less its ground pins, is inside the load's
        voltage window at the currents solved."""
        if self.status:
            return self.status
        if self.passed is None:
            return ""
        low, high = self.margin_low, self.margin_high
        if self.passed:
            spare = min(m for m in (low, high) if m is not None)
            where = " at known currents" if self.unknowns_on_rail else ""
            return f"In range{where}, {spare * 1e3:.4g} mV spare"
        if low is not None and low < 0:
            return f"Out of range: {-low * 1e3:.4g} mV below its {self.limit_low:.4g} V minimum"
        return f"Out of range: {-high * 1e3:.4g} mV above its {self.limit_high:.4g} V maximum"


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
    regulation: str = ""                     # why the VRM cannot hold its target voltage
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
        """The voltage the VRM regulates to: its target voltage less the load-line droop."""
        return self.v_set - self.v_droop

    @property
    def p_loss(self) -> Optional[float]:
        return None if self.p_in is None else self.p_in - self.p_out


@dataclass
class LinkRating:
    """A series part's solved current against its rated current."""
    name: str
    group: str
    current: float               # amps through it (NaN for a short)
    rated_a: Optional[float]     # its continuous rated current; None when not given

    @property
    def within(self) -> Optional[bool]:
        """At or below its rating; a current that rounds to the rating (a USB port's 0.5 A
        through a 0.5 A switch, plus the switch's own microamps) is at it."""
        if self.rated_a is None or self.current != self.current:
            return None
        i = abs(self.current)
        return i <= self.rated_a or fmt_amps(i) == fmt_amps(self.rated_a)

    @property
    def answer(self) -> str:
        if self.rated_a is None:
            return "No rated current given: not checked"
        if self.current != self.current:
            return "Current not resolved (a short): not checked"
        i = abs(self.current)
        if fmt_amps(i) == fmt_amps(self.rated_a):
            return f"At its rating: {fmt_amps(i)} of {fmt_amps(self.rated_a)}"
        if i <= self.rated_a:
            return f"{fmt_amps(i)} of {fmt_amps(self.rated_a)} rated"
        return f"Above its rating: {fmt_amps(i)} of {fmt_amps(self.rated_a)}"


@dataclass
class RailHeadroom:
    """What a rail's loads whose current is not known may draw, together, before a load
    on the rail leaves its tolerance: the designer's unknowns (what a header or a
    translator's lines will carry) as a budget, from the margin the known currents leave.
    Supply copper and, where the unknown has ground pins, its ground return; the VRM
    holds what it held (a load line's droop is not counted)."""
    group: str                          # the rail's label
    vrm: str                            # the VRM that drives it
    known_a: float                      # what the VRM supplies at the known currents
    unknown: List[str]                  # the loads whose current is not known
    tightest: str = ""                  # the load with the least room ("" when none is judged)
    margin_v: Optional[float] = None    # its lowest pin above its tolerance's low end
    room_a: Optional[float] = None      # what the unknowns may add together (0: already out)
    costliest: str = ""                 # the unknown whose current costs that load the most
    per_amp_v: float = 0.0              # volts the tightest load loses per amp drawn there
    upstream: List[str] = field(default_factory=list)   # rails the same current also loads
    vrm_room_a: Optional[float] = None  # what the VRM's output current limit leaves, if set
    vrm_limit_a: Optional[float] = None  # that limit

    @property
    def binding_a(self) -> Optional[float]:
        """What the unknowns may add: the copper's room or the VRM limit's, the smaller."""
        rooms = [r for r in (self.room_a, self.vrm_room_a) if r is not None]
        return max(0.0, min(rooms)) if rooms else None

    @property
    def vrm_binds(self) -> bool:
        """The VRM's rating, not the copper, sets ``binding_a``."""
        return self.vrm_room_a is not None and (self.room_a is None
                                                or self.vrm_room_a < self.room_a)

    @property
    def limited_by(self) -> str:
        """What sets ``binding_a``: the tightest load's supply minimum, or the VRM's rating."""
        if self.binding_a is None:
            return ""
        if self.vrm_binds:
            return f"{self.vrm} rated {fmt_amps(self.vrm_limit_a)}"
        return f"{self.tightest}'s supply minimum"

    def sentence(self) -> str:
        """The budget in one sentence, for the notes and the report."""
        who = ", ".join(self.unknown)
        s = f"{self.group}: {fmt_amps(self.known_a)} known. Not known: {who}. "
        if self.margin_v is not None and self.margin_v <= 0:
            return s + (f"{self.tightest} is already {-self.margin_v * 1e3:.4g} mV below "
                        "its minimum at the known currents; nothing can be added.")
        if self.binding_a is None:
            return s + ("No load on this rail has a minimum, and its VRM no rated current, "
                        "so the room for them cannot be given.")
        s += f"Together they must stay under {fmt_amps(self.binding_a)}"
        if self.vrm_binds:
            s += f": {self.vrm} is rated {fmt_amps(self.vrm_limit_a)}."
            if self.room_a is not None:
                s += (f" The copper alone would take {fmt_amps(self.room_a)} before "
                      f"{self.tightest} ({self.margin_v * 1e3:.4g} mV above its minimum) "
                      "leaves its tolerance.")
        else:
            s += (f", or {self.tightest} ({self.margin_v * 1e3:.4g} mV above its minimum) "
                  f"leaves its tolerance (worst case, all of it at {self.costliest}: "
                  f"{self.per_amp_v * 1e3:.4g} mV per A).")
            if self.vrm_room_a is not None:
                s += f" {self.vrm}'s rating leaves {fmt_amps(self.vrm_room_a)}."
        if self.upstream:
            s += (f" Through {self.vrm} the same current also loads "
                  f"{', '.join(self.upstream)}; not counted there.")
        return s


@dataclass
class TreeResult:
    groups: Dict[str, GroupResult] = field(default_factory=dict)
    loads: List[LoadReport] = field(default_factory=list)
    vrms: List[VrmReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    solve_s: float = 0.0
    thermal: Optional[object] = None          # kipi.dc.thermal.cosim.ThermalResult, if run
    report_path: Optional[object] = None      # HTML report written for this solve, if any
    corner: str = "typ"                       # "typ" or "max": kipi.dc.pi.corner
    headroom: List[RailHeadroom] = field(default_factory=list)   # rails with unknowns
    link_ratings: List[LinkRating] = field(default_factory=list)  # series parts vs rating

    def summary(self) -> str:
        """The loads in one sentence: how many are in range, out of range, not checked
        (no voltage window) or not solved, and which rails have currents not known."""
        n = len(self.loads)
        inside = sum(1 for l in self.loads if l.passed and not l.status)
        out = sum(1 for l in self.loads if l.passed is False)
        unjudged = sum(1 for l in self.loads if l.passed is None and not l.status)
        other = n - inside - out - unjudged
        s = (f"{n} loads solved: {inside} in range, {out} out of range, {unjudged} with no "
             "voltage window to check against")
        s += f", {other} not solved (see the Loads tab)." if other else "."
        rated = [k for k in self.link_ratings if k.within is not None]
        if self.link_ratings:
            over = sum(1 for k in rated if not k.within)
            s += (f" Series parts: {len(rated) - over} within their rated current, {over} "
                  f"above, {len(self.link_ratings) - len(rated)} with no rating to check.")
        rails = [h.group for h in self.headroom]
        if rails:
            s += (f" {len(rails)} rail{'s' if len(rails) > 1 else ''} with currents not "
                  f"known ({', '.join(rails)}): the Headroom tab says how much they may add.")
        return s

    def rail_target(self, vrm: VrmReport) -> Optional[float]:
        """The target voltage of the rail ``vrm`` drives, as its solve measured drops
        from; None if the rail was not solved."""
        g = self.groups.get(vrm.group)
        if g is None or g.kind != "rail":
            return None
        return g.result.fields.summary["v_ref"]

    def rail_drop(self, vrm: VrmReport) -> Tuple[Optional[float], Optional[float]]:
        """The largest drop anywhere on the copper of the rail ``vrm`` drives, below its
        target voltage: volts and % of it (#188). None, None if the rail was not solved."""
        g = self.groups.get(vrm.group)
        if g is None or g.kind != "rail":
            return None, None
        s = g.result.fields.summary
        drop, ref = s["max_drop"], s["v_ref"]
        return drop, (100.0 * drop / abs(ref) if ref else None)
