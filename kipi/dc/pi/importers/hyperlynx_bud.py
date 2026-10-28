"""Read a HyperLynx BoardSim session file (``.bud``, #132) into a setup.

A session file holds what was set up in HyperLynx for one board, in sections of records:

.. code-block:: text

    {SUPPLIES
    (S NAME=+3.3V VAL=3.3 V?=NO C?=NO)
    }
    {DC_CONN
    (REF_DES=FB701 PIN1=1 PIN2=2 RDC=0.01)
    }
    {DCCGROUPS
    (G NAME=Group1)
    }
    {DCCAPACITORS
    (C REFDES=C702 DCCGROUP=Group1)
    }
    {PI_VRM
    (VRM ID=0 PHASES=1 NAME="POS3P3" TYPE=SIMPLE V=3.3 SL_PWR_PIN=C1001|2 SL_REF_PIN=C1102|1)
    (PHASE VRM=0 PERCENT=100 R=0 L=2e-09 PWR_NET=+3.3V REF_NET=GND PWR_PIN1=U402|2 REF_PIN1=U402|6)
    }
    {PI_SIGNAL_SOURCES
    (DC REF_PIN=U701|15 I=0.5 R=1e+06)
    (REF_DATA PIN=U402|2 REF_NET=GND)
    }

Only these are read:

* ``SUPPLIES`` -> the power nets, ticked in the net list;
* ``DC_CONN`` -> series links, from the part's two pins with its DC resistance (both
  nets are ticked too, as a series link needs);
* ``DCCGROUPS`` and ``DCCAPACITORS`` -> capacitor models, one per group, holding the
  group's parts. Where every part in a group has the same value, its capacitance (and
  voltage rating and dielectric) are taken from it; ESL and ESR are left to fill in;
* ``PI_VRM`` -> VRMs. A HyperLynx VRM says nothing of what it draws from its input, so
  each is an ideal source (``kind="source"``) at ``V``: a ``PHASE`` record's
  ``PWR_PIN<n>`` are its output pins on ``PWR_NET``, its ``REF_PIN<n>`` its ground pins
  on ``REF_NET``, and the VRM's ``SL_PWR_PIN`` / ``SL_REF_PIN`` its remote sense and
  ground sense. The phases' ``R`` in parallel is its load line; ``L`` is AC impedance,
  which KiPI's VRMs do not have, and ``PERCENT`` is not read, as KiPI's phases share the
  current equally;
* ``PI_SIGNAL_SOURCES`` -> loads. Its ``DC`` records are DC sinks, pin by pin
  (``REF_PIN=U701|15 I=0.5``): one load per part and net, drawing the pins' currents, in
  pin groups where they differ. ``R`` (HyperLynx's sink resistance) is not modelled, as
  KiPI's loads draw a fixed current. A load's ground is the part's pins on a
  ``REF_DATA`` or VRM ``REF_NET`` net if it has any, else its ground net as KiPI guesses
  it.

The rest of the file (stackup, signal-integrity settings, ...) is not KiPI's business.
HyperLynx keeps the file's format private, so another section that looks like a VRM or
a DC load is named in the notes rather than guessed at.

The import merges into the setup it is given: a series link on the same part is
replaced, as is a VRM of the same name; a load on the same part and net gets the file's
current (keeping its tolerances and ground); a capacitor model of the same name gets the
group's parts (a part moves out of any other model). Every setting stays as it was.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kipi.ac.model import AGING_PCT_PER_DECADE, CapModel
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.parts import (
    is_class2, parse_capacitance, parse_dielectric, parse_voltage_rating,
)
from kipi.dc.pi.autofill import guess_ground_pins
from kipi.dc.pi.importers.pad_index import PadIndex
from kipi.dc.pi.load_model import groups_of
from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm, VrmPhase
from kipi.dc.pi.split import existing_load

SUFFIX = ".bud"
MAX_FILE_BYTES = 256 << 20      # a session file is megabytes at most
OPEN_OHMS = 1e6                 # HyperLynx's own "no resistance" for a DC sink
KIND = "_KIND"                  # a record's leading word: "(PHASE VRM=0 ...)" -> "PHASE"
_READ = {"PI_VRM", "PI_SIGNAL_SOURCES"}
# Other sections that may hold VRMs or DC loads, whose records KiPI has never seen.
_UNREAD = re.compile(r"VRM|SINK|LOAD|DC_?CURR", re.IGNORECASE)
_PIN_FIELD = re.compile(r"(PWR|REF)_PIN(\d+)$")
_LEADING = re.compile(r"\(\s*([A-Za-z_]\w*)(?=\s|\)|$)")
_FIELD = re.compile(r'([^\s=()]+)\s*=\s*("[^"]*"|[^\s()]+)')


class BudImportError(ValueError):
    pass


def is_bud(path) -> bool:
    return Path(path).suffix.lower() == SUFFIX


def sections(text: str) -> Dict[str, List[Dict[str, str]]]:
    """{section name: [record fields]}; a one-line ``{NAME=value}`` section has none. A
    record's leading word, if it is not a field, is under ``KIND``."""
    out: Dict[str, List[Dict[str, str]]] = {}
    current: Optional[List[Dict[str, str]]] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("{"):
            name = re.match(r"\{\s*([\w?]*)", line).group(1).upper()
            current = out.setdefault(name, [])
            if "}" in line:                 # all on one line: "{BUD} ...", "{VERSION=2.2}"
                current = None
        elif line.startswith("}"):
            current = None
        elif line.startswith("(") and current is not None:
            fields = {k.upper(): v.strip('"') for k, v in _FIELD.findall(line)}
            lead = _LEADING.match(line)
            if lead:
                fields[KIND] = lead.group(1).upper()
            current.append(fields)
    return out


