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

Only these are read:

* ``SUPPLIES`` -> the power nets, ticked in the net list;
* ``DC_CONN`` -> series links, from the part's two pins with its DC resistance (both
  nets are ticked too, as a series link needs);
* ``DCCGROUPS`` and ``DCCAPACITORS`` -> capacitor models, one per group, holding the
  group's parts. Where every part in a group has the same value, its capacitance (and
  voltage rating and dielectric) are taken from it; ESL and ESR are left to fill in.

The rest of the file (stackup, signal-integrity settings, ...) is not KiPI's business.
HyperLynx keeps the file's format private, so a section that is not one of these, but
looks like a VRM or a DC load, is named in the notes rather than guessed at.

The import merges into the setup it is given: a series link on the same part is
replaced, a capacitor model of the same name gets the group's parts (a part moves out of
any other model), and VRMs, loads and every setting stay as they were.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kipi.ac.model import AGING_PCT_PER_DECADE, CapModel
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.parts import (
    is_class2, parse_capacitance, parse_dielectric, parse_voltage_rating,
)
from kipi.dc.pi.importers.pad_index import PadIndex
from kipi.dc.pi.model import DcLink, PinRef, PISetup

SUFFIX = ".bud"
MAX_FILE_BYTES = 256 << 20      # a session file is megabytes at most
# Sections that may hold VRMs or DC loads, whose records KiPI has never seen.
_UNREAD = re.compile(r"VRM|SINK|LOAD|DC_?CURR", re.IGNORECASE)
_FIELD = re.compile(r'([^\s=()]+)\s*=\s*("[^"]*"|[^\s()]+)')


class BudImportError(ValueError):
    pass


def is_bud(path) -> bool:
    return Path(path).suffix.lower() == SUFFIX


def sections(text: str) -> Dict[str, List[Dict[str, str]]]:
    """{section name: [record fields]}; a one-line ``{NAME=value}`` section has none."""
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
            current.append({k.upper(): v.strip('"') for k, v in _FIELD.findall(line)})
    return out


def _ohms(text: str) -> Optional[float]:
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
            r_dc = _ohms(r.get("RDC"))
            if r_dc is None:
                self.notes.append(f"{ref}: no DC resistance given; imported as a short")
            r_dc = max(r_dc or 0.0, 0.0)
            self.setup.links = [k for k in self.setup.links
                                if k.name != ref and {s.ref for s in k.sides} != {ref}]
            self.setup.links.append(DcLink(ref, a, b, r_dc))
            self._tick(a.net)
            self._tick(b.net)

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
            if records and _UNREAD.search(name):
                self.notes.append(f"Section {name} ({len(records)} records) not read: KiPI "
                                  "does not know its format yet. Set these up by hand, or "
                                  "send a sample so it can be read")

    def run(self) -> Tuple[PISetup, List[str], List[str]]:
        self._supplies()
        self._links()
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
