"""Read a Tomachie power-tree file (``<design>.pdn.json``, schema ``tomachie-pdn`` v1) into
a :class:`~kipi.dc.pi.model.PISetup`.

The file comes from the schematic and the parts' datasheets: every rail, its voltage
and source, the parts in series between rails, every load with its typical and maximum
current. What maps where:

* a rail sourced by a connector, battery or module pin -> a ``source`` VRM;
* a rail sourced by a regulator -> an ``ldo`` / ``smps`` VRM whose input pins are the
  regulator's pins on its input rail (the file also lists those as a load of the input
  rail; that entry is not imported, because KiPI derives a VRM's input current itself);
* a part in series between rails, and a switcher's inductor -> a DC connection;
* a load switch (a regulator entry of type ``load_switch``: it passes its input to its
  output and regulates nothing) -> a DC connection from its input pins to its output
  pins with its on-resistance, and its quiescent current a load on its input pins;
* a load -> a load with its typical current, and a tolerance from its rail's stated
  minimum and maximum, tightened to the datasheet's minimum supply voltage when that is
  higher.

KiPI solves one value per item, so where the file gives a range only one end is used:
the typical current of a load and the typical resistance of a series part (the maximum
when no typical is printed). The minimum and maximum left out are listed in the notes. A
current the file does not know (unknown, a part that draws what it drives, power leaving
the board at a connector pin) is imported as 0 A and named in the notes, to be entered by
hand.

Nothing is guessed. Whatever cannot be placed on the board is listed in the notes.
"""

from __future__ import annotations

import dataclasses
import json
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.importers.pad_index import PadIndex
from kipi.dc.pi.importers.pdn_figures import amps, dc_resistance, number
from kipi.dc.pi.model import (
    DcLink, Load, PISetup, Vrm, group_name, net_groups, volts_to_pct,
)

SCHEMA = "tomachie-pdn"
SCHEMA_VERSION = 1
SOURCE_ROLES = ("board_input", "battery", "generator")
VRM_KIND = {"linear": "ldo", "switching": "smps"}


class PdnImportError(ValueError):
    pass


def _volts(v) -> Optional[float]:
    """A voltage object's volts; None for "unknown" or a missing field."""
    return number(v.get("volts")) if isinstance(v, dict) else None


def path_to_rail(parts: List[dict], start: str, rail: str) -> List[dict]:
    """The parts of a regulator's ``output_path`` that lead from its output net to its
    rail, in order. Files written before Tomachie 5.137 also list parts that only hang
    off the output node (pull-ups, feedback-divider legs); those are not on the walk."""
    reached: Dict[str, Optional[dict]] = {start: None}
    queue = deque([start])
    while queue and rail not in reached:
        net = queue.popleft()
        for e in parts:
            nxt = (e.get("net_out") if e.get("net_in") == net else
                   e.get("net_in") if e.get("net_out") == net else None)
            if nxt and nxt not in reached:
                reached[nxt] = dict(e, net_in=net, net_out=nxt)
                queue.append(nxt)
    path: List[dict] = []
    net = rail
    while reached.get(net) is not None:
        path.insert(0, reached[net])
        net = reached[net]["net_in"]
    return path


