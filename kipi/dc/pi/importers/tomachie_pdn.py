# Portions Copyright (c) 2026 Tomachie LLC.
"""Read a Tomachie power-tree file (``<design>.pdn.json``, schema ``tomachie-pdn`` v1) into
a :class:`~kipi.dc.pi.model.PISetup`.

The file comes from the schematic and the parts' datasheets: every rail, its voltage
and source, the parts in series between rails, every load with its typical and maximum
current. What maps where:

* a rail sourced by a connector, battery or module pin -> a ``source`` VRM;
* a rail sourced by a regulator -> an ``ldo`` / ``smps`` VRM whose input pins are the
  regulator's pins on its input rail (the file also lists those as a load of the input
  rail; that entry is not imported, because KiPI derives a VRM's input current itself).
  An isolated converter is an ``isolated`` VRM: its input power is its output power over
  its efficiency, plus its no-load current; its ground pins are its output side's 0 V
  reference and its input ground pins its input side's return;
* a part in series between rails, and a switcher's inductor -> a DC connection;
* a load switch (a regulator entry of type ``load_switch``: it passes its input to its
  output and regulates nothing) -> a DC connection from its input pins to its output
  pins with its on-resistance, and its quiescent current a load on its input pins;
* a load -> a load with its typical current, and a tolerance from its rail's stated
  minimum and maximum, tightened to the datasheet's minimum and maximum supply voltage
  where those are stricter; its pre-layout copper margin (``copper_margin``) is kept
  beside it;
* a regulator's output band (``regulator_band``: its datasheet accuracy with its
  feedback divider's tolerance, at the board's temperature grade where given) -> the
  VRM's DC accuracy, in volts; a worst-case solve runs the VRM at its low end;
* a series part's ESL -> its DC connection's ESL; each capacitor's part number, value
  and ESL -> one capacitor model per part number (AC), filling only what a model of
  that name does not already hold.

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
import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.ac.model import CapModel, Probe
from kipi.core.model.parts import parse_capacitance, parse_resistance
from kipi.dc.pi.importers.pad_index import PadIndex
from kipi.dc.pi.importers.tomachie_converter_inputs import converter_input_probes
from kipi.dc.pi.importers.tomachie_ac import (
    MODULE_NO_STEP, STEP_BECAUSE, defaults_notes, doubt_notes, fill_cap_model, load_rise, load_step,
    maker_target, vrm_bandwidth_khz,
)

from kipi.dc.pi.importers.pdn_figures import (
    amps, curve_points, dc_resistance, forward_drop, number, rated_current,
)
from kipi.dc.pi.model import (
    GROUND_REF_KINDS, SWITCHING_KINDS, DcLink, Load, PinGroup, PinRef, PISetup, Vrm, group_name,
    net_groups, volts_to_pct,
)
from kipi.dc.pi import series_z
from kipi.dc.pi.diode_curve import DIODE_MIN_OHMS, diode_ref

# A part on a rail by a signal pin only (detect, enable, reference): 0 A, not a load.
# "signal_pin_only" since Tomachie 7710456a; "not_a_supply" in files written before.
NOT_A_LOAD = ("signal_pin_only", "not_a_supply")
# Loads whose supply range is the datasheet's to state (a resistor's or a connector's is
# not): one not stated is a value still to find.
IC_ROLES = ("ic", "ic_with_regulator")

SCHEMA = "tomachie-pdn"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 64 << 20       # a power-tree file is kilobytes; this is not one
SOURCE_ROLES = ("board_input", "battery", "generator")
# A resistor joining two ground nets above this carries no DC to speak of (a shield's 1M).
GROUND_LINK_MAX_OHMS = 1e3
VRM_KIND = {"linear": "ldo", "switching": "smps", "isolated": "isolated"}



def _return_part(load: dict) -> str:
    """The part a load's current returns to ground through: a resistor / LED chain's
    last part, the part a pull resistor pulls (pull_to, <refdes>_<pin>), else the load."""
    if load.get("role") == "pull_resistor" and "_" in (load.get("pull_to") or ""):
        return load["pull_to"].rsplit("_", 1)[0]
    return (load.get("path_parts") or [load["refdes"]])[-1]

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
        if base is None:
            # The file gives every load's current, and what a load that passes power on or
            # sends it off the board dissipates itself: a new setup from it heats the
            # board with its loads. A setup imported onto keeps its own choice.
            self.setup.thermal.load_power = True
            self.notes.append("Thermal: load power is included (Thermal settings tab), "
                              "from the loads' currents and stated heat")
        self._board_temperature()
        self._via_plating()
        # One value per item is solved; the other ends of a range, summed up at the end.
        self.unused_current: List[Tuple[str, str]] = []     # (load, what is left out)
        # The maxima, solved in a worst-case solve (kipi.dc.pi.corner).
        self.max_current: Dict[str, float] = {}
        self.max_resistance: List[str] = []
        self.maker_z: Dict[str, List[str]] = {}
        self.unknown_current: List[str] = []
        self.static_current: Dict[str, float] = {}   # solved at a static figure, more not known
        self.not_supplied: List[str] = []    # on a rail by no supply pin: not a load
        self.no_supply_range: List[str] = []  # ICs whose datasheet supply range is not known
        self.rails: Dict[str, dict] = {r["name"]: r for r in pdn.get("rails", [])}
        self.regulators: Dict[str, dict] = {g["refdes"]: g for g in pdn.get("regulators", [])}
        self.skipped_rails: Set[str] = set()
        self.linked: Set[str] = set()
        self.accuracies: List[str] = []          # VRM output bands taken from the file
        self.out_before_copper: List[Tuple[str, str]] = []   # (load, by how much)
        # AC values (spec revision 9): what came from estimates, and the probes added.
        self.estimated_bandwidths: List[str] = []
        self.new_probes: List[Tuple[Probe, dict]] = []
        self.targets_set: List[str] = []
        self.targets_missing: Dict[str, List[str]] = {}
        self.targets_not_needed: List[str] = []
        self.rail_levels: Dict[str, List[float]] = {}   # rail -> its selectable voltages
        self.probes_without_ground: List[str] = []
        self.vrm_regs: Dict[str, dict] = {}
        self.cells: Set[str] = set()

    # ------------------------------------------------------------ helpers
    def _board_temperature(self):
        if "ambient_max_c" not in self.pdn:
            return
        t = number(self.pdn.get("ambient_max_c"))
        if t is None:
            self.notes.append("Temperature: the file states no maximum ambient; the solves "
                              "keep the setup's temperatures")
            return
        s = self.setup
        s.thermal = dataclasses.replace(s.thermal, ambient_c=t, copper_at_ambient=True)
        s.ac = dataclasses.replace(s.ac, temperature_c=t,
                                   temperature="thermal" if s.ac.temperature == "thermal"
                                   else "fixed")
        self.notes.append(f"Temperature: every solve at the board's maximum ambient, {t:g} °C: "
                          f"copper resistivity, parts with a temperature model, capacitor "
                          f"dielectrics (Thermal and AC settings)")

    # pdn.json via_plating (revision 20) key -> the via rule kinds it sets.
    _VIA_PLATING = {"through_um": ("through", "pth", "any"), "blind_um": ("blind", "blind_buried"),
                    "buried_um": ("buried",), "microvia_um": ("micro",)}

    def _via_plating(self):
        """The via barrel plating the file states - no board file carries it, so Tomachie
        writes the IPC-6012 class it assumes - into the setup's via rules, each rule's
        drill range and fill kept. The designer changes it in the Via rules editor."""
        vp = self.pdn.get("via_plating")
        if not isinstance(vp, dict):
            return
        set_to = []
        for key, kinds in self._VIA_PLATING.items():
            um = number(vp.get(key))
            if um is None or um <= 0:
                continue
            for r in self.setup.settings.via_rules:
                if r.kind in kinds:
                    r.plating_mm = um / 1000.0
            set_to.append(f"{key[:-3]} {um:g} um")
        if set_to:
            self.notes.append(f"Via plating from the file ({vp.get('basis') or 'not stated'}): "
                              f"{', '.join(set_to)}; change it in Via rules")

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
        """The rail's voltage: its nominal, else, for a rail whose level a pin or firmware
        selects (``voltage.levels``), its power-up default (``voltage.default_level``),
        else the lowest level; the others are kept for the VRM editor and noted."""
        volts = rail.get("voltage") or {}
        v = number(volts.get("nominal"))
        levels = sorted({x for x in (number(y) for y in volts.get("levels") or []) if x})
        if v is None and levels:
            dl = volts.get("default_level")
            default = number(dl.get("volts")) if isinstance(dl, dict) else number(dl)
            v = default if default in levels else levels[0]
            how = "its power-up default" if default in levels else "the lowest of its levels"
            others = ", ".join(f"{x:g} V" for x in levels if x != v)
            self.rail_levels[rail["name"]] = levels
            self.notes.append(f"Rail {rail['name']}: solved at {v:g} V ({how}); it also runs "
                              f"at {others}: pick the level on the VRMs tab (Level) to solve "
                              "it there")
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
        self._output_path(src.get("refdes", ""), src, name, in_loop=False)
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
                                           **self._input_range(r, src)))
                # The parts between the connector's pin and the rail carry its current.
                if src.get("output_path"):
                    self._output_path(src.get("refdes", ""), src, name)

    def _input_range(self, rail: dict, src: dict) -> dict:
        """A board input's stated range, as the source's accuracy in volts (the worst case
        solves it at its minimum), and its cable allowance (a USB Type-C receptacle's)."""
        out: dict = {}
        volts = rail.get("voltage") or {}
        lo, hi = number(volts.get("min")), number(volts.get("max"))
        if lo is not None or hi is not None:
            out.update(acc_min=lo, acc_max=hi, acc_pct=False, acc_dv=False)
        cable = src.get("cable") if isinstance(src.get("cable"), dict) else {}
        ohms = number(cable.get("resistance_ohm"))
        if ohms:
            out["cable_mohm"] = ohms * 1e3
        return out

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

    def _regulator_input(self, reg: dict):
        """The regulator's pins on its input rail, from its load entry there."""
        l = self._regulator_entry(reg)
        return self.idx.pin_ref(l["pins"], self.notes, f"{reg['refdes']} input") if l else None

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

    def _regulated_rails(self):
        for name, r in self.rails.items():
            src = r["source"]
            if src["role"] != "regulator":
                continue
            reg = self.regulators.get(src.get("refdes", ""))
            if reg is not None and reg.get("type") == "load_switch":
                self._load_switch(reg, src, name)
                continue
            if reg is not None and reg.get("type") == "charger":
                self._charger(reg, src, name)
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
            bandwidth_khz, bw_basis = vrm_bandwidth_khz(reg)
            if bw_basis == "estimated":
                how = (reg.get("ac") or {}).get("bandwidth", {}).get("from", "")
                self.estimated_bandwidths.append(f"{reg['refdes']} ({how})" if how else reg["refdes"])
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
                                       bandwidth_khz=bandwidth_khz,
                                       levels=self.rail_levels.get(name, []),
                                       **self._accuracy(r, reg["refdes"])))
            self.vrm_regs[self.setup.vrms[-1].name] = reg
            self._remote_sense(self.setup.vrms[-1], src, name)
            if vin is not None:
                self._input_filter(reg)
            self._output_path(reg["refdes"], src, name, in_loop=kind in SWITCHING_KINDS)

    def _remote_sense(self, vrm, src: dict, rail: str):
        """The file's ``source.sense``: the pads the regulator holds at its set voltage -
        its own sense pins when the feedback is tied straight to the rail, else the
        divider's top-resistor pads on the rail (``at``) - and its return-sense pads on
        the rail's ground, when the VRM's ground is simulated. Solved at the output pad
        instead, a remote-sensed rail reads the whole copper drop as a load deficit."""
        sense = src.get("sense")
        if not isinstance(sense, dict) or not sense.get("pins"):
            return
        point = sense.get("at") if isinstance(sense.get("at"), dict) else sense
        pins = self.idx.pin_ref(point.get("pins", []), self.notes, f"{vrm.name} sense")
        # The sense sits on the regulated rail: a switcher's output pins are on its
        # switch node, the rail is the far side of its inductor, one DC link away.
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

    def _load_switch(self, reg: dict, src: dict, rail: str):
        """A power or load switch regulates nothing: its output is its input less I times
        its on-resistance, so it is a DC connection from its input pins to its output
        pins, not an LDO (which would make 5 V from 5 V and be out of regulation). The
        parts between its input rail and its input pins (an input filter) and between its
        output pins and its rail are DC connections too. Its quiescent current is drawn
        at its input pins, as an LDO VRM's is."""
        ref = reg["refdes"]
        if ref in self.linked:
            # A second rail behind the same switch (MB2059 U14: 5V_VIN, and VL behind
            # R68 0R): the switch is already one DC connection with its quiescent load;
            # a second would halve its on-resistance and draw its quiescent twice.
            self._output_path(ref, src, rail)
            return
        inputs = [(l, self.idx.pin_ref(l["pins"], self.notes, f"{ref} input"))
                  for l in self._regulator_entries(reg)]
        inputs = [(l, p) for l, p in inputs if p is not None]
        out = self._source_pins(src, f"{ref} output")
        if not inputs or out is None:
            self.notes.append(f"Rail {rail}: load switch {ref} "
                              f"{'input' if not inputs else 'output'} pins not found on the "
                              "board; its loads are not imported")
            self.skipped_rails.add(rail)
            return
        self.linked.add(ref)
        typ, mx, fig_notes = dc_resistance({"datasheet_figures":
                                            reg.get("datasheet_resistances") or []})
        vin = inputs[0][1]
        mux = ref if len(inputs) > 1 else ""
        first = self._add_link(ref, vin, out, typ, mx, fig_notes, "on-resistance",
                               rated_a=amps(reg.get("max_output_current")),
                               name=f"{ref} ({vin.net})" if mux else None, mux=mux)
        for k, (entry, pins) in enumerate(inputs[1:], 1):
            self.setup.mux_links.append(dataclasses.replace(
                first, name=self._unique(f"{ref} ({pins.net})"), side_a=pins, mux_input=k))
            self._input_filter(reg, entry)
        if mux:
            self.notes.append(f"{ref} ({rail}): power mux, a DC connection to {out.net} "
                              f"from each of its {len(inputs)} inputs "
                              f"({', '.join(p.net for _, p in inputs)}), one closed at a "
                              "time; each input is solved as a path of its own")
        else:
            self.notes.append(f"{ref} ({rail}): load switch, imported as a DC connection "
                              f"from {vin.net} to {out.net}, not a regulator")
        self._input_filter(reg, inputs[0][0])
        self._output_path(ref, src, rail)
        iq = amps(reg.get("quiescent_current"))
        if iq is not None:
            self.setup.loads.append(Load(self._unique(f"{ref} quiescent"), vin, iq,
                                         ground=self._ground(ref)))

    def _charger(self, reg: dict, src: dict, rail: str):
        ref = reg["refdes"]
        cell = reg.get("cell") if isinstance(reg.get("cell"), dict) else {}
        if not cell.get("refdes"):
            self.notes.append(f"Rail {rail}: battery charger {ref} with no battery or "
                              "connector on its output; its loads are not imported")
            self.skipped_rails.add(rail)
            return
        r = self.rails[rail]
        if self._voltage(r) is None:
            return
        self._output_path(ref, src, rail)
        if rail != cell.get("rail"):
            return
        out = self.idx.pin_ref(cell.get("pins", []), self.notes, f"Cell {cell['refdes']}")
        if out is None:
            self.notes.append(f"Rail {rail}: the cell {cell['refdes']} of charger {ref} "
                              "not found on the board; its loads are not imported")
            self.skipped_rails.add(rail)
            return
        self.cells.add(cell["refdes"])
        self.setup.vrms.append(Vrm(self._unique(f"{cell['refdes']} {rail}"), out,
                                   self._voltage(r), "source",
                                   ground=self._ground(cell["refdes"], r.get("ground")),
                                   **self._input_range(r, {})))
        rails = ", ".join(reg.get("output_rails") or [rail])
        entry = self._regulator_entry(reg)
        vin = self.idx.pin_ref(entry["pins"], self.notes, f"{ref} input") if entry else None
        if vin is None:
            self.notes.append(f"{ref} ({rail}): battery charger; the cell {cell['refdes']} "
                              f"supplies {rails}; its input pins not found, so its input "
                              "current is not drawn")
            return
        self._input_filter(reg, entry)
        label = self._unique(f"{ref} charge input")
        self.setup.loads.append(Load(label, vin, self._current(entry, vin.net, label),
                                     ground=self._ground(ref),
                                     current_max=self.max_current.get(label),
                                     current_known=label not in self.unknown_current))
        self.notes.append(f"{ref} ({rail}): battery charger; the cell {cell['refdes']} "
                          f"supplies {rails} (the battery case), and {ref} draws its input "
                          f"current from {vin.net}; the charge current from {ref}'s "
                          f"output into the cell is not solved in the copper")

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
        esl = _henries(e.get("esl"))
        if e.get("class") == "diode":
            vf, vf_max, slope, vf_notes = forward_drop(e)
            if vf is None:
                self.notes.append(f"{ref}: a diode in series with no forward voltage in the "
                                  "file; modeled as a short, so the rail after it reads high")
                typ, mx = 0.0, None
            else:
                # Anode on the side the power comes from (net_in).
                self.notes.extend(f"{ref}: diode, {n}" for n in vf_notes)
                # A diode needs a series resistance; the solver's floor when the file
                # gives one figure (no slope).
                self._add_link(ref, a, b, max(slope, DIODE_MIN_OHMS), None, [],
                               forward_v=vf, forward_v_max=vf_max,
                               esl_h=esl, vf_curve=curve_points(e), rated_a=rated_current(e))
                return
        if loop_of:
            self.notes.append(f"{ref}: inside {loop_of}'s regulation loop, so its DC "
                              "resistance lowers nothing (its loss is in the efficiency); "
                              "modeled as a short")
            typ, mx, fig_notes = 0.0, None, []
        ac = e.get("ac") if isinstance(e.get("ac"), dict) else {}
        if typ is None and mx is None:
            dcr = _positive_number(ac.get("dcr_ohm")) or _through_ohms(e)
            if dcr:
                typ, fig_notes = dcr, []
        link = self._add_link(ref, a, b, typ, mx, fig_notes, esl_h=esl, rated_a=rated_current(e))
        if not loop_of and e.get("class") in ("inductor", "ferrite"):
            self._maker_z(link, ac, e)

    def _maker_z(self, link: DcLink, ac: dict, e: dict):
        l_points = series_z.l_points_from(e)
        if len(l_points) >= 2:
            link.l_by_current = l_points
            self.maker_z.setdefault("maker's inductance at each DC current", []).append(link.name)
        circuit = series_z.circuit_from(ac) if ac.get("basis") == "maker_equivalent_circuit" else None
        curves = series_z.curves_from(ac)
        points = series_z.points_from(ac)
        if circuit:
            link.circuit, kind = circuit, "maker's equivalent circuit"
        elif len(curves) >= 2:
            link.z_by_current, kind = curves, "maker's measured curves at their DC current"
        elif len(points) >= 2:
            link.z_points, kind = points, "maker's measured curve"
        else:
            return
        link.z_basis = str(ac.get("basis", ""))
        self.maker_z.setdefault(kind, []).append(link.name)

    def _add_link(self, ref: str, a, b, typ: Optional[float], mx: Optional[float],
                  fig_notes: List[str], what: str = "DC resistance",
                  forward_v: Optional[float] = None, forward_v_max: Optional[float] = None,
                  esl_h: Optional[float] = None, vf_curve: Optional[list] = None,
                  rated_a: Optional[float] = None, name: Optional[str] = None,
                  mux: str = "") -> DcLink:
        self.notes.extend(f"{ref}: {n}" for n in fig_notes)
        if typ is None and mx is None:
            self.notes.append(f"{ref}: {what} not known; modeled as a short")
            typ = 0.0
        elif typ is None:
            self.notes.append(f"{ref}: only a maximum {what} is printed; it is used")
            typ = mx
        elif mx is not None and mx != typ:
            self.max_resistance.append(f"{ref} {_ohms(mx)}")
        self.setup.links.append(DcLink(self._unique(name or ref), a, b, typ,
                                       resistance_max=mx if mx is not None and mx != typ
                                       else None,
                                       diode_vf=forward_v or 0.0, diode_vf_max=forward_v_max,
                                       esl_h=esl_h, vf_curve=vf_curve, rated_current_a=rated_a,
                                       mux=mux))
        diode_ref(self.setup.links[-1])       # a diode: its model from the datasheet's
        return self.setup.links[-1]

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

    def _series_parts(self):
        for r in self.rails.values():
            for e in r.get("series_from_parent", []):
                self._link(e)
        for r in self.rails.values():
            self._feed_paths(f"{r['name']} feeds", r.get("other_feeds"), f"Rail {r['name']}")

    def _feed_paths(self, mux: str, feeds, who: str, own=()):
        """Other rails feeding a rail or a load's node through a diode chain, beside its
        own: each chain's parts as a path of its own, closed one path at a time; the
        own path's parts named in ``own`` open with it."""
        feeds = [f for f in feeds or [] if isinstance(f, dict) and f.get("series")]
        if not feeds:
            return
        for link in self.setup.links:
            if link.name in own:
                link.mux, link.mux_input = mux, 0
        for k, f in enumerate(feeds, 1):
            n0 = len(self.setup.links)
            for e in f["series"]:
                self._link(e)
            chain = self.setup.links[n0:]
            del self.setup.links[n0:]
            if not chain:
                continue
            for link in chain:
                link.mux, link.mux_input = mux, k
            self.setup.mux_links.extend(chain)
            self.notes.append(f"{who}: also fed from {f.get('rail')} through "
                              f"{', '.join(x.name for x in chain)}; solved as a path of its "
                              "own")

    # ------------------------------------------------------------ loads
    def _tolerance(self, rail: dict, l: dict, label: str = "") -> dict:
        """The load's PASS / FAIL range: the rail's stated minimum and maximum, in % from
        nominal, or in volts when the datasheet's minimum supply voltage (``supply_min``)
        is higher than the rail's minimum and takes its place; on a rail a board input
        feeds, the datasheet's range always takes it. A load whose datasheet
        states no supply range has none: the rail's range is its source's (a battery's
        10 V minimum), and a worst case solved there fails it at any copper drop. An IC
        so left is named, its range being a value still to find."""
        v = rail.get("voltage", {})
        nom, lo, hi = number(v.get("nominal")), number(v.get("min")), number(v.get("max"))
        smin, smax = (number(x.get("volts")) if isinstance(x, dict) else None
                      for x in (l.get("supply_min"), l.get("supply_max")))
        if smin is None and smax is None:
            if l.get("role") in IC_ROLES:
                self.no_supply_range.append(label or l["refdes"])
            return {}
        if not nom:
            return {}
        if smin is not None or smax is not None:
            # A board input's range is the source's own (USB's 4.75 V, a PXIe slot's
            # 2.97 V), where the worst case solves it: as a load's floor it fails the load
            # at any copper drop. There the datasheet's range is the load's.
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
            # draws, with what it drives on top. That is solved, never 0 A. Its own maximum
            # (an ESP32's radio transmitting) is still its worst case.
            self.notes.append(f"{l['refdes']} on {rail}: draws what it drives "
                              f"({l.get('load_dependent_reason', 'no reason given')})")
            static = typ if typ is not None else mx
            if static is None:
                self.unknown_current.append(label)
                return 0.0
            self.static_current[label] = static
            if mx is not None and mx != static:
                self.max_current[label] = mx
            return static
        if typ is None and mx is not None:
            self.notes.append(f"{label}: only a maximum current is known; {_amps(mx)} is "
                              "used")
            return mx
        if typ is None:
            self.unknown_current.append(label)
            return 0.0
        if mn is not None and mn != typ:
            self.unused_current.append((label, f"min {_amps(mn)}"))
        if mx is not None and mx != typ:
            self.max_current[label] = mx
        return typ

    def _rail_loads(self, rail: dict):
        name = rail["name"]
        for l in rail.get("loads", []):
            if l["role"] == "regulator":
                continue                   # the regulator's VRM input carries this current
            if l["refdes"] in self.cells:
                continue
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
            self._feed_paths(f"{label} feeds", l.get("other_feeds"), label,
                             [e["refdes"] for e in l.get("input_filter") or []
                              if e.get("class") == "diode"])
            returns = _return_part(l)
            ground = (self.idx.pin_ref(drv["return_pins"], self.notes,
                                       f"Return of {l['refdes']} on {name}")
                      if drv.get("return_pins") else None)
            groups, package, io_tied = ([], {}, 0.0) if drv else self._die_supplies(l, label, current)
            if io_tied:
                # The I/O estimate of a load solved as one node: on top of its figure.
                current += io_tied
                if self.max_current.get(label) is not None:
                    self.max_current[label] += io_tied
            self.setup.loads.append(Load(label, supply, current,
                                         **self._tolerance(rail, l, label),
                                         ground=ground or self._ground(returns,
                                                                       rail.get("ground")),
                                         groups=groups,
                                         current_max=self.max_current.get(label),
                                         current_known=label not in self.unknown_current
                                         and label not in self.static_current,
                                         copper_margin_v=self._copper_margin(l, label),
                                         package_ohms=package))
            self._probe(l, label, supply, self.setup.loads[-1].ground)
        for pin in rail.get("offboard_pins", []):
            supply = self.idx.pin_ref([pin], self.notes, f"Off-board pin {pin} on {name}")
            if supply is not None:
                label = self._unique(f"{pin} (off-board)")
                self.unknown_current.append(label)
                # Its current is not in the file: 0 A, not known (so it heats nothing).
                self.setup.loads.append(Load(label, supply, 0.0,
                                             **self._tolerance(rail, {"refdes": pin}),
                                             ground=self._ground(supply.ref,
                                                                 rail.get("ground")),
                                             current_known=False))

    @staticmethod
    def _io_current(g: dict) -> Optional[float]:
        """A die supply's I/O current (revision 19 ``io_current``; revision 18 wrote it as
        ``current`` with ``current_is`` "estimated")."""
        if isinstance(g.get("io_current"), dict):
            return amps(g["io_current"])
        if g.get("current_is") == "estimated":
            return amps(g.get("current"))
        return None

    def _io_note(self, label: str, g: dict, a: float):
        cur = g.get("io_current") if isinstance(g.get("io_current"), dict) else g.get("current")
        why = cur.get("conditions", "") if isinstance(cur, dict) else ""
        self.notes.append(f"{label} [{g.get('name', '?')}]: I/O current {_amps(a)} ESTIMATED, "
                          f"not a data-sheet figure ({why}); change it to the bank's real current")

    def _die_supplies(self, l: dict, label: str, current: float):
        """A load's balls by die supply (revision 18), as pin groups, each ball's package
        resistance by pin number, and the I/O current to add to the load's own when it is
        solved as one node.

        Balls the board joins on one net can feed different supplies inside the part
        (MB2059 U7: F8/F10 VDDIO3, F14 VDDIO1, 1G1.. VDD on VDDIO). Each supply is its own
        group, so two supplies are never one node. The load's current is the group's whose
        row names it (``current_is`` "this_supply"); a supply with none stated draws 0 A.
        A supply's I/O current (revision 19 ``io_current``: what its bank pins drive,
        Tomachie's estimate) adds to whatever the supply draws by its own figure - a
        data-sheet supply current is measured without it. When the file names no one
        supply for the load's current, the load stays one tied node carrying its current
        plus every I/O current. A ball's package resistance is its terminal's (all its
        balls in parallel) times the terminal's ball count: an equal share, stated as
        such."""
        die = l.get("die_supplies")
        if not isinstance(die, list) or not die:
            return [], {}, 0.0
        ref = l["refdes"]
        package: Dict[str, float] = {}
        shared = []
        for g in die:
            pkg = g.get("package") if isinstance(g.get("package"), dict) else None
            ohms, n = (number(pkg.get("ohms")), number(pkg.get("balls_in_terminal"))) if pkg \
                else (None, None)
            if ohms and n:
                shared.append(g.get("name", "?"))
                for p in g.get("pins", []):
                    package[p.split("_", 1)[1] if p.startswith(ref + "_") else p] = ohms * n
        if shared:
            self.notes.append(f"{label}: package resistance per ball is its terminal's times "
                              f"its ball count (an equal share) for {', '.join(shared)}")
        owners = [g for g in die if g.get("current_is") == "this_supply"]
        io = {id(g): self._io_current(g) for g in die}
        with_io = [g for g in die if io[id(g)]]
        load_has_own = label not in self.unknown_current
        if not owners and with_io and not load_has_own:
            # No figure of its own (an FPGA's VCCO banks): the I/O currents are its current.
            self.unknown_current.remove(label)
        elif len(die) < 2 or len(owners) != 1:
            tied = sum(io[id(g)] for g in with_io)
            for g in with_io:
                self._io_note(label, g, io[id(g)])
            if len(die) >= 2:
                self.notes.append(f"{label}: {len(die)} die supplies, but the file names none "
                                  "as the current's; solved as one node"
                                  + (f" with the I/O currents ({_amps(tied)}) added"
                                     if tied else ""))
            return [], package, tied
        groups, unstated = [], []
        mx = self.max_current.get(label)
        for g in die:
            pins = [p.split("_", 1)[1] if p.startswith(ref + "_") else p for p in g.get("pins", [])]
            name = g.get("name", "")
            extra = io[id(g)] or 0.0
            if extra:
                self._io_note(label, g, extra)
            if owners and g is owners[0]:
                groups.append(PinGroup(pins, current + extra, name,
                                       mx + extra if mx is not None else None))
                continue
            if extra:
                groups.append(PinGroup(pins, extra, name, extra))
                continue
            unstated.append(name or "?")
            groups.append(PinGroup(pins, 0.0, name, 0.0 if mx is not None else None))
        whose = (f"its current is {owners[0].get('name')}'s" if owners
                 else "no current of its own; its I/O currents are solved")
        self.notes.append(f"{label}: {whose}"
                          + (f"; {', '.join(unstated)} draw{'s' if len(unstated) == 1 else ''} "
                             "none stated (0 A solved)" if unstated else ""))
        return groups, package, 0.0

    def _copper_margin(self, l: dict, label: str) -> Optional[float]:
        """The load's pre-layout copper margin, volts: what its supply copper and ground
        return may drop together before it is below its minimum supply, from its rail's
        lowest output less the series parts. Below 0 it is out before any copper; so is
        a load whose maximum supply its rail's highest output is above."""
        cm = l.get("copper_margin")
        if not isinstance(cm, dict):
            return None
        v, high = number(cm.get("v")), number(cm.get("high_room_v"))
        if v is not None and v < 0:
            self.out_before_copper.append((label, f"{-v * 1e3:.4g} mV below its minimum"))
        if high is not None and high < 0:
            self.out_before_copper.append((label, f"{-high * 1e3:.4g} mV above its maximum "
                                                  "at its rail's highest output"))
        return v

    def _loads(self):
        for name, r in self.rails.items():
            if name not in self.skipped_rails:
                self._rail_loads(r)

    def _probe(self, l: dict, label: str, supply: PinRef, ground: Optional[PinRef]):
        """A probe on an IC load's supply and ground pins, where the file gives the load
        its AC values (spec revision 9). Its target is set once the power tree is in
        (:meth:`_probe_targets`)."""
        if l.get("role") != "ic" or not isinstance(l.get("ac"), dict):
            return
        if any(p.name == label for p in self.setup.probes):
            return
        if ground is None:
            self.probes_without_ground.append(label)
            return
        probe = Probe(label, supply, ground)
        self.setup.probes.append(probe)
        self.new_probes.append((probe, l))

    def _probe_targets(self):
        """Each new probe's target: the maker's own where the file gives one, else
        Z = V_ac / I_step up to f = 1 / (pi t_rise), with V_ac the load's tolerance (its
        tighter side) less the DC accuracy of the regulator driving its rail. The DC drop
        is not taken off: it needs a DC solve (the target calculator does it)."""
        from kipi.ac.target import _hz as _hz_text
        from kipi.ac.target import probe_loads, target_from_transient, vrm_accuracies
        kept = {p.name for p in self.setup.probes}
        for probe, l in self.new_probes:
            if probe.name not in kept:
                continue
            ohm, hz, said = maker_target(l)
            if ohm and hz:
                probe.target_ohm, probe.target_hz, probe.target_from = ohm, hz, said
                self.targets_set.append(f"{probe.name} {ohm * 1e3:.4g} mOhm to "
                                        f"{_hz_text(hz)} ({said})")
                continue
            pins = [f"{probe.rail.ref}_{p}" for p in probe.rail.pins or []]
            step, step_basis = load_step(l, pins)
            if not step:
                ac = l.get("ac") or {}
                if ac.get("i_step_because") == "module":
                    probe.target_from = MODULE_NO_STEP
                    self.targets_not_needed.append(probe.name)
                    continue
                why = STEP_BECAUSE.get(ac.get("i_step_because"),
                                       ac.get("i_step_because") or "the file gives none")
                self.targets_missing.setdefault(f"no current step: {why}", []).append(
                    probe.name)
                continue
            rise, rise_basis = load_rise(l, step)
            if not rise:
                self.targets_missing.setdefault("no rise time, slew or fastest clock",
                                                []).append(probe.name)
                continue
            loads = [x for x in probe_loads(self.setup, probe.rail, probe.ground)
                     if x.tolerance]
            if not loads:
                self.targets_missing.setdefault("no supply tolerance", []).append(probe.name)
                continue
            accuracy = max((a.v for a in vrm_accuracies(self.setup, probe.rail)), default=0.0)
            v_ac = loads[0].tolerance.v_ac_v - accuracy
            t = target_from_transient(v_ac, step, step / rise) if v_ac > 0 else None
            if t is None:
                self.targets_missing.setdefault(
                    "the regulator's DC accuracy takes the whole tolerance", []).append(
                    probe.name)
                continue
            probe.target_ohm, probe.target_hz = t.z_ohm, t.f_hz
            probe.target_from = (f"step {step * 1e3:.4g} mA ({step_basis}), "
                                 f"{rise_basis}, ±{v_ac * 1e3:.4g} mV allowed")
            self.targets_set.append(f"{probe.name} {t.z_ohm * 1e3:.4g} mOhm to "
                                    f"{_hz_text(t.f_hz)} ({probe.target_from})")

    # ------------------------------------------------------------ capacitors (AC)
    def _file_caps(self) -> List[dict]:
        """Every capacitor the file lists: a rail's decoupling, a load's local and
        input-node capacitors (revision 11), and the capacitance to ground of each
        feedthrough capacitor in a series chain (``shunt_cap``, revision 11)."""
        def shunts(chain):
            return [e["shunt_cap"] for e in chain or [] if e.get("shunt_cap")]

        out = []
        for r in self.rails.values():
            out += r.get("decoupling") or []
            out += shunts(r.get("series_from_parent"))
            for f in r.get("other_feeds") or []:
                out += shunts(f.get("series"))
            for l in r.get("loads", []):
                out += l.get("local_caps") or []
                out += l.get("input_caps") or []
                out += shunts(l.get("input_filter"))
                for f in l.get("other_feeds") or []:
                    out += shunts(f.get("series"))
        return out

    def _caps(self):
        """One capacitor model per part number, from the capacitors' values and ESL. A
        model of that name already in the setup keeps what it holds and only gets what it
        lacks; a capacitor already in another model stays there. A capacitor with no part
        number that carries AC values (spec revision 9; a schematic that gives values
        only) is grouped by its value and ESL instead."""
        by_pn: Dict[str, List[dict]] = {}
        seen = set()
        for c in self._file_caps():
            ref = c.get("refdes")
            if not ref or ref in seen or ref not in self.idx.by_part:
                continue
            key = _cap_model_name(c)
            if key:
                seen.add(ref)
                by_pn.setdefault(key, []).append(c)
        if not by_pn:
            return
        models = {m.name: m for m in self.setup.caps}
        assigned = {ref for m in self.setup.caps for ref in m.refs}
        added = filled = esr_from_file = esr_estimates = 0
        substituted: List[str] = []
        for pn, caps in sorted(by_pn.items()):
            farads = next((f for f in (parse_capacitance(str(c.get("value", "")).split("/")[0])
                                       for c in caps) if f), 0.0)
            esl, esl_basis = next(((h, c["esl"].get("basis", "")) for c in caps
                                   if (h := _henries(c.get("esl")))), (0.0, ""))
            m = models.get(pn)
            if m is None:
                m = models[pn] = CapModel(pn, farads, esl, 0.0)
                self.setup.caps.append(m)
                added += 1
                m.values_from["C"] = "schematic"
                if esl:
                    m.values_from["ESL"] = esl_basis
            else:
                before = (m.capacitance_f, m.esl_h)
                m.capacitance_f, m.esl_h = m.capacitance_f or farads, m.esl_h or esl
                filled += before != (m.capacitance_f, m.esl_h)
                if not before[1] and m.esl_h:
                    m.values_from["ESL"] = esl_basis
            # An ESL its maker does not publish, substituted from another source the file
            # names: said on the model and in the notes, so it is never taken as the part's.
            sub = next((c["esl"] for c in caps if isinstance(c.get("esl"), dict)
                        and c["esl"].get("because") and _henries(c["esl"]) == m.esl_h), None)
            if sub:
                m.defaults_because = m.defaults_because or str(sub["because"])
                refs = ", ".join(c["refdes"] for c in caps)
                substituted.append(f"{pn} ({refs}) {m.esl_h * 1e9:g} nH, {sub.get('from', '')}")
            esr_filled, esr_estimated = fill_cap_model(m, caps, esl)
            esr_from_file += esr_filled
            esr_estimates += esr_estimated
            m.refs += [c["refdes"] for c in caps if c["refdes"] not in assigned]
            assigned.update(m.refs)
        no_esr = sum(1 for pn in by_pn if not models[pn].esr_ohm)
        esr = (f"ESR from the file for {esr_from_file} ({esr_estimates} of them estimates); "
               if esr_from_file else "")
        self.notes.append(f"Capacitor models (AC): {added} added and {filled} filled in by "
                          f"part number (by value and ESL where none is given), with their "
                          f"ESL; {esr}{no_esr} have no ESR in the file, to be entered before "
                          "an AC sweep")
        if substituted:
            self.notes.append("Capacitor ESL (AC): not published by the maker, a substitute "
                              "is used for " + "; ".join(substituted))

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
        # The file describes each regulator and load, so each regulator holds its output
        # above its own ground pins and each load's pads are one node, as its package joins
        # them. Without the file KiPI keeps its defaults.
        self.setup.settings.regulate_above_own_ground = True
        self.setup.settings.tie_load_pads = True
        self.notes.append("Each regulator without ground sense regulates above its own "
                          "ground pins, and each load's pads are one node (KiPI's defaults "
                          "without the file: above 0 V, current shared equally)")
        self._learn_nets()
        self._board_sources()
        self._regulated_rails()
        self._series_parts()
        self._ground_links()
        self._loads()
        self._switched_paths()
        self._drop_unpowered()
        self._unreferenced_grounds()
        self._caps()
        self._probe_targets()
        converter_input_probes(self.setup, self.vrm_regs, self.rails, self.targets_set,
                               self.targets_missing, self.probes_without_ground)
        self._ac_notes()
        self._range_notes()
        self.setup.import_notes = list(self.notes)
        self.setup.import_source = source_text(self.pdn)
        return self.setup, self.notes

    def _ac_notes(self):
        self.notes.extend(defaults_notes(self.pdn))
        self.notes.extend(doubt_notes(self.setup.caps))
        if self.estimated_bandwidths:
            self.notes.append("VRM bandwidth (AC): estimated for "
                              f"{', '.join(self.estimated_bandwidths)}")
        if self.targets_set:
            self.notes.append(f"Probe targets (AC): set for {len(self.targets_set)} from the "
                              "file (the DC drop is not taken off; the target calculator "
                              "does that after a DC solve):\n" + "\n".join(self.targets_set))
        if self.targets_not_needed:
            self.notes.append(f"Probe targets (AC): not needed for "
                              f"{', '.join(self.targets_not_needed)} - {MODULE_NO_STEP}")
        for why, names in self.targets_missing.items():
            self.notes.append(f"Probe targets (AC): none for {len(names)} probe"
                              f"{'' if len(names) == 1 else 's'} - {why}:\n"
                              + ", ".join(names))
        if self.probes_without_ground:
            self.notes.append("Probes (AC): no ground pins found, so no probe, for "
                              f"{', '.join(self.probes_without_ground)}")

    def _switched_paths(self):
        """Power the board carries between its terminals and runs nothing from (a relay
        switching a bench supply through a shunt; spec revision 7): an ideal source at its
        IN terminals at the designer's voltage, its series parts as DC connections, and a
        load at its OUT terminals drawing the designer's load current, both returning on
        the path's return terminals (the first for the source, the last for the load: the
        file does not say which is which), else through an ideal ground."""
        for sp in self.pdn.get("switched_paths") or []:
            label = sp.get("net") or "?"
            what = f"Switched path {label}" + (f" ({sp['switched_by']})"
                                               if sp.get("switched_by") else "")
            inp, out = sp.get("in"), sp.get("out")
            if sp.get("contact_only") or not isinstance(inp, dict) or not isinstance(out, dict):
                self.notes.append(f"{what}: contacts only, no source or load side on the "
                                  "board; not imported")
                continue
            volts = sp.get("voltage") if isinstance(sp.get("voltage"), dict) else {}
            v = number(volts.get("nominal"))
            amps_max = amps(sp.get("load_current_max"))
            if v is None or amps_max is None:
                self.notes.append(f"{what}: its {'voltage' if v is None else 'load current'} "
                                  "is not stated; not imported")
                continue
            src = self._terminal_pins(inp, f"{what} input")
            dst = self._terminal_pins(out, f"{what} output")
            if src is None or dst is None:
                continue
            ret = sp.get("return") if isinstance(sp.get("return"), dict) else {}
            rets = [self._terminal_ground(t, ret.get("net")) for t in ret.get("terminals") or []]
            rets = [g for g in rets if g is not None]
            self.setup.vrms.append(Vrm(self._unique(f"{label} (switched path)"), src, v,
                                       "source", ground=rets[0] if rets else None))
            for e in sp.get("series") or []:
                self._link(e)
            name = self._unique(f"{out.get('net') or label} load (switched path)")
            self.setup.loads.append(Load(name, dst, amps_max, current_max=amps_max,
                                         ground=rets[-1] if rets else None))
            self.notes.append(f"{what}: {inp.get('net')} -> {out.get('net')} at {v:g} V, "
                              f"{amps_max:g} A load (the designer's figures)")

    def _terminal_ground(self, ref: str, net: Optional[str]) -> Optional[PinRef]:
        """A return terminal's pins on the return net."""
        if not net:
            return None
        return self.idx.part_on_net(ref, self.idx.board_net(net) or net)

    def _terminal_pins(self, end: dict, what: str) -> Optional[PinRef]:
        """The pins of a switched path's terminals on its net (the first terminal found)."""
        net = self.idx.board_net(end.get("net", "")) or end.get("net", "")
        for ref in end.get("terminals") or []:
            pins = self.idx.part_on_net(ref, net)
            if pins is not None:
                return pins
        self.notes.append(f"{what}: terminals {', '.join(end.get('terminals') or [])} not "
                          f"found on the board on {end.get('net')}")
        return None

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

    def _range_notes(self):
        """What a single value per item left out, one note each, for the loads kept."""
        kept = {x.name for x in self.setup.loads}
        if self.not_supplied:
            self.notes.append("On a rail by a detect, enable or reference pin, not a supply "
                              "pin, so not a load: " + ", ".join(self.not_supplied))
        static = [f"{n} ({_amps(a)})" for n, a in self.static_current.items() if n in kept]
        if static:
            self.notes.append("Draw what they drive: their own (static) current from the "
                              "datasheet is solved, what they drive is on top and not known, "
                              "the Headroom results say how much that may add: " + ", ".join(static))
        no_range = [n for n in self.no_supply_range if n in kept]
        if no_range:
            self.notes.append("No supply voltage range is stated for these ICs, so they "
                              "are not judged PASS / FAIL; enter it on the Loads tab when "
                              "known: " + ", ".join(no_range))
        unknown = [n for n in self.unknown_current if n in kept]
        if unknown:
            self.notes.append("Current not known, imported as 0 A and marked not known (the "
                              "Headroom results say how much they may add together); enter "
                              "it on the Loads tab when known: " + ", ".join(unknown))
        unused = [f"{n} ({r})" for n, r in self.unused_current if n in kept]
        if unused:
            self.notes.append("The typical current is solved; these minimum currents are "
                              "not used: " + "; ".join(unused))
        if self.accuracies:
            self.notes.append("Output accuracy from the datasheets (a worst-case solve runs "
                              "each VRM at its low end): " + "; ".join(self.accuracies))
        out = [f"{n} ({why})" for n, why in self.out_before_copper if n in kept]
        if out:
            self.notes.append("Out of their supply range before any copper (the regulator's "
                              "output band against the part's datasheet): " + "; ".join(out))
        for kind, names in self.maker_z.items():
            self.notes.append(f"Series impedance (AC) from the {kind}: {', '.join(names)}")
        maxed = [f"{n} ({_amps(a)})" for n, a in self.max_current.items() if n in kept]
        if maxed or self.max_resistance:
            self.notes.append("Maximum currents and resistances are kept for a worst-case "
                              f"solve (beside Solve): {len(maxed)} loads, "
                              f"{len(self.max_resistance)} DC connections")