def _number(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class BudImporter:
    def __init__(self, text: str, snap: BoardSnapshot, base: Optional[PISetup] = None):
        self.sections = sections(text)
        if "BUD" not in self.sections:
            raise BudImportError("not a HyperLynx session file (no {BUD} header)")
        self.snap = snap
        self.idx = PadIndex(snap)
        self.setup = copy.deepcopy(base) if base is not None else PISetup()
        self.notes: List[str] = []
        self.power_nets: List[str] = []
        self.ref_nets: Set[str] = set()     # VRM and DC sink reference (ground) nets

    def _net(self, name: str) -> Optional[str]:
        return self.idx.board_net(name)

    def _tick(self, net: str):
        if net not in self.power_nets:
            self.power_nets.append(net)

    # ------------------------------------------------------------ power nets
    def _supplies(self):
        missing = []
        for r in self.sections.get("SUPPLIES", []):
            name = r.get("NAME", "")
            net = self._net(name) if name else None
            if net is None:
                missing.append(name or "?")
            else:
                self._tick(net)
        if missing:
            self.notes.append("Power nets not on the board: " + ", ".join(missing))

    # ------------------------------------------------------------ series links
    def _pin(self, ref: str, pin: str) -> Optional[PinRef]:
        nets = {p.net for p in self.idx.by_pin.get((ref, pin), []) if p.net}
        return PinRef(ref, nets.pop(), [pin]) if len(nets) == 1 else None

    def _links(self):
        for r in self.sections.get("DC_CONN", []):
            ref, p1, p2 = r.get("REF_DES", ""), r.get("PIN1", ""), r.get("PIN2", "")
            a, b = self._pin(ref, p1), self._pin(ref, p2)
            if a is None or b is None:
                self.notes.append(f"{ref or '?'}: series link pins {p1} and {p2} not both "
                                  "on the board on a net; left out")
                continue
            if a.net == b.net:
                self.notes.append(f"{ref}: both pins on {a.net}, so it joins nothing; "
                                  "left out")
                continue
            r_dc = _number(r.get("RDC"))
            if r_dc is None:
                self.notes.append(f"{ref}: no DC resistance given; imported as a short")
            r_dc = max(r_dc or 0.0, 0.0)
            self.setup.links = [k for k in self.setup.links
                                if k.name != ref and {s.ref for s in k.sides} != {ref}]
            self.setup.links.append(DcLink(ref, a, b, r_dc))
            self._tick(a.net)
            self._tick(b.net)

    # ------------------------------------------------------------ VRMs
    def _pins(self, pins: List[str], net_name: str, what: str) -> Optional[PinRef]:
        """One part's pins (``"U402|6"``) on ``net_name`` (on any net, if not given). Pins
        the board does not have there are noted; pins on several parts give the part with
        the most of them, and the others are noted."""
        want = self._net(net_name) if net_name else None
        by: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        missing = []
        for pin in pins:
            ref, _, number = pin.partition("|")
            pads = [p for p in self.idx.by_pin.get((ref, number), [])
                    if p.net and (want is None or p.net == want)]
            if pads:
                by[(ref, pads[0].net)].append(number)
            else:
                missing.append(pin)
        if missing:
            self.notes.append(f"{what}: {', '.join(missing)} not on the board"
                              + (f" on {net_name}" if net_name else ""))
        if not by:
            return None
        (ref, net), numbers = max(by.items(), key=lambda kv: len(kv[1]))
        if len(by) > 1:
            others = ", ".join(f"{r}|{n}" for (r, nt), ns in by.items()
                               if (r, nt) != (ref, net) for n in ns)
            self.notes.append(f"{what}: pins on more than one part; using {ref}, leaving "
                              f"out {others}")
        return PinRef(ref, net, sorted(set(numbers), key=lambda n: (len(n), n)))

    @staticmethod
    def _numbered(r: Dict[str, str], side: str) -> List[str]:
        """A PHASE record's ``PWR_PIN1``, ``PWR_PIN2``, ... (``side`` "PWR" or "REF")."""
        found = []
        for key, value in r.items():
            m = _PIN_FIELD.match(key)
            if m and m.group(1) == side and value:
                found.append((int(m.group(2)), value))
        return [v for _, v in sorted(found)]

    def _vrms(self):
        records = self.sections.get("PI_VRM", [])
        phases: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for r in records:
            if r.get(KIND) == "PHASE":
                phases[r.get("VRM", "")].append(r)
        for r in records:
            if r.get(KIND) != "VRM":
                continue
            vid = r.get("ID", "")
            name = r.get("NAME") or f"VRM {vid}"
            volts = _number(r.get("V"))
            if volts is None:
                self.notes.append(f"{name}: no voltage given; not imported")
                continue
            if r.get("TYPE", "SIMPLE").upper() != "SIMPLE":
                self.notes.append(f"{name}: a {r['TYPE']} VRM, imported as an ideal source")
            mine = phases.get(vid, [])
            sides = []
            for k, ph in enumerate(mine, 1):
                what = f"{name} phase {k}" if len(mine) > 1 else name
                out = self._pins(self._numbered(ph, "PWR"), ph.get("PWR_NET", ""),
                                 f"{what} output")
                if out is None:
                    self.notes.append(f"{what}: no output pins on the board; phase left out")
                    continue
                gnd = self._pins(self._numbered(ph, "REF"), ph.get("REF_NET", ""),
                                 f"{what} ground")
                if gnd is not None:
                    self.ref_nets.add(gnd.net)
                sides.append((out, gnd, ph))
            if not sides:
                self.notes.append(f"{name}: no phase with output pins on the board; "
                                  "not imported")
                continue
            sense = (self._pins([r["SL_PWR_PIN"]], "", f"{name} sense")
                     if r.get("SL_PWR_PIN") else None)
            gsense = (self._pins([r["SL_REF_PIN"]], "", f"{name} ground sense")
                      if r.get("SL_REF_PIN") else None)
            (out, gnd, _), *rest = sides
            vrm = Vrm(name, out, volts, kind="source", ground=gnd, sense=sense,
                      ground_sense=gsense,
                      phases=[VrmPhase(o, None, g) for o, g, _ in rest])
            ohms = [_number(ph.get("R")) or 0.0 for _, _, ph in sides]
            if all(x > 0 for x in ohms):
                vrm.load_line_mohm = 1e3 / sum(1 / x for x in ohms)
                self.notes.append(f"{name}: output resistance R imported as a "
                                  f"{vrm.load_line_mohm:g} mΩ load line")
            if len({_number(ph.get("PERCENT")) for _, _, ph in sides}) > 1:
                self.notes.append(f"{name}: phases share the current equally in KiPI, not "
                                  "by their PERCENT")
            self.setup.vrms = [v for v in self.setup.vrms if v.name != name]
            self.setup.vrms.append(vrm)
            for ref in (out, gnd):
                if ref is not None:
                    self._tick(ref.net)
        if any(_number(ph.get("L")) for rs in phases.values() for ph in rs):
            self.notes.append("VRM inductance L not imported: KiPI's VRMs have no AC "
                              "impedance; their bandwidth only sets where targets apply")

    # ------------------------------------------------------------ DC loads
    def _loads(self):
        records = self.sections.get("PI_SIGNAL_SOURCES", [])
        for r in records:
            net = self._net(r.get("REF_NET", "")) if r.get(KIND) == "REF_DATA" else None
            if net:
                self.ref_nets.add(net)
        sinks: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(dict)  # (part, net)
        missing, no_current, resistive = [], [], []
        for r in records:
            if r.get(KIND) != "DC":
                continue
            pin = r.get("REF_PIN", "")
            ref, _, number = pin.partition("|")
            nets = {p.net for p in self.idx.by_pin.get((ref, number), []) if p.net}
            if len(nets) != 1:
                missing.append(pin or "?")
                continue
            amps = _number(r.get("I"))
            if amps is None:
                no_current.append(pin)
            ohms = _number(r.get("R"))
            if ohms is not None and 0 < ohms < OPEN_OHMS:
                resistive.append(pin)
            sinks[(ref, nets.pop())][number] = amps or 0.0
        if missing:
            self.notes.append("DC sink pins not on the board on a net: " + ", ".join(missing))
        if no_current:
            self.notes.append("DC sinks with no current given, imported as 0 A: "
                              + ", ".join(no_current))
        if resistive:
            self.notes.append("DC sink resistance R not modelled (KiPI's loads draw a fixed "
                              "current): " + ", ".join(resistive))
        prefer = sorted(self.ref_nets)
        for (ref, net), amps in sinks.items():
            groups = groups_of(amps)
            load = existing_load(self.setup, ref, net)
            if load is None:
                supply = PinRef(ref, net)
                load = Load(self._unique(f"{ref} {net}"), supply, 0.0,
                            ground=guess_ground_pins(self.snap, self.setup, None, supply,
                                                     prefer))
                self.setup.loads.append(load)
            load.current = sum(amps.values())
            load.groups = groups if len(groups) > 1 else []
            self._tick(net)
            if load.ground is not None:
                self._tick(load.ground.net)

    def _unique(self, name: str) -> str:
        taken = {x.name for x in [*self.setup.vrms, *self.setup.loads, *self.setup.links]}
        out, i = name, 2
        while out in taken:
            out, i = f"{name} ({i})", i + 1
        return out

    # ------------------------------------------------------------ capacitor groups
    def _caps(self):
        groups: Dict[str, List[str]] = {g.get("NAME", ""): []
                                        for g in self.sections.get("DCCGROUPS", [])
                                        if g.get("NAME")}
        on_board = {f.reference for f in self.snap.footprints} | set(self.idx.by_part)
        missing = []
        for c in self.sections.get("DCCAPACITORS", []):
            ref, group = c.get("REFDES", ""), c.get("DCCGROUP", "")
            if not ref or not group:
                continue
            if ref not in on_board:
                missing.append(ref)
                continue
            groups.setdefault(group, []).append(ref)
        if missing:
            self.notes.append("Capacitors not on the board: " + ", ".join(missing))
        values = {f.reference: f.value for f in self.snap.footprints}
        no_value: List[str] = []
        moved: Set[str] = set()
        for name, refs in groups.items():
            if not refs:
                self.notes.append(f"Capacitor group {name}: no capacitors on the board; "
                                  "not imported")
                continue
            mine = set(refs)
            for m in self.setup.caps:
                if m.name != name and mine & set(m.refs):
                    moved |= mine & set(m.refs)
                    m.refs = [x for x in m.refs if x not in mine]
            model = next((m for m in self.setup.caps if m.name == name), None)
            if model is None:
                model = CapModel(name)
                self.setup.caps.append(model)
            model.refs = sorted(mine, key=_natural)
            if not model.capacitance_f and not _from_value(model, {values.get(x, "")
                                                                   for x in refs}):
                no_value.append(name)
        if moved:
            self.notes.append("Moved into their HyperLynx group from another capacitor "
                              "model: " + ", ".join(sorted(moved, key=_natural)))
        if no_value:
            self.notes.append("Capacitor groups whose parts do not share one value, so their "
                              "capacitance is left to fill in: " + ", ".join(no_value))
        if any(groups.values()):
            self.notes.append("Capacitor groups: ESL and ESR are not in the session file; "
                              "fill them in on the AC tab (or import the parts' SPICE models)")

    # ------------------------------------------------------------ the rest
    def _unread(self):
        for name, records in self.sections.items():
            if records and name not in _READ and _UNREAD.search(name):
                self.notes.append(f"Section {name} ({len(records)} records) not read: KiPI "
                                  "does not know its format yet. Set these up by hand, or "
                                  "send a sample so it can be read")

    def run(self) -> Tuple[PISetup, List[str], List[str]]:
        self._supplies()
        self._links()
        self._vrms()
        self._loads()
        self._caps()
        self._unread()
        return self.setup, self.notes, self.power_nets


def _natural(ref: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", ref)]


def _from_value(m: CapModel, values: Set[str]) -> bool:
    """Fill a model's capacitance, rating and dielectric from its parts' one value; False
    if they have several values or none KiPI can read."""
    if len(values) != 1:
        return False
    value = values.pop()
    farads = parse_capacitance(value)
    if not farads:
        return False
    m.capacitance_f = farads
    rated = parse_voltage_rating(value)
    if rated is not None and is_class2(value):   # DC bias only for class II ceramics
        m.rated_v = rated
    diel = parse_dielectric(value)
    if diel:
        m.dielectric = diel
        m.aging_pct_per_decade = AGING_PCT_PER_DECADE.get(diel, 0.0)
    return True


def import_bud(path, snap: BoardSnapshot,
               base: Optional[PISetup] = None) -> Tuple[PISetup, List[str], List[str]]:
    """(the setup with the session file merged in, notes, the power nets to tick)."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise BudImportError(f"{path.name} is too large to be a HyperLynx session file")
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return BudImporter(text, snap, base).run()
    except BudImportError as e:
        raise BudImportError(f"{path.name}: {e}") from e
