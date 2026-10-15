"""The user's power-integrity setup: VRMs, loads and DC connections.

Everything here is entered by the user; nothing is inferred from the board:

* **DC connections** (series resistors, ferrites, fuses, load switches...) join nets.
  Nets joined this way form one *group* that is simulated as a single problem.
* **VRMs** drive one group from their output pins. A group may have only one VRM.
  A VRM's input pins (LDO / switcher) make it a load on the group feeding it; that is
  how the power tree is defined. A multi-phase VRM has output, input and ground pins
  per phase; the phases share its output current equally.
* **Loads** draw a current from their supply pins, optionally returning it through
  ground pins.

Pins are referenced by footprint reference + pad numbers on a net, so a setup
survives re-extracting the board from KiCad (pad ids are not stable across sessions).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from kipi.solver.setup import SolveSettings
from kipi.thermal.model import ThermalSettings

FORMAT_VERSION = 1
VRM_KINDS = ("source", "ldo", "smps")


@dataclass
class PinRef:
    """Pads of footprint ``ref`` on ``net``; an empty ``pins`` list means all of them."""
    ref: str
    net: str
    pins: List[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.ref}[{','.join(self.pins)}]" if self.pins else self.ref


@dataclass
class VrmPhase:
    """The pins of one phase of a multi-phase VRM."""
    output: PinRef
    input: Optional[PinRef] = None
    ground: Optional[PinRef] = None


@dataclass
class Vrm:
    name: str
    output: PinRef                       # phase 1's pins: output, input, ground
    voltage: float
    kind: str = "source"                 # "source" (ideal supply), "ldo", "smps"
    efficiency: float = 0.9              # switchers: P_in = P_out / efficiency
    quiescent_a: float = 0.0             # extra input current (LDO / switcher)
    input: Optional[PinRef] = None       # required for "ldo" and "smps"
    sense: Optional[PinRef] = None       # remote sense: regulate the voltage here
    ground: Optional[PinRef] = None      # ground pins (enables ground-net simulation)
    ground_sense: Optional[PinRef] = None  # negative remote sense: regulate against these
    # Phases 2..N of a multi-phase VRM (empty = single phase). Each phase carries
    # 1/N of the output current; sense and ground sense are shared by all phases.
    phases: List[VrmPhase] = field(default_factory=list)
    # PASS if the total output current (all phases) is at most this many amps.
    current_limit_a: Optional[float] = None
    dropout_v: float = 0.0               # LDO: least input-output difference it regulates at
    vin_min: Optional[float] = None      # input range it regulates over (LDO / switcher)
    vin_max: Optional[float] = None

    def all_phases(self) -> List[VrmPhase]:
        return [VrmPhase(self.output, self.input, self.ground), *self.phases]

    def pin_refs(self) -> List[PinRef]:
        """Every pin reference of the VRM, all phases included."""
        refs = [self.sense, self.ground_sense]
        for ph in self.all_phases():
            refs.extend((ph.output, ph.input, ph.ground))
        return [r for r in refs if r]


@dataclass
class PinGroup:
    """Some of a load's supply pins drawing their own share of current."""
    pins: List[str]
    current: float
    name: str = ""


@dataclass
class Load:
    name: str
    supply: PinRef
    current: float
    ground: Optional[PinRef] = None
    tolerance_pct: Optional[float] = None   # pass if every pin is within +/- this % of nominal
    # Empty: ``current`` is split equally across the supply pins. Otherwise each group
    # of supply pins draws its own current and the load's total is their sum.
    groups: List[PinGroup] = field(default_factory=list)
    # Heat the part puts into the board. None: all the power it takes from its supply
    # pins (V x I). Set it for a part that passes power on, such as a load switch or an
    # LED or motor driver, whose own loss is far less than what flows through it.
    dissipation_w: Optional[float] = None

    def total_current(self) -> float:
        return sum(g.current for g in self.groups) if self.groups else self.current


@dataclass
class DcLink:
    name: str
    side_a: PinRef
    side_b: PinRef
    resistance: float                       # ohms; 0 = short


