"""Load models (#17): what a part draws from each of its supplies, in a file of its own, to
use for every copy of the part and on other boards.

A model file is YAML (JSON where PyYAML is missing) and names one part:

.. code-block:: yaml

    kipi_load_model: 1
    part: XC7A35T-1CSG324C          # what it is a model of (free text)
    supplies:                       # one per supply rail of the part
      - name: VCCINT
        pins: [F7, F9, H7]          # its supply pins (optional)
        ground_pins: [A1, A10]      # its ground pins (optional)
        current_a: 1.2              # or pin_groups, each drawing its own current
        pin_groups: [{name: core, pins: [F7, F9], current_a: 0.8}]
        tolerance: {min: -3, max: 3, unit: "%"}     # unit V, ±V or %
        heat_w: 0.9
        target: {ohm: 0.01, up_to_hz: 50.0e6}      # or {points: [[hz, ohm], ...]}
        load_step: {step_a: 0.6, slew_a_per_us: 5, ac_tolerance_mv: 30}

Every key but ``name`` is optional, and only the ones given are applied. The DC loads take
the current, pin groups, tolerance and heat; the AC probes the target, or one worked out
from the load step as the target calculator does (with the AC tolerance given, else the
tighter side of the DC tolerance). A model exported from a setup holds every load and probe
on one reference designator, one supply each, so a part with several supplies keeps them
all. Pins make a model usable to add the loads and probes for another copy of the part.
"""

from __future__ import annotations

import json
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
    heat_w: Optional[float] = None
    target_ohm: Optional[float] = None
    target_hz: Optional[float] = None
    target_points: List[Tuple[float, float]] = field(default_factory=list)
    step_a: Optional[float] = None
    slew_a_per_us: Optional[float] = None
    ac_tolerance_mv: Optional[float] = None

    def has_tolerance(self) -> bool:
        return self.tol_min is not None or self.tol_max is not None

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
        if self.has_tolerance():
            bits.append(tolerance_label(self.tol_min, self.tol_max, self.tol_unit == PERCENT,
                                        self.tol_unit == DELTA_VOLTS))
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
        """Write the model, as YAML unless ``path`` is .json or PyYAML is missing."""
        from kipi.core import yaml_io
        path = Path(path)
        if is_yaml(path) and yaml_io.available():
            path.write_text(yaml_io.dumps(self.to_dict()), encoding="utf-8")
        else:
            path = path if path.suffix.lower() == ".json" else path.with_suffix(".json")
            path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(path) -> "LoadModel":
        from kipi.core import yaml_io
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ModelError(f"cannot read {path.name}: {e}") from e
        try:
            if is_yaml(path):
                if not yaml_io.available():
                    raise ModelError("reading YAML needs PyYAML, which is not installed")
                data = yaml_io.loads(text)
            else:
                data = json.loads(text)
        except ModelError:
            raise
        except Exception as e:  # noqa: BLE001 - YAML and JSON raise their own kinds
            raise ModelError(f"{path.name} is not valid {'YAML' if is_yaml(path) else 'JSON'}"
                             f": {e}") from e
        return LoadModel.from_dict(data)


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
    tol = d.get("tolerance")
    if tol is not None:
        if not isinstance(tol, dict):
            raise ModelError(f"{where}: tolerance needs min and/or max")
        s.tol_min, s.tol_max = _num(tol, "min", where), _num(tol, "max", where)
        s.tol_unit = str(tol.get("unit", PERCENT)).replace("+/-", "±").strip()
        if s.tol_unit not in UNITS:
            raise ModelError(f"{where}: tolerance unit must be V, ±V or %")
    s.heat_w = _num(d, "heat_w", where)
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
        s.ac_tolerance_mv = _num(step, "ac_tolerance_mv", where)
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
        d["tolerance"] = {k: v for k, v in (("min", s.tol_min), ("max", s.tol_max))
                          if v is not None}
        d["tolerance"]["unit"] = s.tol_unit
    if s.heat_w is not None:
        d["heat_w"] = s.heat_w
    if s.target_points:
        d["target"] = {"points": [[f, z] for f, z in s.target_points]}
    elif s.target_ohm is not None:
        d["target"] = {"ohm": s.target_ohm,
                       **({"up_to_hz": s.target_hz} if s.target_hz else {})}
    step = {k: v for k, v in (("step_a", s.step_a), ("slew_a_per_us", s.slew_a_per_us),
                              ("ac_tolerance_mv", s.ac_tolerance_mv)) if v is not None}
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
                        l.dissipation_w)
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
        if p.target_custom and p.target_points:
            s.target_points = sorted(p.target_points)
        elif p.target_ohm is not None:
            s.target_ohm, s.target_hz = p.target_ohm, p.target_hz
    names = [s.name for s in model.supplies]
    for i, s in enumerate(model.supplies):             # two loads on one net: tell apart
        if names.count(s.name) > 1:
            s.name = f"{s.name} {names[:i + 1].count(s.name)}"
    return model


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
    if s.heat_w is not None:
        load.dissipation_w = s.heat_w
        done.append("heat")
    return done


def ac_tolerance_v(s: SupplyModel, nominal: Optional[float]) -> Optional[float]:
    """The AC tolerance, volts: as given, else the tighter side of the DC tolerance."""
    if s.ac_tolerance_mv is not None:
        return s.ac_tolerance_mv / 1e3
    if not (nominal and s.has_tolerance()):
        return None
    lo, hi = range_volts(nominal, s.tol_min, s.tol_max, s.tol_unit)
    sides = [abs(x - nominal) for x in (lo, hi) if x is not None]
    return min(sides) if sides else None


def apply_to_probe(probe, s: SupplyModel, setup: Optional[PISetup] = None) -> List[str]:
    """Set the target ``s`` gives (or works out from its load step) on ``probe``; the
    names of the fields set."""
    if s.target_points:
        probe.target_custom, probe.target_points = True, sorted(s.target_points)
        return ["target mask"]
    if s.target_ohm is not None:
        probe.target_custom = False
        probe.target_ohm, probe.target_hz = s.target_ohm, s.target_hz
        return ["target"]
    if s.step_a is None or s.slew_a_per_us is None:
        return []
    from kipi.ac.target import target_from_transient
    nominal = rail_nominal(setup, probe.rail.net) if setup is not None and probe.rail \
        else None
    v_ac = ac_tolerance_v(s, abs(nominal) if nominal else None)
    t = target_from_transient(v_ac or 0.0, s.step_a, s.slew_a_per_us * 1e6)
    if t is None:
        return []
    probe.target_custom = False
    probe.target_ohm, probe.target_hz = t.z_ohm, t.f_hz
    return ["target from the load step"]


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
        rail = pins_for(part_nets, s.pins) if s.pins else None
        if rail is None:
            continue
        gnd = pins_for(part_nets, s.ground_pins) if s.ground_pins else None
        rail.ref = ref
        if gnd is not None:
            gnd.ref = ref
        out.append((s, rail, gnd))
    return out
