"""Guesses that pre-fill the setup editors as the user picks parts.

Each guess reads only the board and the setup so far, and returns None when it can't
tell, in which case the editor leaves the field as it was. The editors only apply a guess
to a field the user hasn't set themselves, and every guessed value can be changed.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from kipi.core.model.board import BoardSnapshot
from kipi.core.model.nets import classify_nets, is_ground_name, is_power_name
from kipi.core.model.parts import is_capacitor_ref
from kipi.dc.pi.model import PinRef, PISetup

_REF_RE = re.compile(r"^(.*?)(\d+)$")


def part_nets(snap: Optional[BoardSnapshot], ref: str) -> Dict[str, List[str]]:
    """{net: [pad numbers]} for footprint ``ref``'s pads that are on a net."""
    if snap is None or not ref:
        return {}
    index = snap.__dict__.get("_ref_index")
    if index is None:
        index = {}
        for p in snap.pads:
            if p.footprint and p.net:
                index.setdefault(p.footprint, {}).setdefault(p.net, []).append(p.number)
        # Not a dataclass field, so snapshots never serialize it.
        snap.__dict__["_ref_index"] = index
    return {net: sorted(set(pins)) for net, pins in index.get(ref, {}).items()}


def other_net(snap: Optional[BoardSnapshot], ref: str, net: str) -> Optional[str]:
    """The net on the other side of a two-terminal part (one whose pads are on exactly two
    nets: a resistor, ferrite, fuse, inductor...), or None."""
    nets = part_nets(snap, ref)
    if len(nets) != 2 or net not in nets:
        return None
    return next(n for n in nets if n != net)


def setup_grounds(setup: Optional[PISetup]) -> List[str]:
    """Ground nets the setup already uses, most used first."""
    if setup is None:
        return []
    refs = [ph.ground for v in setup.vrms for ph in v.all_phases()]
    refs += [sp.ground_sense for v in setup.vrms for sp in v.all_sense_points()]
    refs += [ld.ground for ld in setup.loads]
    refs += [p.ground for p in setup.probes]
    count: Dict[str, int] = {}
    for r in refs:
        if r is not None and r.net:
            count[r.net] = count.get(r.net, 0) + 1
    return sorted(count, key=lambda n: -count[n])


def _biggest_net(snap: BoardSnapshot) -> Optional[str]:
    """The net with the most pads, which on almost every board is the ground."""
    big = snap.__dict__.get("_biggest_net", "")
    if big == "":
        count: Dict[str, int] = {}
        for p in snap.pads:
            if p.net:
                count[p.net] = count.get(p.net, 0) + 1
        big = max(count, key=count.get) if count else None
        snap.__dict__["_biggest_net"] = big
    return big


def looks_like_ground(snap: Optional[BoardSnapshot], net: str,
                      known: Iterable[str] = ()) -> bool:
    """Whether ``net`` is a ground: a ``known`` one, one named like a ground, or the
    board's biggest net when it isn't named like a rail."""
    if net in set(known) or is_ground_name(net):
        return True
    return snap is not None and net == _biggest_net(snap) and not is_power_name(net)


def ground_net(snap: Optional[BoardSnapshot], ref: str, exclude: Iterable[str] = (),
               allowed: Optional[Iterable[str]] = None,
               known: Iterable[str] = ()) -> Optional[str]:
    """The ground net of part ``ref``, or None.

    Ground nets aren't always called GND, so this tries, in order: ``known`` grounds
    (those the setup already uses), nets named like a ground (GND, PGND, VSS...), then
    the board's biggest net if it isn't named like a rail. When the part has pads on
    several, the one with the most of its pads wins. Nets in ``exclude`` (the part's
    supply) are skipped, and so are nets outside ``allowed`` (the nets ticked in the
    Board panel), unless they are known grounds.
    """
    nets = part_nets(snap, ref)
    known = list(known)
    allowed = None if allowed is None else set(allowed) | set(known)
    cands = [n for n in nets if n not in set(exclude) and (allowed is None or n in allowed)]
    if not cands:
        return None
    big = _biggest_net(snap)
    for test in (lambda n: n in known,
                 is_ground_name,
                 lambda n: n == big and not is_power_name(n)):
        hits = [n for n in cands if test(n)]
        if hits:
            return max(hits, key=lambda n: (len(nets[n]), -cands.index(n)))
    return None


