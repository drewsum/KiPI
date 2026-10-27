"""Load models (#17): what a part draws from each of its supplies, in a file of its own, to
use for every copy of the part and on other boards.

A model file is YAML, always (``.yml`` or ``.yaml``), and names one part:

.. code-block:: yaml

    kipi_load_model: 1
    part: XC7A35T-1CSG324C          # what it is a model of (free text)
    supplies:                       # one per supply rail of the part
      - name: VCCINT
        pins: [F7, F9, H7]          # its supply pins (optional)
        ground_pins: [A1, A10]      # its ground pins (optional)
        current_a: 1.2              # or pin_groups, each drawing its own current
        pin_groups: [{name: core, pins: [F7, F9], current_a: 0.8}]
        tolerance: {min: -3, max: 3, unit: "%"}     # total; unit V, ±V or %
        dc_tolerance: {min: -1.5, max: 1.5, unit: "%"}    # VRM accuracy + drop; V, ±V or %
        ac_tolerance: {min: -0.03, max: 0.03, unit: "±V"} # ripple + transient; ±V or %
        target: {ohm: 0.01, up_to_hz: 50.0e6}      # or {points: [[hz, ohm], ...]}
        load_step: {step_a: 0.6, slew_a_per_us: 5}

A HyperLynx PI model (``.hlpimodel``, #149) reads as a model too: its DC sink currents
pin by pin, as one supply split by net on the part it is applied to (see
:mod:`kipi.dc.pi.importers.hyperlynx_pimodel`).

Every key but ``name`` is optional, and only the ones given are applied. The DC loads take
the current, pin groups and the three tolerances (#174); the AC probes the target, or one
worked out from the load step as the target calculator does (with the tighter side of the
AC tolerance, else of the total tolerance). Older models gave the AC tolerance as
``ac_tolerance_mv`` in ``load_step``, read as that many mV either way. A model exported
from a setup holds every load and probe on one reference designator, one supply each, so
a part with several supplies keeps them all. Pins make a model usable to add the loads
and probes for another copy of the part.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kipi.dc.pi.model import (
    DELTA_VOLTS, PERCENT, VOLTS, Load, PinGroup, PinRef, PISetup, is_yaml, range_unit,
    rail_nominal, range_volts,
)

FORMAT = 1
KEY = "kipi_load_model"
SUFFIX = ".kipi-load.yml"
UNITS = (VOLTS, DELTA_VOLTS, PERCENT)
AC_UNITS = (DELTA_VOLTS, PERCENT)                  # a swing about the DC voltage


class ModelError(ValueError):
    """A file that is not a load model, or one KiPI cannot read."""


@dataclass
class SupplyModel:
    """What a part draws from one of its supplies. None or empty: not given."""
    name: str
    pins: List[str] = field(default_factory=list)
    ground_pins: List[str] = field(default_factory=list)
    current_a: Optional[float] = None
    groups: List[PinGroup] = field(default_factory=list)
    tol_min: Optional[float] = None
    tol_max: Optional[float] = None
    tol_unit: str = PERCENT
    target_ohm: Optional[float] = None
    target_hz: Optional[float] = None
    target_points: List[Tuple[float, float]] = field(default_factory=list)
    step_a: Optional[float] = None
    slew_a_per_us: Optional[float] = None
    # The DC and AC shares of the tolerance (#174), as a load has them.
    dc_tol_min: Optional[float] = None
    dc_tol_max: Optional[float] = None
    dc_tol_unit: str = PERCENT
    ac_tol_min: Optional[float] = None
    ac_tol_max: Optional[float] = None
    ac_tol_unit: str = PERCENT
    # Pins on several nets, one load each (a HyperLynx model's, which names no nets).
    split_by_net: bool = False

    def has_tolerance(self) -> bool:
        return self.tol_min is not None or self.tol_max is not None

    def has_dc_tolerance(self) -> bool:
        return self.dc_tol_min is not None or self.dc_tol_max is not None

    def has_ac_tolerance(self) -> bool:
        return self.ac_tol_min is not None or self.ac_tol_max is not None

    def has_target(self) -> bool:
        return bool(self.target_points) or self.target_ohm is not None or (
            self.step_a is not None and self.slew_a_per_us is not None)

    def summary(self) -> str:
        """One line for lists: what the supply sets."""
        from kipi.dc.pi.model import tolerance_label
        bits = []
        if self.groups:
            bits.append(f"{sum(g.current for g in self.groups):g} A in "
                        f"{len(self.groups)} groups")
        elif self.current_a is not None:
            bits.append(f"{self.current_a:g} A")
        for what, lo, hi, unit in (("", self.tol_min, self.tol_max, self.tol_unit),
                                   ("DC ", self.dc_tol_min, self.dc_tol_max,
                                    self.dc_tol_unit),
                                   ("AC ", self.ac_tol_min, self.ac_tol_max,
                                    self.ac_tol_unit)):
            if lo is not None or hi is not None:
                bits.append(what + tolerance_label(lo, hi, unit == PERCENT,
                                                   unit == DELTA_VOLTS))
        if self.target_points:
            bits.append(f"target mask ({len(self.target_points)} points)")
        elif self.target_ohm is not None:
            bits.append(f"target {self.target_ohm * 1e3:g} mΩ")
        elif self.step_a is not None and self.slew_a_per_us is not None:
            bits.append(f"step {self.step_a:g} A at {self.slew_a_per_us:g} A/µs")
        if self.pins:
            bits.append(f"{len(self.pins)} pins")
        return ", ".join(bits) or "nothing set"


@dataclass
class LoadModel:
    part: str = ""
    supplies: List[SupplyModel] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)     # what reading it left out; not saved

    # ------------------------------------------------------------------ file
    def to_dict(self) -> dict:
        out: Dict[str, object] = {KEY: FORMAT}
        if self.part:
            out["part"] = self.part
        out["supplies"] = [_supply_dict(s) for s in self.supplies]
        return out

    @staticmethod
    def from_dict(d) -> "LoadModel":
        if not isinstance(d, dict) or KEY not in d:
            raise ModelError("not a KiPI load model (it has no kipi_load_model key)")
        if not isinstance(d[KEY], int) or d[KEY] > FORMAT:
            raise ModelError("the load model is from a newer version of KiPI")
        supplies = d.get("supplies") or []
        if not isinstance(supplies, list):
            raise ModelError("supplies must be a list")
        return LoadModel(str(d.get("part") or ""), [_supply(s, i) for i, s in
                                                     enumerate(supplies)])

    def save(self, path) -> Path:
        """Write the model as YAML, to ``path`` with ``.yml`` added unless it ends in
        ``.yml`` or ``.yaml``; the path written."""
        path = Path(path)
        if not is_yaml(path):
            path = path.with_name(path.name + ".yml")
        path.write_text(_yaml().dumps(self.to_dict()), encoding="utf-8")
        return path

    @staticmethod
    def load(path) -> "LoadModel":
        path = Path(path)
        from kipi.dc.pi.importers.hyperlynx_pimodel import is_pimodel, load_pimodel
        if is_pimodel(path):
            return load_pimodel(path)
        if not is_yaml(path):
            raise ModelError(f"{path.name}: load models are YAML files (.yml or .yaml)")
        yaml_io = _yaml()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ModelError(f"cannot read {path.name}: {e}") from e
        try:
            data = yaml_io.loads(text)
        except Exception as e:  # noqa: BLE001 - PyYAML's errors have no one base class here
            raise ModelError(f"{path.name} is not valid YAML: {e}") from e
        return LoadModel.from_dict(data)


def _yaml():
    """KiPI's YAML module; PyYAML is a requirement, so missing means a broken install."""
    from kipi.core import yaml_io
    if not yaml_io.available():
        raise ModelError("load models need PyYAML, one of KiPI's requirements, which is not "
                         "installed: reinstall KiPI's packages")
    return yaml_io


