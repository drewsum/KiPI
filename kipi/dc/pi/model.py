"""The user's power-integrity setup: VRMs, loads and series links, plus the AC setup.

Everything here is entered by the user. The editors pre-fill some fields with guesses
from the board as parts are picked (:mod:`kipi.dc.pi.autofill`), but the setup holds only
what the user kept, and nothing is inferred at solve time:

* **Series links** (series resistors, ferrites, fuses, load switches...) join nets.
  Nets joined this way form one *group* that is simulated as a single problem.
* **VRMs** drive one group from their output pins. A group may have only one VRM.
  A VRM's input pins (LDO / switcher) make it a load on the group feeding it; that is
  how the power tree is defined. A multi-phase VRM has output, input and ground pins
  per phase; the phases share its output current equally.
* **Loads** draw a current from their supply pins, optionally returning it through
  ground pins.
* **Excluded nets** leave their net groups out of the DC solve (see :func:`active_setup`),
  so part of a board can be solved before the rest is modeled or routed.
* **AC setup** (:mod:`kipi.ac.model`): capacitor models, probe points and sweep settings.
  VRMs and series links are shared with the AC analysis; some of their fields apply to
  only one of the two (see each field).
* **Coupled Noise setup** (:mod:`kipi.noise.model`): aggressors (a switching VRM's
  frequency, edges and power parts) and their settings. It uses the VRMs and the
  capacitor models above.

Pins are referenced by footprint reference + pad numbers on a net, so a setup
survives re-extracting the board from KiCad (pad ids are not stable across sessions).
"""

from __future__ import annotations

import copy
import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kipi.ac.model import (
    DEFAULT_KEPT_AT_RATED_PCT, ACSettings, CapModel, Probe, report_format,
)
from kipi.dc.solver.setup import SolveSettings
from kipi.dc.thermal.model import ThermalSettings
from kipi.noise.model import (
    Aggressor, NoiseSettings, Victim, aggressor_from_dict, settings_from_dict as noise_settings,
    victim_from_dict,
)

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
class SensePoint:
    """One mesh remote sense point (#93): sense pins on the rail and, optionally, the
    ground sense pins the rail's voltage there is held above."""
    sense: Optional[PinRef] = None
    ground_sense: Optional[PinRef] = None


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
    # The input range in % from the input rail's nominal voltage (that of the VRM feeding
    # it, see rail_nominal), e.g. -10 / +10, or with vin_dv, in volts from it (e.g. -0.5 /
    # +0.5), instead of in volts (#131).
    vin_pct: bool = False
    vin_dv: bool = False
    # DC accuracy of the set voltage (LDO / switcher), either end None: in % of it (e.g.
    # -1 / +1), with acc_dv in volts from it (e.g. -0.01 / +0.01), or with neither, the
    # output's lowest and highest volts. The target calculator takes its larger side off
    # the AC tolerance.
    acc_min: Optional[float] = None
    acc_max: Optional[float] = None
    acc_pct: bool = True
    acc_dv: bool = False
    # Load line (#94): the regulated voltage droops by this many milliohms times the
    # output current actually drawn (0 = none).
    load_line_mohm: float = 0.0
    # AC only: control loop bandwidth, kHz (None = not given).
    bandwidth_khz: Optional[float] = None
    # Mesh remote sense (#93, DC only, off by default): every sense point's pins are held
    # at the set voltage (less the load line, above that point's ground sense pins), the
    # VRM's current entering there; its output pins only measure. Point 1 is sense and
    # ground_sense above, points 2..N these.
    mesh_sense: bool = False
    sense_points: List[SensePoint] = field(default_factory=list)

    def all_phases(self) -> List[VrmPhase]:
        return [VrmPhase(self.output, self.input, self.ground), *self.phases]

    def all_sense_points(self) -> List[SensePoint]:
        """Every sense point: point 1 (sense, ground_sense), then the mesh's others when
        mesh sense is on."""
        return [SensePoint(self.sense, self.ground_sense),
                *(self.sense_points if self.mesh_sense else [])]

    def pin_refs(self) -> List[PinRef]:
        """Every pin reference of the VRM, all phases included."""
        refs = [r for sp in self.all_sense_points() for r in (sp.sense, sp.ground_sense)]
        for ph in self.all_phases():
            refs.extend((ph.output, ph.input, ph.ground))
        return [r for r in refs if r]

    def has_accuracy(self) -> bool:
        return self.acc_min is not None or self.acc_max is not None

    def accuracy_label(self) -> str:
        return tolerance_label(self.acc_min, self.acc_max, self.acc_pct, self.acc_dv)

    def accuracy_v(self) -> Optional[float]:
        """The larger side of the DC accuracy, volts from the set voltage; None if not set."""
        lo, hi = range_volts(self.voltage, self.acc_min, self.acc_max,
                             range_unit(self.acc_pct, self.acc_dv))
        sides = [abs(x - self.voltage) for x in (lo, hi) if x is not None]
        return max(sides) if sides else None


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
    # PASS if every pin is within this range, either end None for no limit: in % from
    # nominal, the set point of the VRM driving the rail (e.g. -5 / +5), with tol_dv in
    # volts from nominal (e.g. -0.15 / +0.15, #131), or with neither, in volts.
    tol_min: Optional[float] = None
    tol_max: Optional[float] = None
    tol_pct: bool = True
    tol_dv: bool = False
    # Empty: ``current`` is split equally across the supply pins. Otherwise each group
    # of supply pins draws its own current and the load's total is their sum.
    groups: List[PinGroup] = field(default_factory=list)

    def total_current(self) -> float:
        return sum(g.current for g in self.groups) if self.groups else self.current

    def has_tolerance(self) -> bool:
        return self.tol_min is not None or self.tol_max is not None

    def limits(self, nominal: float) -> Tuple[Optional[float], Optional[float]]:
        """The tolerance as (lowest, highest) volts on a rail at ``nominal``, either None."""
        unit = range_unit(self.tol_pct, self.tol_dv)
        if unit != VOLTS and not nominal:
            return None, None
        return range_volts(nominal, self.tol_min, self.tol_max, unit)

    def tolerance_label(self) -> str:
        return tolerance_label(self.tol_min, self.tol_max, self.tol_pct, self.tol_dv)

    def __setstate__(self, state):
        # Setups pickled in results files before asymmetric tolerances: +/- one %.
        t = state.pop("tolerance_pct", None)
        state = {"tol_min": None if t is None else -t, "tol_max": t, "tol_pct": True, **state}
        self.__dict__.update(state)