def input_net(snap: Optional[BoardSnapshot], ref: str, exclude: Iterable[str] = (),
              allowed: Optional[Iterable[str]] = None) -> Optional[str]:
    """The input rail of regulator ``ref``: its only other power net, or None.

    E.g. an LDO with pads on +5V, +3V3 and GND whose output is +3V3 gives +5V. With two
    or more candidates (an EN pin tied to a second rail, say) it doesn't guess.
    """
    if snap is None:
        return None
    kinds = snap.__dict__.get("_net_kinds")
    if kinds is None:
        kinds = snap.__dict__["_net_kinds"] = classify_nets(snap)
    allowed = None if allowed is None else set(allowed)
    cands = [n for n in part_nets(snap, ref)
             if n not in set(exclude) and kinds.get(n) == "power" and not is_ground_name(n)
             and (allowed is None or n in allowed)]
    return cands[0] if len(cands) == 1 else None


def next_part(snap: Optional[BoardSnapshot], ref: str, net: str,
              taken: Iterable[str] = ()) -> Optional[str]:
    """The next part in sequence after ``ref`` with pads on ``net``: L1 -> L2, Q4 -> Q5.

    Skips ``taken`` references and gaps in the numbering, and only follows parts with
    the same prefix, so the phases of a multi-phase regulator can be filled in order.
    """
    m = _REF_RE.match(ref or "")
    if snap is None or not m:
        return None
    prefix, num = m.group(1), int(m.group(2))
    taken = set(taken)
    best = None
    _, _, pads, _ = snap.items_on_net(net)
    for p in pads:
        pm = _REF_RE.match(p.footprint or "")
        if (pm and pm.group(1) == prefix and int(pm.group(2)) > num
                and p.footprint not in taken and (best is None or int(pm.group(2)) < best[0])):
            best = (int(pm.group(2)), p.footprint)
    return best[1] if best else None


def pins_on(snap: Optional[BoardSnapshot], ref: str, net: str, pins: List[str]) -> List[str]:
    """``pins`` if part ``ref`` has all of them on ``net``, else [] (every pin on the net)."""
    have = set(part_nets(snap, ref).get(net, []))
    return list(pins) if pins and set(pins) <= have else []


# KiCad can number each schematic sheet's parts from the sheet number x 100 (or x 1000),
# so the same part on the next sheet is a multiple of this further on.
SHEET_STEP = 100


def _split_ref(ref: Optional[str]):
    """``("L", 101)`` for ``"L101"``; None for a reference that does not end in a number."""
    m = _REF_RE.match(ref or "")
    return (m.group(1), int(m.group(2))) if m else None


