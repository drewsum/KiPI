"""Read a Tomachie power-tree file (``<design>.pdn.json``, schema ``tomachie-pdn`` v1) into
a :class:`~kipi.dc.pi.model.PISetup`.

The file comes from the schematic and the parts' datasheets: every rail, its voltage
and source, the parts in series between rails, every load with its typical and maximum
current. What maps where:

* a rail sourced by a connector, battery or module pin -> a ``source`` VRM, its ground
  pins the pins its current returns on, its rated output (a module's) its current limit;
* a rail sourced by a regulator -> an ``ldo`` / ``smps`` VRM whose input pins are the
  regulator's pins on its input rail (the file also lists those as a load of the input
  rail; that entry is not imported, because KiPI derives a VRM's input current itself).
  A part with outputs of both kinds (a PMIC's bucks beside its LDOs) types each output.
  An isolated converter is an ``isolated`` VRM: its ground pins are its output side's
  0 V reference and its input ground pins its input side's return;
* the regulator's remote sense (``source.sense``: the pins it holds at its set voltage,
  or the point on the rail they are tied to) -> the VRM's sense pins, and its return
  sense pins its ground sense;
* the regulator's output band (``regulator_band``: its datasheet accuracy with its
  feedback divider's tolerance) -> the VRM's DC accuracy, in volts;
* a part in series between rails, and the parts between a connector's pin and its rail
  or between a regulator's input rail and its input pins -> DC connections. A switcher's
  output inductor sits inside its regulation loop (the feedback is on the rail), so it is
  a short, said once; its loss is in the efficiency;
* a part joining two ground nets (a 0R, a net tie, a common-mode choke's return winding)
  -> a DC connection, so a return reaches the reference through it;
* a load switch (a regulator entry of type ``load_switch``: it passes its input to its
  output and regulates nothing) -> a DC connection from its input pins to its output
  pins with its on-resistance, and its quiescent current a load on its input pins;
* a load -> a load with its typical current, its ground pins the pins its current returns
  on (a pull resistor's: the part it pulls; a motor's: its driver's), and a tolerance
  from its rail's stated minimum and maximum, tightened to the datasheet's minimum and
  maximum supply voltage where those are stricter. A part on a rail by a detect, enable
  or reference pin only is not a load.

Only the fields named here are read. A ``details`` object, at any level of the file,
holds what the file knows beyond them (other tools' data) and is not read; nor is any
other field the importer does not name. Neither is a reason to refuse the file.

KiPI solves one value per item, so where the file gives a range only one end is used:
the typical current of a load and the typical resistance of a series part (the maximum
when no typical is printed). The minimum and maximum left out are listed in the notes. A
current the file does not know (unknown, a part that draws what it drives, power leaving
the board at a connector pin) is imported as 0 A and named in the notes, to be entered by
hand; a part that draws what it drives is solved at its own static current.

Nothing is guessed. Whatever cannot be placed on the board is listed in the notes.
"""

from __future__ import annotations

import dataclasses
import json
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.core.model.parts import parse_resistance
from kipi.dc.pi.importers.pad_index import PadIndex
from kipi.dc.pi.importers.pdn_figures import amps, dc_resistance, number
from kipi.dc.pi.model import (
    GROUND_REF_KINDS, SWITCHING_KINDS, DcLink, Load, PinRef, PISetup, Vrm, group_name,
    net_groups, volts_to_pct,
)

SCHEMA = "tomachie-pdn"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 64 << 20       # a power-tree file is kilobytes; this is not one
SOURCE_ROLES = ("board_input", "battery", "generator")
VRM_KIND = {"linear": "ldo", "switching": "smps", "isolated": "isolated"}
# A part on a rail by a signal pin only (detect, enable, reference): 0 A, not a load.
# Older files name it "not_a_supply"; both are read.
NOT_A_LOAD = ("signal_pin_only", "not_a_supply")
# Loads whose supply range is the datasheet's to state (a resistor's or a connector's is
# not): one not stated is a value still to find.
IC_ROLES = ("ic", "ic_with_regulator")
# A resistor joining two ground nets above this carries no DC to speak of (a shield's 1M).
GROUND_LINK_MAX_OHMS = 1e3