def _num(d: dict, key: str, where: str) -> Optional[float]:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ModelError(f"{where}: {key} must be a number")
    return float(v)


def _pins(v, where: str) -> List[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ModelError(f"{where}: pins must be a list")
    return [str(p) for p in v]


def _range(d: dict, key: str, where: str, units=UNITS):
    """A tolerance's (min, max, unit) from ``d[key]``, or None if it has none."""
    tol = d.get(key)
    if tol is None:
        return None
    if not isinstance(tol, dict):
        raise ModelError(f"{where}: {key} needs min and/or max")
    unit = str(tol.get("unit", PERCENT)).replace("+/-", "±").strip()
    if unit not in units:
        raise ModelError(f"{where}: {key} unit must be {', '.join(units[:-1])} or "
                         f"{units[-1]}")
    return _num(tol, "min", where), _num(tol, "max", where), unit


def _range_dict(lo: Optional[float], hi: Optional[float], unit: str) -> dict:
    out: Dict[str, object] = {k: v for k, v in (("min", lo), ("max", hi)) if v is not None}
    out["unit"] = unit
    return out


def _supply(d, i: int) -> SupplyModel:
    if not isinstance(d, dict) or not str(d.get("name") or "").strip():
        raise ModelError(f"supply {i + 1} needs a name")
    where = f"supply {d['name']}"
    s = SupplyModel(str(d["name"]).strip(), _pins(d.get("pins"), where),
                    _pins(d.get("ground_pins"), where), _num(d, "current_a", where))
    for g in d.get("pin_groups") or []:
        if not isinstance(g, dict):
            raise ModelError(f"{where}: each pin group needs pins and current_a")
        cur = _num(g, "current_a", where)
        s.groups.append(PinGroup(_pins(g.get("pins"), where), cur or 0.0,
                                 str(g.get("name") or "")))
    tol = _range(d, "tolerance", where)
    if tol is not None:
        s.tol_min, s.tol_max, s.tol_unit = tol
    tol = _range(d, "dc_tolerance", where)
    if tol is not None:
        s.dc_tol_min, s.dc_tol_max, s.dc_tol_unit = tol
    tol = _range(d, "ac_tolerance", where, AC_UNITS)
    if tol is not None:
        s.ac_tol_min, s.ac_tol_max, s.ac_tol_unit = tol
    target = d.get("target")
    if isinstance(target, dict):
        s.target_ohm = _num(target, "ohm", where)
        s.target_hz = _num(target, "up_to_hz", where)
        for pt in target.get("points") or []:
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                raise ModelError(f"{where}: target points are [frequency in Hz, ohms]")
            s.target_points.append((float(pt[0]), float(pt[1])))
    step = d.get("load_step")
    if isinstance(step, dict):
        s.step_a = _num(step, "step_a", where)
        s.slew_a_per_us = _num(step, "slew_a_per_us", where)
        mv = _num(step, "ac_tolerance_mv", where)       # older models' AC tolerance
        if mv is not None and not s.has_ac_tolerance():
            s.ac_tol_min, s.ac_tol_max, s.ac_tol_unit = -abs(mv) / 1e3, abs(mv) / 1e3, \
                DELTA_VOLTS
    return s


def _supply_dict(s: SupplyModel) -> dict:
    d: Dict[str, object] = {"name": s.name}
    if s.pins:
        d["pins"] = list(s.pins)
    if s.ground_pins:
        d["ground_pins"] = list(s.ground_pins)
    if s.groups:
        d["pin_groups"] = [{**({"name": g.name} if g.name else {}), "pins": list(g.pins),
                            "current_a": g.current} for g in s.groups]
    elif s.current_a is not None:
        d["current_a"] = s.current_a
    if s.has_tolerance():
        d["tolerance"] = _range_dict(s.tol_min, s.tol_max, s.tol_unit)
    if s.has_dc_tolerance():
        d["dc_tolerance"] = _range_dict(s.dc_tol_min, s.dc_tol_max, s.dc_tol_unit)
    if s.has_ac_tolerance():
        d["ac_tolerance"] = _range_dict(s.ac_tol_min, s.ac_tol_max, s.ac_tol_unit)
    if s.target_points:
        d["target"] = {"points": [[f, z] for f, z in s.target_points]}
    elif s.target_ohm is not None:
        d["target"] = {"ohm": s.target_ohm,
                       **({"up_to_hz": s.target_hz} if s.target_hz else {})}
    step = {k: v for k, v in (("step_a", s.step_a), ("slew_a_per_us", s.slew_a_per_us))
            if v is not None}
    if step:
        d["load_step"] = step
    return d


# ---------------------------------------------------------------------- from a setup
def model_refs(setup: PISetup) -> List[str]:
    """The reference designators that have loads or probes, to export a model of."""
    refs = {l.supply.ref for l in setup.loads if l.supply and l.supply.ref}
    refs |= {p.rail.ref for p in setup.probes if p.rail and p.rail.ref}
    return sorted(refs)


def _supply_name(name: str, ref: str, net: str) -> str:
    """A load's name as its supply's: without the reference designator it starts with,
    or the net when that leaves nothing."""
    rest = name[len(ref):].strip(" _-:") if name.startswith(ref) else name
    return rest or net or name


def model_of(setup: PISetup, ref: str, part: str = "") -> LoadModel:
    """The model of every load and probe on ``ref``: one supply per load (with the probe
    on the same net), and one per probe with no load."""
    model = LoadModel(part)
    by_net: Dict[str, SupplyModel] = {}
    for l in setup.loads:
        if not l.supply or l.supply.ref != ref:
            continue
        s = SupplyModel(_supply_name(l.name, ref, l.supply.net), list(l.supply.pins),
                        list(l.ground.pins) if l.ground and l.ground.ref == ref else [],
                        None if l.groups else l.current,
                        [PinGroup(list(g.pins), g.current, g.name) for g in l.groups],
                        l.tol_min, l.tol_max, range_unit(l.tol_pct, l.tol_dv),
                        dc_tol_min=l.dc_tol_min, dc_tol_max=l.dc_tol_max,
                        dc_tol_unit=range_unit(l.dc_tol_pct, l.dc_tol_dv),
                        ac_tol_min=l.ac_tol_min, ac_tol_max=l.ac_tol_max,
                        ac_tol_unit=range_unit(l.ac_tol_pct, l.ac_tol_dv))
        model.supplies.append(s)
        by_net.setdefault(l.supply.net, s)
    for p in setup.probes:
        if not p.rail or p.rail.ref != ref:
            continue
        s = by_net.get(p.rail.net)
        if s is None:
            s = SupplyModel(_supply_name(p.name, ref, p.rail.net), list(p.rail.pins),
                            list(p.ground.pins) if p.ground and p.ground.ref == ref else [])
            model.supplies.append(s)
            by_net[p.rail.net] = s
        if s.has_target():
            continue
        if p.step_a and p.slew_a_per_us:
            s.step_a, s.slew_a_per_us = p.step_a, p.slew_a_per_us
        if p.target_custom and p.target_points:
            s.target_points = sorted(p.target_points)
        elif p.target_ohm is not None:
            s.target_ohm, s.target_hz = p.target_ohm, p.target_hz
    names = [s.name for s in model.supplies]
    for i, s in enumerate(model.supplies):             # two loads on one net: tell apart
        if names.count(s.name) > 1:
            s.name = f"{s.name} {names[:i + 1].count(s.name)}"
    return model


# ---------------------------------------------------------------------- pin currents
def groups_of(amps: Dict[str, float]) -> List[PinGroup]:
    """Pin groups of pins drawing the same current each, from {pin: amps}."""
    by_current: Dict[float, List[str]] = {}
    for pin, a in amps.items():
        by_current.setdefault(a, []).append(pin)
    return [PinGroup(pins, a * len(pins), f"{a:g} A per pin")
            for a, pins in by_current.items()]


def on_pins(s: SupplyModel, pins: List[str]) -> SupplyModel:
    """The share of ``s`` on ``pins`` (a split supply's on one net): its pin groups cut to
    those pins, each pin drawing what it did. One group left is a plain current."""
    keep = set(pins)
    groups = []
    for g in s.groups:
        on = [p for p in g.pins if p in keep]
        if on:
            groups.append(PinGroup(on, g.current * len(on) / len(g.pins), g.name))
    out = SupplyModel(s.name, [p for p in s.pins if p in keep],
                      list(s.ground_pins), s.current_a, groups)
    if len(groups) == 1:
        out.current_a, out.groups = groups[0].current, []
    return out


# ---------------------------------------------------------------------- matching
def best_supply(model: LoadModel, name: str, pins: Optional[PinRef],
                only: bool = True) -> Optional[int]:
    """The index of the supply in ``model`` that a load or probe called ``name`` on
    ``pins`` most likely is: the most shared pins, else its name in the item's name or
    net, else (with ``only``) the only one; None if it cannot tell."""
    have = set(pins.pins) if pins else set()
    overlap = [len(have & set(s.pins)) for s in model.supplies]
    if overlap and max(overlap) > 0:
        return overlap.index(max(overlap))
    text = f"{name} {pins.net if pins else ''}".lower()
    hits = [i for i, s in enumerate(model.supplies) if s.name.lower() in text]
    if hits:
        return max(hits, key=lambda i: len(model.supplies[i].name))
    return 0 if only and len(model.supplies) == 1 else None


# ---------------------------------------------------------------------- applying
def apply_to_load(load: Load, s: SupplyModel) -> List[str]:
    """Set what ``s`` gives on ``load``; the names of the fields set."""
    done = []
    if s.groups:
        load.groups = [PinGroup(list(g.pins), g.current, g.name) for g in s.groups]
        done.append("pin groups")
    elif s.current_a is not None:
        load.current, load.groups = s.current_a, []
        done.append("current")
    if s.has_tolerance():
        load.tol_min, load.tol_max = s.tol_min, s.tol_max
        load.tol_pct, load.tol_dv = s.tol_unit == PERCENT, s.tol_unit == DELTA_VOLTS
        done.append("tolerance")
    if s.has_dc_tolerance():
        load.dc_tol_min, load.dc_tol_max = s.dc_tol_min, s.dc_tol_max
        load.dc_tol_pct, load.dc_tol_dv = (s.dc_tol_unit == PERCENT,
                                           s.dc_tol_unit == DELTA_VOLTS)
        done.append("DC tolerance")
    if s.has_ac_tolerance():
        load.ac_tol_min, load.ac_tol_max = s.ac_tol_min, s.ac_tol_max
        load.ac_tol_pct, load.ac_tol_dv = (s.ac_tol_unit == PERCENT,
                                           s.ac_tol_unit == DELTA_VOLTS)
        done.append("AC tolerance")
    return done


def _tighter_side(nominal: Optional[float], lo: Optional[float], hi: Optional[float],
                  unit: str) -> Optional[float]:
    """The tighter side of a range, volts from ``nominal``; None if it has none, or needs
    the nominal voltage and it is unknown."""
    if unit == DELTA_VOLTS:                         # volts from nominal already
        sides = [abs(x) for x in (lo, hi) if x is not None]
    elif not nominal:
        return None
    else:
        lo, hi = range_volts(nominal, lo, hi, unit)
        sides = [abs(x - nominal) for x in (lo, hi) if x is not None]
    return min(sides) if sides else None


def ac_tolerance_v(s: SupplyModel, nominal: Optional[float]) -> Optional[float]:
    """The AC tolerance, volts: the tighter side of the AC tolerance, else of the total
    tolerance."""
    if s.has_ac_tolerance():
        return _tighter_side(nominal, s.ac_tol_min, s.ac_tol_max, s.ac_tol_unit)
    if not s.has_tolerance():
        return None
    return _tighter_side(nominal, s.tol_min, s.tol_max, s.tol_unit)


def apply_to_probe(probe, s: SupplyModel, setup: Optional[PISetup] = None) -> List[str]:
    """Set the target ``s`` gives (or works out from its load step) on ``probe``, and its
    load step (#166); the names of the fields set."""
    step = []
    if s.step_a and s.slew_a_per_us:
        probe.step_a, probe.slew_a_per_us = s.step_a, s.slew_a_per_us
        step = ["load step"]
    if s.target_points:
        probe.target_custom, probe.target_points = True, sorted(s.target_points)
        return ["target mask"] + step
    if s.target_ohm is not None:
        probe.target_custom = False
        probe.target_ohm, probe.target_hz = s.target_ohm, s.target_hz
        return ["target"] + step
    if s.step_a is None or s.slew_a_per_us is None:
        return []
    from kipi.ac.target import target_from_transient
    nominal = rail_nominal(setup, probe.rail.net) if setup is not None and probe.rail \
        else None
    v_ac = ac_tolerance_v(s, abs(nominal) if nominal else None)
    t = target_from_transient(v_ac or 0.0, s.step_a, s.slew_a_per_us * 1e6)
    if t is None:
        return step
    probe.target_custom = False
    probe.target_ohm, probe.target_hz = t.z_ohm, t.f_hz
    return ["target from the load step"] + step


# ---------------------------------------------------------------------- new items
def pins_for(part_nets: Dict[str, List[str]], pins: List[str]) -> Optional[PinRef]:
    """Where ``pins`` of a part are: the net most of them are on, and those of them on
    it; None if none is on a net. ``part_nets`` is the part's {net: [pins]}."""
    want = set(pins)
    best = max(part_nets.items(), key=lambda kv: len(want & set(kv[1])), default=None)
    if best is None or not want & set(best[1]):
        return None
    net, on = best
    return PinRef("", net, [p for p in pins if p in set(on)])


def new_items(model: LoadModel, ref: str, part_nets: Dict[str, List[str]]):
    """[(supply, supply pins, ground pins or None)] for the supplies of ``model`` that
    have pins on part ``ref``; ``part_nets`` is its {net: [pins]}."""
    out = []
    for s in model.supplies:
        if s.split_by_net:
            for net, on in part_nets.items():
                mine = [p for p in s.pins if p in set(on)]
                if mine:
                    share = on_pins(s, mine)
                    share.name = net
                    out.append((share, PinRef(ref, net, mine), None))
            continue
        rail = pins_for(part_nets, s.pins) if s.pins else None
        if rail is None:
            continue
        gnd = pins_for(part_nets, s.ground_pins) if s.ground_pins else None
        rail.ref = ref
        if gnd is not None:
            gnd.ref = ref
        out.append((s, rail, gnd))
    return out