class TomachiePdnImporter:
    def __init__(self, pdn: dict, snap: BoardSnapshot, base: Optional[PISetup] = None):
        if pdn.get("schema") != SCHEMA or pdn.get("schema_version") != SCHEMA_VERSION:
            raise PdnImportError(f"Not a {SCHEMA} version {SCHEMA_VERSION} file "
                                 f"(schema {pdn.get('schema')!r}, "
                                 f"version {pdn.get('schema_version')!r})")
        self.pdn = pdn
        self.idx = PadIndex(snap)
        self.notes: List[str] = []
        # Only the power tree is replaced: solve settings, thermal, the report option, the
        # stackup and the AC setup stay as the user set them.
        self.setup = (dataclasses.replace(base, vrms=[], loads=[], links=[]) if base
                      else PISetup())
        # One value per item is solved; the other ends of a range, summed up at the end.
        self.unused_current: List[Tuple[str, str]] = []     # (load, what is left out)
        self.unused_resistance: List[str] = []
        self.unknown_current: List[str] = []
        self.rails: Dict[str, dict] = {r["name"]: r for r in pdn.get("rails", [])}
        self.regulators: Dict[str, dict] = {g["refdes"]: g for g in pdn.get("regulators", [])}
        self.skipped_rails: Set[str] = set()
        self.linked: Set[str] = set()

    # ------------------------------------------------------------ helpers
    def _unique(self, name: str) -> str:
        taken = {x.name for x in [*self.setup.vrms, *self.setup.loads, *self.setup.links]}
        out, i = name, 2
        while out in taken:
            out, i = f"{name} ({i})", i + 1
        return out

    def _learn_nets(self):
        """Tie each schematic net in the file to its board net through pins on it."""
        for name, r in self.rails.items():
            src = r["source"]
            if src.get("output_net"):
                self.idx.learn(src["output_net"], src.get("pins", []))
            self.idx.learn(name, [p for l in r.get("loads", []) if not l.get("input_net")
                                  for p in l.get("pins", [])])
            for l in r.get("loads", []):
                if l.get("input_net"):
                    self.idx.learn(l["input_net"], l.get("pins", []))

    def _voltage(self, rail: dict) -> Optional[float]:
        v = number(rail.get("voltage", {}).get("nominal"))
        if v is None:
            self.notes.append(f"Rail {rail['name']}: voltage not stated, so it has no source; "
                              "its loads are not imported")
            self.skipped_rails.add(rail["name"])
        return v

    # ------------------------------------------------------------ sources
    def _source_pins(self, src: dict, what: str):
        """The source's output pins. A pin the file names by its pin name (``J9_VBUS``)
        rather than its pad number is not found among the pads; the file also names the
        part and the net it drives, so its pads on that board net are used, and said so."""
        out = self.idx.pin_ref(src.get("pins", []), self.notes, what)
        ref, net = src.get("refdes"), src.get("output_net")
        if out is not None or not ref or not net:
            return out
        board_net = self.idx.board_net(net)
        out = self.idx.part_on_net(ref, board_net) if board_net else None
        if out is not None:
            self.notes.append(f"{what}: using {ref}'s pads on its output net {board_net} "
                              f"({', '.join(out.pins)})")
        return out

    def _board_sources(self):
        for name, r in self.rails.items():
            src = r["source"]
            if src["role"] == "unresolved":
                self.notes.append(f"Rail {name}: no source traced; its loads are not imported")
                self.skipped_rails.add(name)
            elif src["role"] in SOURCE_ROLES:
                v = self._voltage(r)
                out = self._source_pins(src, f"Source of {name}")
                if v is None:
                    continue
                if out is None:
                    self.notes.append(f"Rail {name}: source {src.get('refdes') or '?'} not "
                                      "found on the board; its loads are not imported")
                    self.skipped_rails.add(name)
                    continue
                self.setup.vrms.append(Vrm(self._unique(f"{src.get('refdes') or name} {name}"),
                                           out, v, "source"))

    def _regulator_input(self, reg: dict):
        """The regulator's pins on its input rail, from its load entry there."""
        l = self._regulator_entry(reg)
        return self.idx.pin_ref(l["pins"], self.notes, f"{reg['refdes']} input") if l else None

    def _regulator_kind(self, reg: dict, rail: str) -> Tuple[str, float]:
        kind = VRM_KIND.get(reg.get("type"))
        eff = number(reg.get("efficiency"))
        if kind is None:
            self.notes.append(f"{reg['refdes']} ({rail}): not known whether linear or "
                              "switching; modeled as an ideal source, drawing nothing from "
                              "its input rail")
            return "source", 1.0
        if kind == "smps" and eff is None:
            self.notes.append(f"{reg['refdes']} ({rail}): efficiency not read from its "
                              "datasheet; solved lossless, so its input current and the rail "
                              "feeding it are understated")
            return kind, 1.0
        return kind, eff if eff is not None else 1.0

    def _regulated_rails(self):
        for name, r in self.rails.items():
            src = r["source"]
            if src["role"] != "regulator":
                continue
            reg = self.regulators.get(src.get("refdes", ""))
            if reg is not None and reg.get("type") == "load_switch":
                self._load_switch(reg, src, name)
                continue
            v = self._voltage(r)
            out = self._source_pins(src, f"{src.get('refdes')} output")
            if v is None or out is None or reg is None:
                if v is not None:
                    why = ("not listed among the regulators" if reg is None else
                           "output pins not found on the board")
                    self.notes.append(f"Rail {name}: regulator {src.get('refdes') or '?'} "
                                      f"{why}; its loads are not imported")
                self.skipped_rails.add(name)
                continue
            kind, eff = self._regulator_kind(reg, name)
            vin = self._regulator_input(reg) if kind != "source" else None
            if kind != "source" and vin is None:
                self.notes.append(f"{reg['refdes']} ({name}): input pins not found; modeled "
                                  "as an ideal source")
                kind = "source"
            self.setup.vrms.append(Vrm(self._unique(f"{reg['refdes']} {name}"), out, v, kind,
                                       efficiency=eff, input=vin,
                                       quiescent_a=amps(reg.get("quiescent_current")) or 0.0,
                                       dropout_v=_volts(reg.get("dropout_voltage")) or 0.0,
                                       vin_min=_volts(reg.get("input_voltage_min")),
                                       vin_max=_volts(reg.get("input_voltage_max"))))
            self._output_path(reg["refdes"], src, name)

    def _output_path(self, ref: str, src: dict, rail: str):
        """The parts between a regulator's output pins and its rail, as DC connections."""
        parts = src.get("output_path", [])
        path = path_to_rail(parts, src.get("output_net", ""), rail)
        off = sorted({e["refdes"] for e in parts} - {e["refdes"] for e in path})
        if off:
            self.notes.append(f"{ref} ({rail}): {', '.join(off)} listed on its "
                              "output path but not between its output and the rail; left out")
        for e in path:
            self._link(e)

    def _regulator_entry(self, reg: dict) -> Optional[dict]:
        """The regulator's load entry on its input rail."""
        for rail_name in reg.get("input_rails", []):
            for l in self.rails.get(rail_name, {}).get("loads", []):
                if l["role"] == "regulator" and l["refdes"] == reg["refdes"]:
                    return l
        return None

    def _load_switch(self, reg: dict, src: dict, rail: str):
        """A power or load switch regulates nothing: its output is its input less I times
        its on-resistance, so it is a DC connection from its input pins to its output
        pins, not an LDO (which would make 5 V from 5 V and be out of regulation). The
        parts between its input rail and its input pins (an input filter) and between its
        output pins and its rail are DC connections too. Its quiescent current is drawn
        at its input pins, as an LDO VRM's is."""
        ref = reg["refdes"]
        vin = self._regulator_input(reg)
        out = self._source_pins(src, f"{ref} output")
        if vin is None or out is None:
            self.notes.append(f"Rail {rail}: load switch {ref} "
                              f"{'input' if vin is None else 'output'} pins not found on the "
                              "board; its loads are not imported")
            self.skipped_rails.add(rail)
            return
        self.linked.add(ref)
        typ, mx, fig_notes = dc_resistance({"datasheet_figures":
                                            reg.get("datasheet_resistances") or []})
        self._add_link(ref, vin, out, typ, mx, fig_notes, "on-resistance")
        self.notes.append(f"{ref} ({rail}): load switch, imported as a DC connection from "
                          f"{vin.net} to {out.net}, not a regulator")
        for e in (self._regulator_entry(reg) or {}).get("input_filter") or []:
            self._link(e)
        self._output_path(ref, src, rail)
        iq = amps(reg.get("quiescent_current"))
        if iq is not None:
            self.setup.loads.append(Load(self._unique(f"{ref} quiescent"), vin, iq))

    # ------------------------------------------------------------ series parts
    def _link(self, e: dict):
        ref = e["refdes"]
        if ref in self.linked:
            return
        self.linked.add(ref)
        net_in = self.idx.board_net(e.get("net_in", ""))
        a = self.idx.part_on_net(ref, net_in) if net_in else None
        net_out = self.idx.board_net(e.get("net_out", ""))
        b = (self.idx.part_on_net(ref, net_out) if net_out else None) or (
            self.idx.other_side(ref, net_in) if net_in else None)
        if a is None or b is None:
            self.notes.append(f"{ref}: series part between {e.get('net_in')} and "
                              f"{e.get('net_out')} not found on the board")
            return
        typ, mx, fig_notes = dc_resistance(e)
        if e.get("class") == "diode":
            self.notes.append(f"{ref}: a diode in series; its forward voltage is not part of "
                              "a resistive DC solve, so it is modeled as a short")
            typ, mx = 0.0, None
        self._add_link(ref, a, b, typ, mx, fig_notes)

    def _add_link(self, ref: str, a, b, typ: Optional[float], mx: Optional[float],
                  fig_notes: List[str], what: str = "DC resistance"):
        self.notes.extend(f"{ref}: {n}" for n in fig_notes)
        if typ is None and mx is None:
            self.notes.append(f"{ref}: {what} not known; modeled as a short")
            typ = 0.0
        elif typ is None:
            self.notes.append(f"{ref}: only a maximum {what} is printed; it is used")
            typ = mx
        elif mx is not None and mx != typ:
            self.unused_resistance.append(f"{ref} {_ohms(mx)}")
        self.setup.links.append(DcLink(self._unique(ref), a, b, typ))

    def _series_parts(self):
        for r in self.rails.values():
            for e in r.get("series_from_parent", []):
                self._link(e)

    # ------------------------------------------------------------ loads
    def _tolerance(self, rail: dict, l: dict) -> dict:
        """The load's PASS / FAIL range: the rail's stated minimum and maximum, in % from
        nominal, or in volts when the datasheet's minimum supply voltage (``supply_min``)
        is higher than the rail's minimum and takes its place."""
        v = rail.get("voltage", {})
        nom, lo, hi = number(v.get("nominal")), number(v.get("min")), number(v.get("max"))
        sm = l.get("supply_min")
        smin = number(sm.get("volts")) if isinstance(sm, dict) else None
        if not nom:
            return {}
        if smin is not None:
            if nom < 0 or smin < 0:
                self.notes.append(f"{l['refdes']} on {rail['name']}: minimum supply "
                                  f"{smin:g} V not applied on a negative rail; set its "
                                  "tolerance by hand")
            elif lo is None or smin > lo:
                return {"tol_min": smin, "tol_max": hi, "tol_pct": False}
        if lo is None and hi is None:
            return {}
        tmin, tmax = volts_to_pct(nom, lo, hi)
        return {"tol_min": None if tmin is None else round(tmin, 6),
                "tol_max": None if tmax is None else round(tmax, 6), "tol_pct": True}

    def _current(self, l: dict, rail: str, label: str) -> float:
        """The one current KiPI solves for a load: its typical, else its maximum, else
        0 A (named in the notes)."""
        typ, mx, mn = (amps(l.get(k)) for k in ("current", "current_max", "current_min"))
        if l["role"] == "ic_with_regulator":
            rin, rin_max, rin_min = (amps(l.get(k)) for k in (
                "regulator_input", "regulator_input_max", "regulator_input_min"))
            typ = None if typ is None or rin is None else typ + rin
            mx = None if mx is None or rin_max is None else mx + rin_max
            mn = None if mn is None or rin_min is None else mn + rin_min
        if l.get("load_dependent"):
            self.notes.append(f"{l['refdes']} on {rail}: draws what it drives "
                              f"({l.get('load_dependent_reason', 'no reason given')})")
            typ = mx = mn = None
        if typ is None and mx is not None:
            self.notes.append(f"{label}: only a maximum current is known; {_amps(mx)} is "
                              "used")
            return mx
        if typ is None:
            self.unknown_current.append(label)
            return 0.0
        left = [f"{w} {_amps(x)}" for w, x in (("min", mn), ("max", mx))
                if x is not None and x != typ]
        if left:
            self.unused_current.append((label, ", ".join(left)))
        return typ

    def _rail_loads(self, rail: dict):
        name = rail["name"]
        for l in rail.get("loads", []):
            if l["role"] == "regulator":
                continue                   # the regulator's VRM input carries this current
            supply = self.idx.pin_ref(l["pins"], self.notes, f"Load {l['refdes']} on {name}")
            if supply is None:
                continue
            taken = {x.name for x in self.setup.loads}
            label = self._unique(l["refdes"] if l["refdes"] not in taken
                                 else f"{l['refdes']} ({name})")
            current = self._current(l, name, label)
            self.setup.loads.append(Load(label, supply, current, **self._tolerance(rail, l)))
        for pin in rail.get("offboard_pins", []):
            supply = self.idx.pin_ref([pin], self.notes, f"Off-board pin {pin} on {name}")
            if supply is not None:
                label = self._unique(f"{pin} (off-board)")
                self.unknown_current.append(label)
                self.setup.loads.append(Load(label, supply, 0.0,
                                             **self._tolerance(rail, {"refdes": pin})))

    def _loads(self):
        for name, r in self.rails.items():
            if name not in self.skipped_rails:
                self._rail_loads(r)

    # ------------------------------------------------------------ what nothing powers
    def _drop_unpowered(self):
        """Leave out VRMs whose input nothing in the file powers, and loads on nets nothing
        drives, until every item left is powered; each one left out is named."""
        while True:
            groups = net_groups(self.setup)
            driven = {groups[v.output.net] for v in self.setup.vrms}
            cut = [v for v in self.setup.vrms if v.input and groups[v.input.net] not in driven]
            for v in cut:
                self.notes.append(f"VRM {v.name}: nothing in the file powers its input "
                                  f"({group_name(groups[v.input.net])}); left out")
            self.setup.vrms = [v for v in self.setup.vrms if v not in cut]
            if not cut:
                break
        groups = net_groups(self.setup)
        driven = {groups[v.output.net] for v in self.setup.vrms}
        unpowered: Dict[str, List[str]] = {}
        for l in self.setup.loads:
            g = groups[l.supply.net]
            if g not in driven:
                unpowered.setdefault(group_name(g), []).append(l.name)
        for net, names in unpowered.items():
            self.notes.append(f"Nothing in the file powers {net}; its loads are left out: "
                              + ", ".join(names))
        dropped = {n for names in unpowered.values() for n in names}
        self.setup.loads = [l for l in self.setup.loads if l.name not in dropped]

    # ------------------------------------------------------------ run
    def run(self) -> Tuple[PISetup, List[str]]:
        self._learn_nets()
        self._board_sources()
        self._regulated_rails()
        self._series_parts()
        self._loads()
        self._drop_unpowered()
        self._range_notes()
        return self.setup, self.notes

    def _range_notes(self):
        """What a single value per item left out, one note each, for the loads kept."""
        kept = {x.name for x in self.setup.loads}
        unknown = [n for n in self.unknown_current if n in kept]
        if unknown:
            self.notes.append("Current not known, imported as 0 A; enter it on the Loads tab: "
                              + ", ".join(unknown))
        unused = [f"{n} ({r})" for n, r in self.unused_current if n in kept]
        if unused:
            self.notes.append("KiPI solves one current per load, so the typical is used and "
                              "these minimum / maximum currents are not: " + "; ".join(unused))
        if self.unused_resistance:
            self.notes.append("KiPI solves one resistance per DC connection, so the typical "
                              "is used and these maximum resistances are not: "
                              + "; ".join(self.unused_resistance))


def _amps(a: float) -> str:
    return f"{a * 1e3:.4g} mA" if abs(a) < 1 else f"{a:.4g} A"


def _ohms(r: float) -> str:
    return f"{r * 1e3:.4g} mΩ" if abs(r) < 1 else f"{r:.4g} Ω"


def import_tomachie_pdn(path, snap: BoardSnapshot,
                        base: Optional[PISetup] = None) -> Tuple[PISetup, List[str]]:
    """The setup described by a Tomachie ``.pdn.json`` on this board, and notes on what
    could not be placed."""
    pdn = json.loads(Path(path).read_text(encoding="utf-8"))
    return TomachiePdnImporter(pdn, snap, base).run()