def pct_to_volts(nominal: float, lo: Optional[float], hi: Optional[float]):
    """A (min, max) range in % from ``nominal`` as volts, either None. -10 % of a negative
    rail is its higher end."""
    lo, hi = (None if p is None else nominal * (1 + p / 100) for p in (lo, hi))
    return (hi, lo) if nominal < 0 else (lo, hi)


def volts_to_pct(nominal: float, lo: Optional[float], hi: Optional[float]):
    """The inverse of :func:`pct_to_volts`."""
    lo, hi = (None if x is None else (x / nominal - 1) * 100 for x in (lo, hi))
    return (hi, lo) if nominal < 0 else (lo, hi)


# A range's units (#131): volts, volts from nominal, or % from nominal.
VOLTS, DELTA_VOLTS, PERCENT = "V", "±V", "%"


def range_unit(pct: bool, dv: bool = False) -> str:
    """The unit of a range saved as its ``*_pct`` and ``*_dv`` flags."""
    return PERCENT if pct else DELTA_VOLTS if dv else VOLTS


def range_volts(nominal: Optional[float], lo: Optional[float], hi: Optional[float],
                unit: str):
    """A (min, max) range in ``unit`` as volts, either None (both None when it is
    relative and ``nominal`` is None). Volts from nominal add to it, so min is the lower
    end on a negative rail too."""
    if unit == VOLTS:
        return lo, hi
    if nominal is None:
        return None, None
    if unit == PERCENT:
        return pct_to_volts(nominal, lo, hi)
    return tuple(None if x is None else nominal + x for x in (lo, hi))


def volts_in(nominal: Optional[float], lo: Optional[float], hi: Optional[float], unit: str):
    """The inverse of :func:`range_volts`."""
    if unit == VOLTS:
        return lo, hi
    if nominal is None:
        return None, None
    if unit == PERCENT:
        return volts_to_pct(nominal, lo, hi)
    return tuple(None if x is None else x - nominal for x in (lo, hi))