@dataclass
class PISetup:
    vrms: List[Vrm] = field(default_factory=list)
    loads: List[Load] = field(default_factory=list)
    links: List[DcLink] = field(default_factory=list)
    settings: SolveSettings = field(default_factory=SolveSettings)
    via_current_limit_a: Optional[float] = None   # vias above this are flagged FAIL
    thermal: ThermalSettings = field(default_factory=ThermalSettings)   # off by default
    html_report: bool = False                     # write an HTML report after solving
    # Nets ticked in the Board panel (the nets in scope). None = never chosen, so the
    # GUI guesses the main power and ground planes; [] = the user unticked them all.
    nets: Optional[List[str]] = None

    def pin_nets(self) -> set:
        """Nets the VRMs, loads and DC connections have pins on."""
        refs = [r for v in self.vrms for r in v.pin_refs()]
        refs += [r for ld in self.loads for r in (ld.supply, ld.ground) if r]
        refs += [r for k in self.links for r in (k.side_a, k.side_b) if r]
        return {r.net for r in refs}

    # ------------------------------------------------------------------ JSON
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["format_version"] = FORMAT_VERSION
        return d

    @staticmethod
    def from_dict(d: dict) -> "PISetup":
        if d.get("format_version", 1) > FORMAT_VERSION:
            raise ValueError("KiPI setup file is from a newer version of KiPI")

        def pin(x):
            return PinRef(x["ref"], x["net"], list(x.get("pins", []))) if x else None

        known = {f.name for f in dataclasses.fields(SolveSettings)}
        return PISetup(
            vrms=[Vrm(name=v["name"], output=pin(v["output"]), voltage=v["voltage"],
                      kind=v.get("kind", "source"), efficiency=v.get("efficiency", 0.9),
                      quiescent_a=v.get("quiescent_a", 0.0), input=pin(v.get("input")),
                      sense=pin(v.get("sense")), ground=pin(v.get("ground")),
                      ground_sense=pin(v.get("ground_sense")),
                      phases=[VrmPhase(pin(ph["output"]), pin(ph.get("input")),
                                       pin(ph.get("ground"))) for ph in v.get("phases", [])],
                      current_limit_a=v.get("current_limit_a"),
                      dropout_v=v.get("dropout_v", 0.0), vin_min=v.get("vin_min"),
                      vin_max=v.get("vin_max"))
                  for v in d.get("vrms", [])],
            loads=[Load(name=l["name"], supply=pin(l["supply"]), current=l["current"],
                        ground=pin(l.get("ground")), tolerance_pct=l.get("tolerance_pct"),
                        groups=[PinGroup(list(g["pins"]), g["current"], g.get("name", ""))
                                for g in l.get("groups", [])],
                        dissipation_w=l.get("dissipation_w"))
                   for l in d.get("loads", [])],
            links=[DcLink(name=k["name"], side_a=pin(k["side_a"]), side_b=pin(k["side_b"]),
                          resistance=k["resistance"]) for k in d.get("links", [])],
            via_current_limit_a=d.get("via_current_limit_a"),
            thermal=ThermalSettings(**{k: v for k, v in d.get("thermal", {}).items()
                                       if k in {f.name for f in dataclasses.fields(ThermalSettings)}}),
            html_report=bool(d.get("html_report", False)),
            nets=list(d["nets"]) if d.get("nets") is not None else None,
            settings=_settings(d.get("settings", {}), known),
        )

    def save(self, path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(path) -> "PISetup":
        return PISetup.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _settings(d: dict, known) -> SolveSettings:
    """Solve settings from a setup file. A file from before via rules carries one
    "plating_mm" for every hole: it becomes the plating of every default rule."""
    s = SolveSettings(**{k: v for k, v in d.items() if k in known})
    if "plating_mm" in d and "via_rules" not in d:
        for r in s.via_rules:
            r.plating_mm = float(d["plating_mm"])
    return s


def setup_path(board_file_or_name: str, project_dir: str) -> Path:
    """Where a board's setup lives: ``<project dir>/<board stem>.kipi-setup.json``."""
    stem = Path(board_file_or_name).stem
    return Path(project_dir or ".") / f"{stem}.kipi-setup.json"


# ---------------------------------------------------------------------------
# Groups of nets joined by DC connections
# ---------------------------------------------------------------------------

def net_groups(setup: PISetup, extra_nets=()) -> Dict[str, frozenset]:
    """Map every net mentioned in the setup to its group (nets joined by DC links)."""
    parent: Dict[str, str] = {}

    def find(n):
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a, b):
        parent[find(a)] = find(b)

    nets = set(extra_nets)
    for v in setup.vrms:
        nets.update(p.net for p in v.pin_refs())
    for l in setup.loads:
        nets.add(l.supply.net)
        if l.ground:
            nets.add(l.ground.net)
    for k in setup.links:
        nets.update((k.side_a.net, k.side_b.net))
        union(k.side_a.net, k.side_b.net)
    groups: Dict[str, set] = {}
    for n in nets:
        groups.setdefault(find(n), set()).add(n)
    return {n: frozenset(groups[find(n)]) for n in nets}