class PdnImportError(ValueError):
    pass


def _volts(v) -> Optional[float]:
    """A voltage object's volts; None for "unknown" or a missing field."""
    return number(v.get("volts")) if isinstance(v, dict) else None


def _return_part(load: dict) -> str:
    """The part a load's current returns to ground through: a resistor / LED chain's
    last part, the part a pull resistor pulls (pull_to, <refdes>_<pin>), else the load."""
    if load.get("role") == "pull_resistor" and "_" in (load.get("pull_to") or ""):
        return load["pull_to"].rsplit("_", 1)[0]
    return (load.get("path_parts") or [load["refdes"]])[-1]


def path_to_rail(parts: List[dict], start: str, rail: str) -> List[dict]:
    """The parts of a regulator's ``output_path`` that lead from its output net to its
    rail, in order. Some older files also list parts that only hang off the output
    node (pull-ups, feedback-divider legs); those are not on the walk."""
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
        self.static_current: Dict[str, float] = {}   # solved at a static figure, more not known
        self.not_supplied: List[str] = []    # on a rail by no supply pin: not a load
        self.no_supply_range: List[str] = []  # ICs whose datasheet supply range is not known
        self.accuracies: List[str] = []       # VRM output bands taken from the file
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

    def _ground(self, ref: Optional[str], net: Optional[str] = None) -> Optional[PinRef]:
        """The part's pads on its ground net - ``net`` when the file names it (the ground
        its rail returns on), else the first of the file's ground nets it has pads on - so
        its return current flows in the ground copper too (None: it has none there, and
        returns through an ideal ground)."""
        if ref and net and net not in ("unknown", "not_specified"):
            pins = self.idx.part_on_net(ref, self.idx.board_net(net) or net)
            if pins is not None:
                return pins
        for name in self.pdn.get("ground_nets", []):
            pins = self.idx.part_on_net(ref, self.idx.board_net(name) or name) if ref else None
            if pins is not None:
                return pins
        return None

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

    def _behind_placed_output(self, name: str, src: dict, out) -> bool:
        """True (and its series parts added) when this rail is a regulator output already
        placed for another rail, reached through the series parts of its output path (a
        current-sense shunt between a regulator's output and the rail it feeds): the rail
        is that output's, behind those parts, and keeps its loads."""
        path = src.get("output_path") or []
        if out is None or not any(v.output and v.output.net == out.net for v in self.setup.vrms):
            return False
        if not path:
            # The same output under its own name, placed first through another rail's
            # path: its loads are that output's.
            if self.idx.board_net(name) != out.net:
                return False
            self.notes.append(f"Rail {name}: {src.get('refdes')}'s output, placed with "
                              "another rail it feeds")
            return True
        if not path_to_rail(path, out.net, name):
            return False                    # its series parts lead elsewhere: a wrong file
        self._output_path(src.get("refdes", ""), src, name)
        parts = ", ".join(p.get("refdes", "?") for p in path)
        self.notes.append(f"Rail {name}: fed from {src.get('refdes')}'s output {out.net} "
                          f"through {parts}")
        return True

    def _off_its_net(self, name: str, src: dict, out) -> bool:
        """True (and said) when the file is wrong about this rail's source: the source pins
        it names are on another board net than the source's output net, or they already
        drive a rail placed before. Placing it would drive another rail."""
        if out is None:
            return False
        pins = ", ".join(src.get("pins", [])) or src.get("refdes", "?")
        net = src.get("output_net")
        board_net = self.idx.board_net(net) if net else None
        if board_net is not None and out.net != board_net:
            why = f"on the board that pin is on {out.net}, not {board_net}"
        elif any(v.output and v.output.net == out.net for v in self.setup.vrms):
            why = f"those pins already drive {out.net}"
        else:
            return False
        self.notes.append(f"Rail {name}: the file names {pins} as its source, but {why}; "
                          "the rail and its loads are not imported")
        self.skipped_rails.add(name)
        return True

    def _board_sources(self):
        for name, r in self.rails.items():
            src = r["source"]
            if src["role"] == "unresolved":
                self.notes.append(f"Rail {name}: no source traced; its loads are not imported")
                self.skipped_rails.add(name)
            elif src["role"] in SOURCE_ROLES:
                v = self._voltage(r)
                out = self._source_pins(src, f"Source of {name}")
                # A board input one or more series parts from its connector (a reverse-
                # battery FET, a fuse): a second rail from the same connector pin is
                # behind the source already placed, through its own parts.
                if v is None or self._behind_placed_output(name, src, out) or \
                        self._off_its_net(name, src, out):
                    continue
                if out is None:
                    self.notes.append(f"Rail {name}: source {src.get('refdes') or '?'} not "
                                      "found on the board; its loads are not imported")
                    self.skipped_rails.add(name)
                    continue
                ret = src.get("return") if isinstance(src.get("return"), dict) else {}
                self.setup.vrms.append(Vrm(self._unique(f"{src.get('refdes') or name} {name}"),
                                           out, v, "source",
                                           ground=self._ground(src.get("refdes"),
                                                               ret.get("net") or r.get("ground")),
                                           # A module's rated output for this supply.
                                           current_limit_a=amps(src.get("max_output_current")),
                                           **self._input_range(r)))
                # The parts between the connector's pin and the rail carry its current.
                if src.get("output_path"):
                    self._output_path(src.get("refdes", ""), src, name)

    def _input_range(self, rail: dict) -> dict:
        """A board input's stated range, as the source's accuracy in volts."""
        volts = rail.get("voltage") or {}
        lo, hi = number(volts.get("min")), number(volts.get("max"))
        if lo is None and hi is None:
            return {}
        return dict(acc_min=lo, acc_max=hi, acc_pct=False, acc_dv=False)

    # ------------------------------------------------------------ regulators
    def _regulator_entries(self, reg: dict) -> List[dict]:
        """The regulator's load entries on its input rails, one per input."""
        out = []
        for rail_name in reg.get("input_rails", []):
            for l in self.rails.get(rail_name, {}).get("loads", []):
                if l["role"] == "regulator" and l["refdes"] == reg["refdes"]:
                    out.append(l)
                    break
        return out

    def _regulator_entry(self, reg: dict) -> Optional[dict]:
        """The regulator's load entry on its (first) input rail."""
        return next(iter(self._regulator_entries(reg)), None)

    def _regulator_input(self, reg: dict):
        """The regulator's pins on its input rail, from its load entry there."""
        l = self._regulator_entry(reg)
        return self.idx.pin_ref(l["pins"], self.notes, f"{reg['refdes']} input") if l else None

    def _regulator_kind(self, reg: dict, rail: str) -> Tuple[str, float]:
        # A part with both kinds of output (a PMIC's bucks beside its LDOs) types each
        # output: a linear one draws its output current from the input, not its power.
        own = (reg.get("output_kinds") or {}).get(rail)
        kind = VRM_KIND.get(own or reg.get("type"))
        if own == "linear":
            return kind, 1.0
        eff = number(reg.get("efficiency"))
        if kind is None:
            self.notes.append(f"{reg['refdes']} ({rail}): not known whether linear or "
                              "switching; modeled as an ideal source, drawing nothing from "
                              "its input rail")
            return "source", 1.0
        if kind in SWITCHING_KINDS and eff is None:
            self.notes.append(f"{reg['refdes']} ({rail}): efficiency not read from its "
                              "datasheet; solved lossless, so its input current and the rail "
                              "feeding it are understated")
            return kind, 1.0
        return kind, eff if eff is not None else 1.0

    def _output_rating(self, src: dict, reg: dict, rail: str) -> Optional[float]:
        """The rated current of the output making this rail: the rail source's own
        ``max_output_current`` (one channel of a multi-output part, a PMIC's switcher D
        4 A beside its 0.5 A LDO), else the regulator's when it has one output. A part
        with several outputs and no figure for this one is not rated: its one figure is
        some output's, not necessarily this one's."""
        own = amps(src.get("max_output_current"))
        if own is not None:
            return own
        part = amps(reg.get("max_output_current"))
        if part is not None and len(reg.get("output_rails") or []) > 1:
            self.notes.append(f"{reg['refdes']} ({rail}): no rating for this output of a "
                              f"part with several; its {part:g} A is not applied")
            return None
        return part

    def _accuracy(self, rail: dict, ref: str) -> dict:
        """The VRM's DC accuracy in volts from the rail's ``regulator_band``: the band at
        the board's temperature grade, else at 25 C. Nothing when the file gives none."""
        band = rail.get("regulator_band")
        if not isinstance(band, dict):
            return {}
        for key in ("at_grade", "at_25c"):
            b = band.get(key)
            if not isinstance(b, dict):
                continue
            lo, hi = number(b.get("min_v")), number(b.get("max_v"))
            if lo is None and hi is None:
                continue
            grade = number(band.get("grade_c"))
            where = f"at {grade:g} C" if key == "at_grade" and grade is not None else "at 25 C"
            self.accuracies.append(f"{ref} {rail['name']} {_volts_range(lo, hi)} {where}")
            return {"acc_min": lo, "acc_max": hi, "acc_pct": False, "acc_dv": False}
        why = band.get("not_known_because")
        if why:
            self.notes.append(f"{ref} ({rail['name']}): output accuracy not known: "
                              + "; ".join(map(str, why)))
        return {}

    def _regulator_grounds(self, reg: dict, rail: dict, kind: str):
        """(ground, output ground) pins of a regulator. A source VRM's ground pins are a 0 V
        reference; a regulator only standing in as one is not the board's reference, so
        its ground floats with the copper (one reference, at the board input). An isolated
        converter returns its input on its input rail's ground and holds its output rail's
        ground - isolated from it - at 0 V at its own pins there."""
        if kind == "source":
            return None, None
        ref = reg["refdes"]
        out_net = rail.get("ground")
        in_rail = next(iter(reg.get("input_rails") or []), None)
        in_net = self.rails.get(in_rail, {}).get("ground") if in_rail else None
        if reg.get("type") != "isolated":
            return self._ground(ref, out_net), None
        ground = self._ground(ref, in_net)
        out_ground = self._ground(ref, out_net)
        if out_ground is None or ground is None or out_ground.net == ground.net:
            self.notes.append(f"{ref} ({rail['name']}): isolated converter whose output "
                              "ground pins were not found; its output returns as if on its "
                              "input's ground")
            return ground, None
        return ground, out_ground

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
            if self._behind_placed_output(name, src, out):
                continue
            if self._off_its_net(name, src, out):
                continue
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
            ground, out_ground = self._regulator_grounds(reg, r, kind)
            input_ground = None
            if kind == "isolated":
                if out_ground is None:
                    kind = "smps"          # its output returns as if on its input's ground
                else:
                    ground, input_ground = out_ground, ground
            self.setup.vrms.append(Vrm(self._unique(f"{reg['refdes']} {name}"), out, v, kind,
                                       efficiency=eff, input=vin,
                                       quiescent_a=amps(reg.get("quiescent_current")) or 0.0,
                                       dropout_v=_volts(reg.get("dropout_voltage")) or 0.0,
                                       vin_min=_volts(reg.get("input_voltage_min")),
                                       vin_max=_volts(reg.get("input_voltage_max")),
                                       # Its datasheet's rated output current: this
                                       # output's own on a part with several outputs.
                                       current_limit_a=self._output_rating(src, reg, name),
                                       ground=ground, input_ground=input_ground,
                                       **self._accuracy(r, reg["refdes"])))
            self._remote_sense(self.setup.vrms[-1], src, name)
            if vin is not None:
                self._input_filter(reg)
            self._output_path(reg["refdes"], src, name, in_loop=kind in SWITCHING_KINDS)

    def _remote_sense(self, vrm: Vrm, src: dict, rail: str):
        """The file's ``source.sense``: the pads the regulator holds at its set voltage -
        its own sense pins when the feedback is tied straight to the rail, else the point
        on the rail they are tied to (``at``: a feedback divider's top pad, a net tie's) -
        and its return-sense pads on the rail's ground, when the VRM's ground is simulated.
        Solved at the output pad instead, a remote-sensed rail reads the whole copper drop
        as a load deficit."""
        sense = src.get("sense")
        if not isinstance(sense, dict) or not sense.get("pins"):
            return
        point = sense.get("at") if isinstance(sense.get("at"), dict) else sense
        pins = self.idx.pin_ref(point.get("pins", []), self.notes, f"{vrm.name} sense")
        # The sense sits on the regulated rail: a switcher's output pins are on its
        # switch node, the rail is the far side of its inductor, one DC connection away.
        rail_net = self.idx.board_net(rail)
        if pins is None or pins.net not in {vrm.output.net, rail_net}:
            where = f" on {pins.net}" if pins is not None else ""
            self.notes.append(f"{vrm.name}: sense pins {', '.join(point.get('pins', []))}"
                              f"{where} are not on its rail; regulated at its output pins")
            return
        vrm.sense = pins
        names = ", ".join(sense.get("pin_names") or sense.get("pins", []))
        ret = sense.get("return") if isinstance(sense.get("return"), dict) else None
        gsense = None
        if ret and vrm.ground is not None:
            gsense = self.idx.pin_ref(ret.get("pins", []), self.notes, f"{vrm.name} return sense")
            if gsense is not None and gsense.net == vrm.ground.net:
                vrm.ground_sense = gsense
            else:
                gsense = None
        self.notes.append(f"{vrm.name}: remote sense at {pins.label()} ({names}) on {pins.net}"
                          + (f", return sense at {gsense.label()}" if gsense else ""))

    def _output_path(self, ref: str, src: dict, rail: str, in_loop: bool = False):
        """The parts between a regulator's output pins and its rail, as DC connections.
        A switcher regulates its rail AFTER them (its feedback divider sits on the rail),
        so with ``in_loop`` they lower nothing: shorts, their loss in the efficiency."""
        parts = src.get("output_path", [])
        path = path_to_rail(parts, src.get("output_net", ""), rail)
        off = sorted({e["refdes"] for e in parts} - {e["refdes"] for e in path})
        if off:
            self.notes.append(f"{ref} ({rail}): {', '.join(off)} listed on its "
                              "output path but not between its output and the rail; left out")
        for e in path:
            self._link(e, loop_of=ref if in_loop else None)

    def _input_filter(self, reg: dict, entry: Optional[dict] = None):
        """The parts between a regulator's input rail and its input pins (a ferrite or an
        inductor ahead of it), as DC connections: a real drop, before any regulation."""
        for e in (entry or self._regulator_entry(reg) or {}).get("input_filter") or []:
            self._link(e)

    def _load_switch(self, reg: dict, src: dict, rail: str):
        """A power or load switch regulates nothing: its output is its input less I times
        its on-resistance, so it is a DC connection from its input pins to its output
        pins, not an LDO (which would make 5 V from 5 V and be out of regulation). The
        parts between its input rail and its input pins (an input filter) and between its
        output pins and its rail are DC connections too. Its quiescent current is drawn
        at its input pins, as an LDO VRM's is."""
        ref = reg["refdes"]
        if ref in self.linked:
            # A second rail behind the same switch (one switch feeding a rail, and
            # another behind a 0R from it): the switch is already one DC connection with
            # its quiescent load; a second would halve its on-resistance and draw its
            # quiescent twice.
            self._output_path(ref, src, rail)
            return
        entry = self._regulator_entry(reg)
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
        self._input_filter(reg, entry)
        self._output_path(ref, src, rail)
        iq = amps(reg.get("quiescent_current"))
        if iq is not None:
            self.setup.loads.append(Load(self._unique(f"{ref} quiescent"), vin, iq,
                                         ground=self._ground(ref)))

    # ------------------------------------------------------------ series parts
    def _link(self, e: dict, loop_of: Optional[str] = None):
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
                              "a resistive DC solve, so it is modeled as a short (import its "
                              "SPICE model on the Series links tab for its drop)")
            typ, mx = 0.0, None
        if loop_of:
            self.notes.append(f"{ref}: inside {loop_of}'s regulation loop, so its DC "
                              "resistance lowers nothing (its loss is in the efficiency); "
                              "modeled as a short")
            typ, mx, fig_notes = 0.0, None, []
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

    def _ground_links(self):
        """The parts joining one ground net to another (a common-mode choke's return
        winding, a 0R, a net-tie) as DC connections, so a return reaches the reference
        through them. Capacitors, TVS diodes and resistors above 1 kOhm carry no DC to
        speak of; shield nets carry no load current."""
        for g in self.pdn.get("grounds", []):
            if g.get("kind") == "shield":
                continue
            for c in g.get("connections", []):
                cls = str(c.get("class", "")).lower()
                if cls in ("capacitor", "tvs", "diode", "esd"):
                    continue
                ohms = parse_resistance(str(c.get("value", ""))) if cls == "resistor" else None
                if ohms is not None and ohms > GROUND_LINK_MAX_OHMS:
                    continue
                self._link({"refdes": c["refdes"], "class": cls, "net_in": g["net"],
                            "net_out": c.get("to"), "datasheet_figures": [],
                            "resistance_ohm": ohms if ohms is not None else "unknown"})

    # ------------------------------------------------------------ loads
    def _tolerance(self, rail: dict, l: dict, label: str = "") -> dict:
        """The load's PASS / FAIL range: the rail's stated minimum and maximum, in % from
        nominal, or in volts when the datasheet's supply range (``supply_min``,
        ``supply_max``) is stricter and takes its place; on a rail a board input feeds,
        the datasheet's range always takes it (the rail's range is the source's own, a
        battery's 10 V minimum, where a load fails at any copper drop). An IC whose
        datasheet states no supply range is named: a value still to find."""
        v = rail.get("voltage", {})
        nom, lo, hi = number(v.get("nominal")), number(v.get("min")), number(v.get("max"))
        smin, smax = (number(x.get("volts")) if isinstance(x, dict) else None
                      for x in (l.get("supply_min"), l.get("supply_max")))
        if smin is None and smax is None and l.get("role") in IC_ROLES:
            self.no_supply_range.append(label or l["refdes"])
        if not nom:
            return {}
        if smin is not None or smax is not None:
            from_source = (rail.get("source") or {}).get("role") in SOURCE_ROLES
            low = smin if smin is not None and (from_source or lo is None or smin > lo) else lo
            high = smax if smax is not None and (from_source or hi is None or smax < hi) else hi
            if nom < 0 or (smin or 0) < 0 or (smax or 0) < 0:
                self.notes.append(f"{l['refdes']} on {rail['name']}: supply range "
                                  f"{_volts_range(smin, smax)} not applied on a negative "
                                  "rail; set its tolerance by hand")
            elif (low, high) != (lo, hi):
                return {"tol_min": low, "tol_max": high, "tol_pct": False}
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
            # What the datasheet gives is the part's own (static) current: the least it
            # draws, with what it drives on top. That is solved, never 0 A.
            self.notes.append(f"{l['refdes']} on {rail}: draws what it drives "
                              f"({l.get('load_dependent_reason', 'no reason given')})")
            static = typ if typ is not None else mx
            if static is None:
                self.unknown_current.append(label)
                return 0.0
            self.static_current[label] = static
            return static
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
            current = l.get("current")
            if isinstance(current, dict) and current.get("basis") in NOT_A_LOAD:
                # On the rail by a detect, enable or reference pin only: it draws nothing.
                self.not_supplied.append(f"{l['refdes']} ({name})")
                continue
            # A motor driven off the board: its current leaves the rail at the driver's
            # supply pins and returns at the driver's ground pins (the connector's
            # terminals sit on the motor phases, not on the rail).
            drv = l.get("driver") if isinstance(l.get("driver"), dict) else {}
            supply = self.idx.pin_ref(drv.get("supply_pins") or l["pins"], self.notes,
                                      f"Load {l['refdes']} on {name}")
            if supply is None:
                continue
            taken = {x.name for x in self.setup.loads}
            label = self._unique(l["refdes"] if l["refdes"] not in taken
                                 else f"{l['refdes']} ({name})")
            current = self._current(l, name, label)
            # The parts between the rail and pins behind a ferrite (input_net): DC
            # connections, so the load is reached through them.
            for e in l.get("input_filter") or []:
                if e.get("refdes") == l["refdes"]:
                    self.notes.append(f"{label}: listed in its own input filter; not a "
                                      "series part")
                    continue
                self._link(e)
            ground = (self.idx.pin_ref(drv["return_pins"], self.notes,
                                       f"Return of {l['refdes']} on {name}")
                      if drv.get("return_pins") else None)
            self.setup.loads.append(Load(label, supply, current,
                                         **self._tolerance(rail, l, label),
                                         ground=ground or self._ground(_return_part(l),
                                                                       rail.get("ground"))))
        for pin in rail.get("offboard_pins", []):
            supply = self.idx.pin_ref([pin], self.notes, f"Off-board pin {pin} on {name}")
            if supply is not None:
                label = self._unique(f"{pin} (off-board)")
                self.unknown_current.append(label)
                self.setup.loads.append(Load(label, supply, 0.0,
                                             **self._tolerance(rail, {"refdes": pin}),
                                             ground=self._ground(supply.ref,
                                                                 rail.get("ground"))))

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

    def _unreferenced_grounds(self):
        """A ground net carries return current only from a reference: a source VRM's ground
        pins, or an isolated converter's output ground pins. A ground with neither (an
        isolated converter whose output ground pins were not found) sends what returns
        there to an ideal ground instead, named once."""
        groups = net_groups(self.setup)
        referenced = {groups[v.ground.net] for v in self.setup.vrms
                      if v.kind in GROUND_REF_KINDS and v.ground}
        ideal = set()
        for x in [*self.setup.vrms, *self.setup.loads]:
            if x.ground and groups[x.ground.net] not in referenced:
                ideal.add(x.ground.net)
                x.ground = None
        if ideal:
            self.notes.append("No supply returns through " + ", ".join(sorted(ideal))
                              + " (an isolated converter's output side), so what returns "
                              "there goes to an ideal ground")

    # ------------------------------------------------------------ run
    def run(self) -> Tuple[PISetup, List[str]]:
        self._learn_nets()
        self._board_sources()
        self._regulated_rails()
        self._series_parts()
        self._ground_links()
        self._loads()
        self._drop_unpowered()
        self._unreferenced_grounds()
        self._range_notes()
        return self.setup, self.notes

    def _range_notes(self):
        """What a single value per item left out, one note each, for the loads kept."""
        kept = {x.name for x in self.setup.loads}
        if self.not_supplied:
            self.notes.append("On a rail by a detect, enable or reference pin, not a supply "
                              "pin, so not a load: " + ", ".join(self.not_supplied))
        static = [f"{n} ({_amps(a)})" for n, a in self.static_current.items() if n in kept]
        if static:
            self.notes.append("Draw what they drive: their own (static) current from the "
                              "datasheet is solved, what they drive is on top and not known: "
                              + ", ".join(static))
        no_range = [n for n in self.no_supply_range if n in kept]
        if no_range:
            self.notes.append("No supply voltage range is stated for these ICs, so they "
                              "are not judged PASS / FAIL; enter it on the Loads tab when "
                              "known: " + ", ".join(no_range))
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
        if self.accuracies:
            self.notes.append("Output accuracy from the datasheets: " + "; ".join(self.accuracies))