def next_phase_refs(snap: Optional[BoardSnapshot],
                    phases: List[List[Optional[PinRef]]]) -> List[Optional[PinRef]]:
    """A new phase's pins, role by role, from the phases so far (each a list of its pins in
    the same roles: output, input, ground). The last phase's pins move to the parts the new
    phase most likely uses, keeping their pin numbers where those parts have them:

    1. the same step on as from the phase before (L1, L3 -> L5);
    2. one schematic sheet on, when every role has a part there with the same number on
       that sheet: L101, Q101, Q102 -> L201, Q201, Q202 (#91);
    3. the next part in sequence on the role's net (L1 -> L2, Q1 -> Q2);
    4. the last phase's own part, for the user to change.
    """
    last = phases[-1]
    roles = range(len(last))
    taken = [{ph[i].ref for ph in phases if ph[i] is not None} for i in roles]
    out: List[Optional[PinRef]] = [None] * len(last)

    def free(i: int, ref: str) -> bool:
        return ref not in taken[i] and last[i].net in part_nets(snap, ref)

    def moved(i: int, ref: str) -> PinRef:
        prev = last[i]
        return PinRef(ref, prev.net, pins_on(snap, ref, prev.net, prev.pins))

    def step(i: int) -> int:
        """How far the role's part moved since the latest earlier phase on another part (a
        phase that repeats the last one's part is passed over); 0 if not forward."""
        b = _split_ref(last[i].ref)
        for ph in reversed(phases[:-1]):
            if ph[i] is not None and ph[i].ref != last[i].ref:
                a = _split_ref(ph[i].ref)
                return b[1] - a[1] if a and b and a[0] == b[0] and b[1] > a[1] else 0
        return 0

    todo = [i for i in roles if last[i] is not None and last[i].ref]
    if snap is None:
        todo = []
    # 1. The step from the phase before.
    for i in list(todo):
        d = step(i)
        if d:
            prefix, num = _split_ref(last[i].ref)
            if free(i, f"{prefix}{num + d}"):
                out[i] = moved(i, f"{prefix}{num + d}")
                todo.remove(i)
    # 2. One sheet on: the same shift, a multiple of SHEET_STEP, for every role left.
    nums = {i: _split_ref(last[i].ref) for i in todo}
    if todo and all(n is not None and n[1] >= SHEET_STEP for n in nums.values()):
        prefix, num = nums[todo[0]]
        shifts = sorted({n[1] - num for p in snap.items_on_net(last[todo[0]].net)[2]
                         if (n := _split_ref(p.footprint)) and n[0] == prefix
                         and n[1] > num and (n[1] - num) % SHEET_STEP == 0})
        for d in shifts:
            refs = {i: f"{nums[i][0]}{nums[i][1] + d}" for i in todo}
            if all(free(i, r) for i, r in refs.items()):
                for i, r in refs.items():
                    out[i] = moved(i, r)
                todo = []
                break
    # 3. The next part in sequence, unless the phases so far are a sheet apart (the next
    # in sequence is then on the last phase's sheet), or 4. the same part.
    for i in todo:
        by_sheet = step(i) > 0 and step(i) % SHEET_STEP == 0
        ref = None if by_sheet else next_part(snap, last[i].ref, last[i].net, taken[i])
        out[i] = moved(i, ref) if ref else PinRef(last[i].ref, last[i].net, list(last[i].pins))
    for i in roles:
        if out[i] is None and last[i] is not None:
            out[i] = PinRef(last[i].ref, last[i].net, list(last[i].pins))
    return out


# ---------------------------------------------------------------------------
# Whole pin references, as the editors use them
# ---------------------------------------------------------------------------

def guess_ground_pins(snap: Optional[BoardSnapshot], setup: Optional[PISetup],
                      nets: Optional[Iterable[str]], supply: Optional[PinRef],
                      prefer: Iterable[str] = ()) -> Optional[PinRef]:
    """The ground pins of the part whose supply pins are ``supply``: the same part, on
    its ground net (see :func:`ground_net`; ``prefer`` grounds come first)."""
    if supply is None or not supply.ref:
        return None
    known = [n for n in prefer if n] + setup_grounds(setup)
    gnd = ground_net(snap, supply.ref, [supply.net], nets, list(dict.fromkeys(known)))
    return PinRef(supply.ref, gnd) if gnd else None


def guess_input_pins(snap: Optional[BoardSnapshot], nets: Optional[Iterable[str]],
                     output: Optional[PinRef],
                     ground: Optional[PinRef] = None) -> Optional[PinRef]:
    """A regulator's input pins on the same part as its output pins (see :func:`input_net`)."""
    if output is None or not output.ref:
        return None
    exclude = [output.net] + ([ground.net] if ground else [])
    net = input_net(snap, output.ref, exclude, nets)
    return PinRef(output.ref, net) if net else None


def guess_other_side(snap: Optional[BoardSnapshot], side: Optional[PinRef],
                     grounds: Iterable[str] = ()) -> Optional[PinRef]:
    """The other side of a two-terminal series part: the same part, on its other net.

    Not for a capacitor, nor to a ground net (``grounds`` and those that look like one):
    a two-terminal part to ground is a bypass or a load, not a part in series.
    """
    if side is None or not side.ref or is_capacitor_ref(side.ref):
        return None
    net = other_net(snap, side.ref, side.net)
    if net is None or looks_like_ground(snap, net, grounds):
        return None
    return PinRef(side.ref, net)
