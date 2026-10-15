"""The power tree as a block diagram (#58): VRMs, rails, series links, loads and
capacitors, laid out left to right in the direction power flows.

This module only works out the blocks, the arrows between them and which column and row
each block goes in; :mod:`kipi.gui.power_tree` draws it. Nothing here needs Qt.

* Every net the setup uses for power is a **rail** block (ground nets are left out:
  every block returns to ground, and drawing it would only add clutter).
* A **VRM** takes power from its input rail (LDO / switcher) and drives its output rail;
  a source has no input, so it starts a branch of the tree.
* A **series link** joins two rails; its arrow points away from the VRM that feeds it.
* **Loads** and **capacitors** hang off the rail they sit on. Capacitors are grouped by
  model and rail, and need the board to find which rail each part's pads are on.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from kipi.core.model.nets import is_ground_name
from kipi.dc.pi.model import PISetup, net_groups

VRM_KIND_LABELS = {"source": "Source", "ldo": "LDO", "smps": "Switcher"}


@dataclass
class Block:
    id: str
    kind: str                       # "vrm", "rail", "link", "load", "cap"
    title: str
    lines: List[str] = field(default_factory=list)
    vrm_kind: str = ""              # VRMs: "source", "ldo" or "smps"
    tip: str = ""
    column: int = 0
    row: int = 0                    # order within the column, top to bottom


@dataclass
class Arrow:
    src: str
    dst: str
    dashed: bool = False            # capacitors: decoupling, no power flows to them


@dataclass
class Diagram:
    blocks: Dict[str, Block] = field(default_factory=dict)
    arrows: List[Arrow] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def columns(self) -> List[List[Block]]:
        """Blocks per column, each column in row order."""
        n = 1 + max((b.column for b in self.blocks.values()), default=-1)
        cols: List[List[Block]] = [[] for _ in range(n)]
        for b in self.blocks.values():
            cols[b.column].append(b)
        return [sorted(c, key=lambda b: b.row) for c in cols]


def si(x: float, unit: str) -> str:
    """'10 mΩ', '4.7 µF', '3.3 V'."""
    if not x or not math.isfinite(x):
        return f"0 {unit}"
    e = max(-15, min(9, 3 * math.floor(math.log10(abs(x)) / 3)))
    prefix = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[e]
    return f"{x / 10 ** e:.3g} {prefix}{unit}"


def _ground_nets(setup: PISetup) -> set:
    grounds = set()
    for v in setup.vrms:
        for ph in v.all_phases():
            if ph.ground:
                grounds.add(ph.ground.net)
        if v.ground_sense:
            grounds.add(v.ground_sense.net)
    for l in setup.loads:
        if l.ground:
            grounds.add(l.ground.net)
    for p in setup.probes:
        grounds.add(p.ground.net)
    if setup.ac.ground:
        grounds.add(setup.ac.ground)
    return grounds


def _refs_text(refs: List[str], most: int = 6) -> str:
    refs = sorted(refs, key=_natural)
    return ", ".join(refs[:most]) + (f" +{len(refs) - most} more" if len(refs) > most else "")


def _natural(ref: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


def build_diagram(setup: PISetup, snapshot=None) -> Diagram:
    """The blocks and arrows of ``setup``'s power tree, laid out in columns. ``snapshot``
    (the board) places the capacitors on their rails; without it they are left out."""
    d = Diagram()
    grounds = _ground_nets(setup)
    groups = net_groups(setup)
    group_v = {groups[v.output.net]: v.voltage for v in setup.vrms if v.output.net in groups}

    def rail(net: str) -> str:
        bid = f"rail:{net}"
        if bid not in d.blocks:
            g = groups.get(net)
            v = group_v.get(g) if g is not None else None
            d.blocks[bid] = Block(bid, "rail", net, [si(v, "V")] if v else [],
                                  tip=f"Net {net}")
        return bid

    for v in setup.vrms:
        bid = f"vrm:{v.name}"
        kind = v.kind if v.kind in VRM_KIND_LABELS else "source"
        lines = [f"{si(v.voltage, 'V')} out"]
        if kind == "smps":
            lines.append(f"η {v.efficiency * 100:.3g} %")
        if v.phases:
            lines.append(f"{len(v.phases) + 1} phases")
        if v.current_limit_a:
            lines.append(f"limit {si(v.current_limit_a, 'A')}")
        tip = [f"{VRM_KIND_LABELS[kind]} {v.name}: output on {v.output.ref} ({v.output.net})"]
        if v.input:
            tip.append(f"input on {v.input.ref} ({v.input.net})")
        if v.sense:
            tip.append(f"remote sense at {v.sense.ref} ({v.sense.net})")
        d.blocks[bid] = Block(bid, "vrm", v.name, lines, vrm_kind=kind, tip="\n".join(tip))
        if v.input and v.input.net not in grounds:
            d.arrows.append(Arrow(rail(v.input.net), bid))
        d.arrows.append(Arrow(bid, rail(v.output.net)))

    links = []
    for k in setup.links:
        bid = f"link:{k.name}"
        lines = [si(k.resistance, "Ω") if k.resistance else "short"]
        if k.esl_h:
            lines.append(f"ESL {si(k.esl_h, 'H')}")
        d.blocks[bid] = Block(bid, "link", k.name, lines,
                              tip=f"Series link {k.name}: {k.side_a.net} ↔ {k.side_b.net}")
        links.append((bid, rail(k.side_a.net), rail(k.side_b.net)))

    for l in setup.loads:
        bid = f"load:{l.name}"
        lines = [si(l.current, "A")]
        if l.tolerance_pct:
            lines.append(f"± {l.tolerance_pct:g} %")
        if l.groups:
            lines.append(f"{len(l.groups)} pin groups")
        d.blocks[bid] = Block(bid, "load", l.name, lines,
                              tip=f"Load {l.name} on {l.supply.ref} ({l.supply.net})")
        d.arrows.append(Arrow(rail(l.supply.net), bid))

    _add_caps(d, setup, snapshot, grounds, rail)
    for bid, a, b in _orient_links(d, links):
        d.arrows += [Arrow(a, bid), Arrow(bid, b)]
    _layout(d)
    return d


def _add_caps(d: Diagram, setup: PISetup, snapshot, grounds: set, rail):
    if not setup.caps:
        return
    if snapshot is None:
        d.notes.append("Load the board to place the capacitors on their rails.")
        return
    nets_of: Dict[str, set] = {}
    for p in snapshot.pads:
        if p.footprint and p.net:
            nets_of.setdefault(p.footprint, set()).add(p.net)
    missing = []
    for m in setup.caps:
        by_net: Dict[str, List[str]] = {}
        for ref in m.refs:
            nets = {n for n in nets_of.get(ref, ()) if n not in grounds and not is_ground_name(n)}
            if not nets:
                missing.append(ref)
            for n in nets:
                by_net.setdefault(n, []).append(ref)
        for net, refs in sorted(by_net.items()):
            bid = f"cap:{m.name}:{net}"
            lines = [f"{len(refs)} × {si(m.capacitance_f, 'F')}" if m.capacitance_f
                     else f"{len(refs)} × (no value)", _refs_text(refs, 4)]
            tip = (f"Capacitor model {m.name} on {net}: {_refs_text(refs, 40)}\n"
                   f"C {si(m.capacitance_f, 'F')}, ESL {si(m.esl_h, 'H')}, "
                   f"ESR {si(m.esr_ohm, 'Ω')}")
            d.blocks[bid] = Block(bid, "cap", m.name, lines, tip=tip)
            d.arrows.append(Arrow(rail(net), bid, dashed=True))
    if missing:
        d.notes.append(f"Not on a rail of this board: {_refs_text(missing)}")


def _orient_links(d: Diagram, links) -> List[Tuple[str, str, str]]:
    """Point each series link away from the VRMs: rails are numbered by how many blocks
    separate them from a source (breadth first), and a link runs from its nearer rail."""
    drives: Dict[str, List[str]] = {}            # rail -> rails it feeds through a VRM
    for a in d.arrows:
        if a.src.startswith("rail:") and a.dst.startswith("vrm:"):
            for b in d.arrows:
                if b.src == a.dst:
                    drives.setdefault(a.src, []).append(b.dst)
    touching: Dict[str, List[Tuple[str, str]]] = {}
    for bid, a, b in links:
        touching.setdefault(a, []).append((bid, b))
        touching.setdefault(b, []).append((bid, a))
    fed = {a.dst for a in d.arrows if a.src.startswith("vrm:")}
    sources = [b.id for b in d.blocks.values()
               if b.kind == "vrm" and not any(a.dst == b.id for a in d.arrows)]
    seeds = [a.dst for a in d.arrows if a.src in sources]
    dist: Dict[str, int] = {}
    oriented: Dict[str, Tuple[str, str]] = {}
    rails = sorted(bid for bid, b in d.blocks.items() if b.kind == "rail")
    # Driven rails first, then any rail still unreached (nothing drives it).
    for start in seeds + sorted(fed) + rails:
        if start in dist:
            continue
        dist[start] = 0
        queue = deque([start])
        while queue:
            r = queue.popleft()
            for link, other in touching.get(r, []):
                oriented.setdefault(link, (r, other))
                if other not in dist:
                    dist[other] = dist[r] + 1
                    queue.append(other)
            for other in drives.get(r, []):
                if other not in dist:
                    dist[other] = dist[r] + 1
                    queue.append(other)
    return [(bid, *oriented.get(bid, (a, b))) for bid, a, b in links]


_KIND_ORDER = {"vrm": 0, "rail": 1, "link": 2, "load": 3, "cap": 4}


def _layout(d: Diagram):
    """Columns by the longest chain of arrows into each block; rows ordered by where
    their feeding blocks sit, to keep arrows from crossing."""
    preds: Dict[str, List[str]] = {bid: [] for bid in d.blocks}
    succs: Dict[str, List[str]] = {bid: [] for bid in d.blocks}
    for a in d.arrows:
        preds[a.dst].append(a.src)
        succs[a.src].append(a.dst)
    indeg = {bid: len(p) for bid, p in preds.items()}
    col = {bid: 0 for bid in d.blocks}
    queue = deque(sorted(bid for bid, n in indeg.items() if n == 0))
    done = set()
    while len(done) < len(d.blocks):
        if not queue:                  # a loop (a rail feeding itself): break it
            bid = min((b for b in d.blocks if b not in done), key=lambda b: indeg[b])
            queue.append(bid)
            indeg[bid] = 0
        bid = queue.popleft()
        if bid in done:
            continue
        done.add(bid)
        for s in succs[bid]:
            if s in done:
                continue
            col[s] = max(col[s], col[bid] + 1)
            indeg[s] -= 1
            if indeg[s] <= 0:
                queue.append(s)
    # Leaves (loads, capacitors) sit right beside their rail.
    for bid, b in d.blocks.items():
        if b.kind in ("load", "cap") and preds[bid]:
            col[bid] = max(col[p] for p in preds[bid]) + 1
    for bid, b in d.blocks.items():
        b.column = col[bid]
    for c in d.columns():
        def key(b):
            rows = [d.blocks[p].row for p in preds[b.id] if d.blocks[p].column < b.column]
            return (sum(rows) / len(rows) if rows else -1, _KIND_ORDER[b.kind],
                    _natural(b.title))
        for i, b in enumerate(sorted(c, key=key)):
            b.row = i