def group_name(group) -> str:
    return " + ".join(sorted(group))


def _overlaps(a: PinRef, b: PinRef) -> bool:
    """Whether two pin references share a pad (an empty pin list means all pads)."""
    if a.ref != b.ref or a.net != b.net:
        return False
    return not a.pins or not b.pins or bool(set(a.pins) & set(b.pins))


def _phase_errors(v: Vrm, groups) -> List[str]:
    if not v.phases:
        return []
    errors: List[str] = []
    phases = v.all_phases()
    for i, ph in enumerate(phases[1:], start=2):
        if groups[ph.output.net] != groups[v.output.net]:
            errors.append(f"VRM {v.name}: phase {i} output must be on the same rail as "
                          "phase 1")
        if v.kind in ("ldo", "smps") and ph.input is None:
            errors.append(f"VRM {v.name}: phase {i} needs its input pins set")
        elif ph.input and v.input and groups[ph.input.net] != groups[v.input.net]:
            errors.append(f"VRM {v.name}: phase {i} input must be on the same rail as "
                          "phase 1's input")
        if bool(ph.ground) != bool(v.ground):
            errors.append(f"VRM {v.name}: set ground pins on every phase or on none")
        elif ph.ground and groups[ph.ground.net] != groups[v.ground.net]:
            errors.append(f"VRM {v.name}: phase {i} ground must be on the same ground as "
                          "phase 1")
    for what in ("output", "input", "ground"):
        refs = [(i, getattr(ph, what)) for i, ph in enumerate(phases, start=1)]
        refs = [(i, r) for i, r in refs if r]
        for a in range(len(refs)):
            for b in range(a + 1, len(refs)):
                if _overlaps(refs[a][1], refs[b][1]):
                    errors.append(f"VRM {v.name}: phases {refs[a][0]} and {refs[b][0]} "
                                  f"share {what} pins; pick each phase's own pins")
    return errors