def tolerance_label(lo: Optional[float], hi: Optional[float], pct: bool,
                    dv: bool = False) -> str:
    """A range for tables: "±5 %", "-3 / +5 %", "±0.1 V", "3.135 to 3.465 V",
    "min 3.2 V"..."""
    if lo is None and hi is None:
        return ""
    if pct or dv:
        u = "%" if pct else "V"
        if lo is not None and hi is not None:
            return f"±{hi:g} {u}" if lo == -hi else f"{lo:+g} / {hi:+g} {u}"
        return f"min {lo:+g} {u}" if hi is None else f"max {hi:+g} {u}"
    if lo is not None and hi is not None:
        return f"{lo:g} to {hi:g} V"
    return f"min {lo:g} V" if hi is None else f"max {hi:g} V"


@dataclass
class DcLink:
    name: str
    side_a: PinRef
    side_b: PinRef
    resistance: float                       # ohms; 0 = short (DC and AC)
    esl_h: Optional[float] = None           # series inductance, henries (AC only)


DEFAULT_VIA_CURRENT_LIMIT_A = 1.0


@dataclass
class PISetup:
    vrms: List[Vrm] = field(default_factory=list)
    loads: List[Load] = field(default_factory=list)
    links: List[DcLink] = field(default_factory=list)
    settings: SolveSettings = field(default_factory=SolveSettings)
    # Vias above this are flagged FAIL; None = no limit. 1 A is a placeholder that suits a
    # typical 0.3 mm via (#114).
    via_current_limit_a: Optional[float] = DEFAULT_VIA_CURRENT_LIMIT_A
    thermal: ThermalSettings = field(default_factory=ThermalSettings)   # off by default
    html_report: bool = False                     # write a report after solving
    report_format: str = "html"                   # "html" or "pdf" (#119)
    # Nets ticked in the Board panel (the nets in scope). None = never chosen, so the
    # GUI guesses the main power and ground planes; [] = the user unticked them all.
    nets: Optional[List[str]] = None
    # DC: net groups with any of these nets are left out of the solve (see active_setup).
    excluded_nets: List[str] = field(default_factory=list)
    # Stackup thicknesses edited in the Stackup pane (kipi.core.model.stackup.to_setup);
    # None = use the board's own.
    stackup: Optional[Dict] = None
    # The thermal settings' Laminate k from a setup made before 0.8.0, W/(m K), for the
    # GUI to move into the stackup (#90); never saved.
    legacy_laminate_k: float = field(default=0.0, repr=False, compare=False)
    # AC analysis
    caps: List[CapModel] = field(default_factory=list)
    probes: List[Probe] = field(default_factory=list)
    ac: ACSettings = field(default_factory=ACSettings)
    # Coupled Noise analysis (#69)
    aggressors: List[Aggressor] = field(default_factory=list)
    victims: List[Victim] = field(default_factory=list)
    noise: NoiseSettings = field(default_factory=NoiseSettings)

    def pin_nets(self) -> set:
        """Nets the VRMs, loads, series links and probes have pins on."""
        refs = [r for v in self.vrms for r in v.pin_refs()]
        refs += [r for ld in self.loads for r in (ld.supply, ld.ground) if r]
        refs += [r for k in self.links for r in (k.side_a, k.side_b) if r]
        refs += [r for p in self.probes for r in (p.rail, p.ground) if r]
        refs += self._aggressor_pins()
        return {r.net for r in refs}

    def part_refs(self) -> set:
        """Footprints given a model: VRMs, loads, series links and capacitor models."""
        refs = [r for v in self.vrms for r in v.pin_refs()]
        refs += [r for ld in self.loads for r in (ld.supply, ld.ground) if r]
        refs += [r for k in self.links for r in (k.side_a, k.side_b) if r]
        refs += self._aggressor_pins()
        out = {r.ref for r in refs} | {ref for m in self.caps for ref in m.refs}
        out |= {ph.inductor for a in self.aggressors for ph in a.phases}
        out.discard("")
        return out

    def _aggressor_pins(self) -> List[PinRef]:
        return ([r for a in self.aggressors for ph in a.phases
                 for r in (ph.high_side, ph.low_side) if r]
                + [r for v in self.victims for r in (v.driver, v.receiver) if r])

    # ------------------------------------------------------------------ file
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d.pop("legacy_laminate_k")
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
                      vin_max=v.get("vin_max"), vin_pct=v.get("vin_pct", False),
                      vin_dv=v.get("vin_dv", False),
                      acc_min=v.get("acc_min"), acc_max=v.get("acc_max"),
                      acc_pct=v.get("acc_pct", True), acc_dv=v.get("acc_dv", False),
                      load_line_mohm=v.get("load_line_mohm", 0.0),
                      bandwidth_khz=v.get("bandwidth_khz"),
                      mesh_sense=v.get("mesh_sense", False),
                      sense_points=[SensePoint(pin(sp.get("sense")),
                                               pin(sp.get("ground_sense")))
                                    for sp in v.get("sense_points", [])])
                  for v in d.get("vrms", [])],
            loads=[Load(name=l["name"], supply=pin(l["supply"]), current=l["current"],
                        ground=pin(l.get("ground")), **_load_tolerance(l),
                        groups=[PinGroup(list(g["pins"]), g["current"], g.get("name", ""))
                                for g in l.get("groups", [])])
                   for l in d.get("loads", [])],
            links=[DcLink(name=k["name"], side_a=pin(k["side_a"]), side_b=pin(k["side_b"]),
                          resistance=k["resistance"], esl_h=k.get("esl_h"))
                   for k in d.get("links", [])],
            via_current_limit_a=d.get("via_current_limit_a", DEFAULT_VIA_CURRENT_LIMIT_A),
            thermal=ThermalSettings(**{k: v for k, v in d.get("thermal", {}).items()
                                       if k in {f.name for f in dataclasses.fields(ThermalSettings)}}),
            html_report=bool(d.get("html_report", False)),
            report_format=report_format(d.get("report_format")),
            nets=list(d["nets"]) if d.get("nets") is not None else None,
            excluded_nets=list(d.get("excluded_nets", [])),
            stackup=d.get("stackup") or None,
            legacy_laminate_k=float(d.get("thermal", {}).get("laminate_k_w_mk") or 0.0),
            settings=_settings(d.get("settings", {}), known),
            caps=[CapModel(c["name"], c.get("capacitance_f", 0.0), c.get("esl_h", 0.0),
                           c.get("esr_ohm", 0.0), list(c.get("refs", [])),
                           c.get("rated_v"),
                           c.get("kept_at_rated_pct", DEFAULT_KEPT_AT_RATED_PCT),
                           c.get("dielectric", ""), c.get("aging_pct_per_decade", 0.0),
                           c.get("mfg_tolerance_pct"))
                  for c in d.get("caps", [])],
            probes=[Probe(p["name"], pin(p["rail"]), pin(p["ground"]), p.get("target_ohm"),
                          p.get("target_hz"), bool(p.get("target_custom", False)),
                          [(float(f), float(z)) for f, z in p.get("target_points", [])])
                    for p in d.get("probes", [])],
            ac=ACSettings(**{k: v for k, v in d.get("ac", {}).items()
                             if k in {f.name for f in dataclasses.fields(ACSettings)}}),
            aggressors=[aggressor_from_dict(a, pin) for a in d.get("aggressors", [])],
            victims=[victim_from_dict(v, pin) for v in d.get("victims", [])],
            noise=noise_settings(d.get("noise", {})),
        )

    def save(self, path) -> Path:
        """Write the setup as YAML, to ``path`` with a ``.yml`` suffix in place of ``.json``
        (#76); the path written is returned. Only without PyYAML does it stay JSON."""
        from kipi.core import yaml_io
        path = Path(path)
        if yaml_io.available():
            if path.suffix.lower() == ".json":
                path = path.with_suffix(".yml")
            path.write_text(yaml_io.dumps(self.to_dict()), encoding="utf-8")
        else:
            path = path.with_suffix(".json") if is_yaml(path) else path
            path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(path) -> "PISetup":
        """A setup from YAML or, for setups saved before 0.8.0, JSON (deprecated)."""
        from kipi.core import yaml_io
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        if not is_yaml(path):
            return PISetup.from_dict(json.loads(text))
        if not yaml_io.available():
            raise ValueError("reading a .yml setup needs PyYAML; reinstall KiPI's "
                             "dependencies")
        return PISetup.from_dict(yaml_io.loads(text) or {})