def _amps(a: float) -> str:
    if abs(a) < 1e-3:
        return f"{a * 1e6:.4g} µA"
    return f"{a * 1e3:.4g} mA" if abs(a) < 1 else f"{a:.4g} A"


def _ohms(r: float) -> str:
    return f"{r * 1e3:.4g} mΩ" if abs(r) < 1 else f"{r:.4g} Ω"


def _volts_range(lo: Optional[float], hi: Optional[float]) -> str:
    return " to ".join("?" if x is None else f"{x:.4g}" for x in (lo, hi)) + " V"


def import_tomachie_pdn(path, snap: BoardSnapshot,
                        base: Optional[PISetup] = None) -> Tuple[PISetup, List[str]]:
    """The setup described by a Tomachie ``.pdn.json`` on this board, and notes on what
    could not be placed."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise PdnImportError(f"{path.name} is too large to be a power-tree file")
    pdn = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pdn, dict):
        raise PdnImportError(f"{path.name} is not a {SCHEMA} file")
    try:
        return TomachiePdnImporter(pdn, snap, base).run()
    except (AttributeError, TypeError, KeyError) as e:
        # A field of the wrong kind (a list where an object belongs, say): the file is
        # not one Tomachie wrote, or is damaged.
        raise PdnImportError(f"{path.name} is not a valid {SCHEMA} file "
                             f"({type(e).__name__}: {e})") from e