def validate(setup: PISetup) -> List[str]:
    """Problems that make the setup unsolvable (empty list = OK)."""
    errors: List[str] = []
    names = [x.name for x in [*setup.vrms, *setup.loads, *setup.links]]
    from kipi.solver.via_barrel import rule_errors
    errors.extend(rule_errors(setup.settings.via_rules))
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"Names must be unique: {', '.join(dupes)}")
    groups = net_groups(setup)

    by_group: Dict[frozenset, List[str]] = {}
    ground_groups = set()
    for v in setup.vrms:
        by_group.setdefault(groups[v.output.net], []).append(v.name)
        if v.kind not in VRM_KINDS:
            errors.append(f"VRM {v.name}: unknown type {v.kind!r}")
        if v.kind in ("ldo", "smps") and v.input is None:
            errors.append(f"VRM {v.name}: an {v.kind.upper()} needs its input pins set "
                          "(that is what connects it into the power tree)")
        if v.kind == "smps" and not (0 < v.efficiency <= 1):
            errors.append(f"VRM {v.name}: efficiency must be between 0 and 1")
        if v.current_limit_a is not None and not v.current_limit_a > 0:
            errors.append(f"VRM {v.name}: the output current limit must be positive")
        if v.quiescent_a < 0 or v.dropout_v < 0:
            errors.append(f"VRM {v.name}: quiescent current and dropout cannot be negative")
        if v.vin_min is not None and v.vin_max is not None and v.vin_min > v.vin_max:
            errors.append(f"VRM {v.name}: input range minimum is above its maximum")
        if v.sense and groups[v.sense.net] != groups[v.output.net]:
            errors.append(f"VRM {v.name}: sense pins must be on the output net (or nets "
                          "DC-connected to it)")
        if v.ground_sense and not v.ground:
            errors.append(f"VRM {v.name}: ground sense needs the VRM's ground pins set "
                          "(that is what makes its ground net simulated)")
        elif v.ground_sense and groups[v.ground_sense.net] != groups[v.ground.net]:
            errors.append(f"VRM {v.name}: ground sense pins must be on the VRM's ground net "
                          "(or nets DC-connected to it)")
        if v.input and groups[v.input.net] == groups[v.output.net]:
            errors.append(f"VRM {v.name}: input and output are on the same net group")
        if v.ground:
            ground_groups.add(groups[v.ground.net])
        errors.extend(_phase_errors(v, groups))
    for g, vs in by_group.items():
        if len(vs) > 1:
            errors.append(f"Only one VRM may drive a net group; {group_name(g)} has "
                          f"{', '.join(vs)}")

    rail_groups = set(by_group)
    for l in setup.loads:
        if l.tolerance_pct is not None and l.tolerance_pct < 0:
            errors.append(f"Load {l.name}: max drop cannot be negative")
        if l.dissipation_w is not None and l.dissipation_w < 0:
            errors.append(f"Load {l.name}: heat cannot be negative")
        if l.groups:
            seen_pins: Dict[str, int] = {}
            for gi, g in enumerate(l.groups):
                if not g.pins:
                    errors.append(f"Load {l.name}: pin group {g.name or gi + 1} has no pins")
                for pin in g.pins:
                    if pin in seen_pins:
                        errors.append(f"Load {l.name}: pin {pin} is in more than one group")
                    seen_pins[pin] = gi
            if l.supply.pins:
                outside = sorted(set(seen_pins) - set(l.supply.pins))
                if outside:
                    errors.append(f"Load {l.name}: group pins {', '.join(outside)} are not "
                                  "among its supply pins")
        if groups[l.supply.net] not in rail_groups:
            errors.append(f"Load {l.name}: no VRM drives {group_name(groups[l.supply.net])}")
        if l.ground:
            ground_groups.add(groups[l.ground.net])
    for v in setup.vrms:
        if v.input and groups[v.input.net] not in rail_groups:
            errors.append(f"VRM {v.name}: no VRM drives its input "
                          f"({group_name(groups[v.input.net])})")
    for k in setup.links:
        if k.resistance < 0:
            errors.append(f"DC connection {k.name}: resistance cannot be negative")
    for g in ground_groups:
        if g in rail_groups:
            errors.append(f"{group_name(g)} is used both as a supply and as ground")
        if not any(v.kind == "source" and v.ground and groups[v.ground.net] == g
                   for v in setup.vrms):
            errors.append(f"Ground {group_name(g)} needs a reference: set the ground pins of "
                          "the source VRM that returns current through it")

    # The power tree must not loop (A feeds B feeds A).
    feeds = {groups[v.output.net]: set() for v in setup.vrms}
    for v in setup.vrms:
        if v.input:
            feeds.setdefault(groups[v.input.net], set()).add(groups[v.output.net])
    state: Dict[frozenset, int] = {}

    def visit(g):
        if state.get(g) == 1:
            return True
        if state.get(g) == 2:
            return False
        state[g] = 1
        cyc = any(visit(h) for h in feeds.get(g, ()))
        state[g] = 2
        return cyc
    if any(visit(g) for g in list(feeds)):
        errors.append("The power tree has a loop (a VRM feeds, directly or indirectly, its "
                      "own input)")
    s = setup.settings
    if not s.rho_ohm_mm > 0:
        errors.append("Copper resistivity must be positive")
    bad = sorted(n for n, r in s.rho_by_layer.items() if not r > 0)
    if bad:
        errors.append(f"Resistivity must be positive on {', '.join(bad)}")
    return errors