SETUP_SUFFIX = ".kipi-setup.yml"
LEGACY_SETUP_SUFFIX = ".kipi-setup.json"       # before 0.8.0; still read (#76)


def is_yaml(path) -> bool:
    return Path(path).suffix.lower() in (".yml", ".yaml")


def _settings(d: dict, known) -> SolveSettings:
    """Solve settings from a setup file. A file from before via rules carries one
    "plating_mm" for every hole: it becomes the plating of every default rule."""
    s = SolveSettings(**{k: v for k, v in d.items() if k in known})
    if "plating_mm" in d and "via_rules" not in d:
        for r in s.via_rules:
            r.plating_mm = float(d["plating_mm"])
    return s


def setup_path(board_file_or_name: str, project_dir: str) -> Path:
    """Where a board's setup is saved: ``<project dir>/<board stem>.kipi-setup.yml``
    (``.json`` if PyYAML is missing)."""
    from kipi.core import yaml_io
    stem = Path(board_file_or_name).stem
    suffix = SETUP_SUFFIX if yaml_io.available() else LEGACY_SETUP_SUFFIX
    return Path(project_dir or ".") / f"{stem}{suffix}"


def find_setup(board_file_or_name: str, project_dir: str) -> Optional[Path]:
    """The board's saved setup: the YAML one, else one saved as JSON before 0.8.0, else
    None."""
    stem = Path(board_file_or_name).stem
    folder = Path(project_dir or ".")
    for suffix in (SETUP_SUFFIX, LEGACY_SETUP_SUFFIX):
        if (folder / f"{stem}{suffix}").is_file():
            return folder / f"{stem}{suffix}"
    return None