def _positive_number(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _through_ohms(e: dict) -> Optional[float]:
    shunt = e.get("shunt_cap") if isinstance(e.get("shunt_cap"), dict) else {}
    ac = shunt.get("ac") if isinstance(shunt.get("ac"), dict) else {}
    maker = ac.get("maker_model") if isinstance(ac.get("maker_model"), dict) else {}
    return _positive_number(maker.get("through_resistance_ohm"))


def _amps(a: float) -> str:
    if abs(a) < 1e-3:
        return f"{a * 1e6:.4g} µA"
    return f"{a * 1e3:.4g} mA" if abs(a) < 1 else f"{a:.4g} A"


def _volts_range(lo: Optional[float], hi: Optional[float]) -> str:
    return " to ".join("?" if x is None else f"{x:.4g}" for x in (lo, hi)) + " V"


def _cap_model_name(c: dict) -> Optional[str]:
    """The capacitor model a capacitor belongs to: its part number, else (with AC
    values) its value and ESL, named so; None for neither."""
    if c.get("part_number"):
        return str(c["part_number"])
    if not isinstance(c.get("ac"), dict) or not c.get("value"):
        return None
    esl = _henries(c.get("esl"))
    return f"{c['value']}{f', {esl * 1e9:g} nH' if esl else ''} (no part number)"


def _henries(esl) -> Optional[float]:
    """An ``esl`` field's henries; None for "unknown" or none."""
    return number(esl.get("h")) if isinstance(esl, dict) else None


def _ohms(r: float) -> str:
    return f"{r * 1e3:.4g} mΩ" if abs(r) < 1 else f"{r:.4g} Ω"


def source_text(pdn: dict, path=None) -> str:
    """Which power-tree file a setup came from, for the reports: its name and a short
    hash (with ``path``), the tool and version that wrote it, its date and the format's
    revision."""
    parts = []
    if path is not None:
        path = Path(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        parts.append(f"{path.name} (sha256 {digest})")
    if pdn.get("generated_by"):
        parts.append(str(pdn["generated_by"]))
    if pdn.get("date"):
        parts.append(str(pdn["date"]))
    if pdn.get("spec_revision") is not None:
        parts.append(f"format revision {pdn['spec_revision']}")
    return ", ".join(parts)


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
        setup, notes = TomachiePdnImporter(pdn, snap, base).run()
        setup.import_source = source_text(pdn, path)
        return setup, notes
    except (AttributeError, TypeError, KeyError) as e:
        # A field of the wrong kind (a list where an object belongs, say): the file is
        # not one Tomachie wrote, or is damaged.
        raise PdnImportError(f"{path.name} is not a valid {SCHEMA} file "
                             f"({type(e).__name__}: {e})") from e