# ---------------------------------------------------------------------------
# Groups of nets joined by series links
# ---------------------------------------------------------------------------

def _load_tolerance(d: dict) -> dict:
    """A load's tolerance fields from a setup file; older files have +/- one %."""
    if "tol_min" in d or "tol_max" in d:
        return {"tol_min": d.get("tol_min"), "tol_max": d.get("tol_max"),
                "tol_pct": d.get("tol_pct", True), "tol_dv": d.get("tol_dv", False)}
    t = d.get("tolerance_pct")
    return {} if t is None else {"tol_min": -t, "tol_max": t}


def net_groups(setup: PISetup, extra_nets=()) -> Dict[str, frozenset]:
    """Map every net mentioned in the setup to its group (nets joined by series links)."""
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


def rail_nominal(setup: PISetup, net: Optional[str], skip: str = "",
                 groups=None) -> Optional[float]:
    """The nominal voltage of the rail ``net`` is on: the set voltage of a VRM (other than
    ``skip``) driving it. None if no VRM does."""
    if not net:
        return None
    groups = groups or net_groups(setup, [net])
    group = groups.get(net, frozenset([net]))
    return next((u.voltage for u in setup.vrms if u.name != skip and u.voltage
                 and any(ph.output.net in group for ph in u.all_phases())), None)


def vin_range(setup: PISetup, v: Vrm, groups=None) -> Tuple[Optional[float], Optional[float]]:
    """The VRM's input range in volts, (min, max), either None when not set. A range in %
    is taken from its input rail's nominal voltage; (None, None) without one."""
    unit = range_unit(v.vin_pct, v.vin_dv)
    if unit == VOLTS:
        return v.vin_min, v.vin_max
    nom = rail_nominal(setup, v.input.net if v.input else None, v.name, groups)
    return range_volts(nom, v.vin_min, v.vin_max, unit)


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
    from kipi.dc.solver.via_barrel import rule_errors
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
        if v.load_line_mohm < 0:
            errors.append(f"VRM {v.name}: the load line cannot be negative")
        if v.vin_min is not None and v.vin_max is not None and v.vin_min > v.vin_max:
            errors.append(f"VRM {v.name}: input range minimum is above its maximum")
        if v.acc_min is not None and v.acc_max is not None and v.acc_min > v.acc_max:
            errors.append(f"VRM {v.name}: DC accuracy minimum is above its maximum")
        if ((v.vin_pct or v.vin_dv) and v.kind != "source"
                and (v.vin_min, v.vin_max) != (None, None)
                and rail_nominal(setup, v.input.net if v.input else None, v.name,
                                 groups) is None):
            how = "in %" if v.vin_pct else "in volts from nominal"
            errors.append(f"VRM {v.name}: an input range {how} needs a VRM driving its "
                          "input rail, whose voltage it is taken from; give the range in "
                          "volts instead")
        points = v.all_sense_points()
        for k, sp in enumerate(points, 1):
            at = f" (sense point {k})" if v.mesh_sense else ""
            if v.mesh_sense and not sp.sense:
                errors.append(f"VRM {v.name}: mesh sense point {k} has no sense pins")
            if sp.sense and groups[sp.sense.net] != groups[v.output.net]:
                errors.append(f"VRM {v.name}{at}: sense pins must be on the output net (or "
                              "nets DC-connected to it)")
            if sp.ground_sense and not v.ground:
                errors.append(f"VRM {v.name}{at}: ground sense needs the VRM's ground pins "
                              "set (that is what makes its ground net simulated)")
            elif sp.ground_sense and groups[sp.ground_sense.net] != groups[v.ground.net]:
                errors.append(f"VRM {v.name}{at}: ground sense pins must be on the VRM's "
                              "ground net (or nets DC-connected to it)")
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
        if l.tol_min is not None and l.tol_max is not None and l.tol_min > l.tol_max:
            errors.append(f"Load {l.name}: tolerance minimum is above its maximum")
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
                          "the source VRM that returns current through it (and solve that "
                          "VRM's rail)")

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


# ---------------------------------------------------------------------------
# Solving only some net groups
# ---------------------------------------------------------------------------

def _supplied_current(setup: PISetup, groups, g, seen) -> float:
    """Current drawn from rail group ``g`` at nominal voltages, ignoring copper losses:
    its loads plus the input current of every VRM it feeds."""
    amps = sum(l.total_current() for l in setup.loads if groups[l.supply.net] == g)
    for v in setup.vrms:
        if v.input and groups[v.input.net] == g:
            amps += _estimated_input(setup, groups, v, seen)
    return amps


def _estimated_input(setup: PISetup, groups, v: Vrm, seen=frozenset()) -> float:
    """Input current of VRM ``v`` (all phases) from the nominal voltages of its rails."""
    if v.name in seen:                       # a power-tree loop; validate() reports it
        return 0.0
    i_out = _supplied_current(setup, groups, groups[v.output.net], seen | {v.name})
    if v.kind == "smps":
        feed = next((u for u in setup.vrms if groups[u.output.net] == groups[v.input.net]),
                    None)
        if feed is None or feed.voltage <= 0:
            return v.quiescent_a
        return v.voltage * i_out / (v.efficiency * feed.voltage) + v.quiescent_a
    return i_out + v.quiescent_a


def active_setup(setup: PISetup) -> Tuple[PISetup, List[str]]:
    """The part of ``setup`` that is DC-solved, and notes on what was left out.

    Net groups with an excluded net are dropped with their loads, VRMs and series
    links. Where the two halves meet:

    * A VRM whose output is excluded but whose input is solved becomes a constant load
      on its input pins (one per phase), drawing the input current its rail would need
      at nominal voltages, without that rail's copper losses.
    * A VRM whose input is excluded becomes an ideal source.
    * Ground pins on an excluded ground are dropped: that ground is not simulated.
    """
    if not setup.excluded_nets:
        return setup, []
    groups = net_groups(setup)
    off = set(setup.excluded_nets)

    def solved(net: str) -> bool:
        return not (groups[net] & off)

    def kept(ref: Optional[PinRef]) -> Optional[PinRef]:
        return ref if ref and solved(ref.net) else None

    out = copy.deepcopy(setup)
    out.excluded_nets = []
    skipped = sorted({group_name(g) for g in groups.values() if g & off})
    notes = [f"Not solved (excluded): {'; '.join(skipped)}"] if skipped else []
    out.links = [k for k in out.links if solved(k.side_a.net)]
    loads = []
    for l in out.loads:
        if solved(l.supply.net):
            l.ground = kept(l.ground)
            loads.append(l)
    vrms = []
    for v in out.vrms:
        phases = v.all_phases()
        if not solved(v.output.net):
            if v.input and solved(v.input.net):
                amps = _estimated_input(setup, groups, v)
                n = len(phases)
                for k, ph in enumerate(phases, start=1):
                    name = f"{v.name} (input)" if n == 1 else f"{v.name} (input, phase {k})"
                    loads.append(Load(name, ph.input, amps / n, ground=kept(ph.ground)))
                notes.append(f"{v.name}: its output ({group_name(groups[v.output.net])}) is "
                             f"not solved, so its input draws an estimated {amps:.4g} A "
                             "(nominal voltages, no copper losses on that rail)")
            continue
        if v.input and not solved(v.input.net):
            notes.append(f"{v.name}: its input ({group_name(groups[v.input.net])}) is not "
                         "solved, so it is treated as an ideal source")
            v.kind, v.input = "source", None
            for ph in v.phases:
                ph.input = None
        if v.ground and not solved(v.ground.net):
            v.ground = v.ground_sense = None
            for ph in v.phases:
                ph.ground = None
            for sp in v.sense_points:
                sp.ground_sense = None
        vrms.append(v)
    out.vrms, out.loads = vrms, loads
    return out, notes
