"""Find the board's pads for pins named by part reference and pad number.

A power-tree file names pins as ``<reference>_<pad number>`` and nets by their schematic
names. The board is the authority on which net a pad is on: KiCad names a local label
``/Sheet/label``, and a net with two labels takes one of them on the board, so the
schematic name is matched through the pads, never trusted on its own.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from kipi.model.board import BoardSnapshot, Pad
from kipi.pi.model import PinRef


def split_pin(pin: str) -> Tuple[str, str]:
    """``"U13_28"`` -> ``("U13", "28")``; the pad number is after the last underscore."""
    ref, _, number = pin.rpartition("_")
    return ref, number


class PadIndex:
    def __init__(self, snap: BoardSnapshot):
        self.by_pin: Dict[Tuple[str, str], List[Pad]] = defaultdict(list)
        self.by_part: Dict[str, List[Pad]] = defaultdict(list)
        for p in snap.pads:
            if p.footprint:
                self.by_pin[(p.footprint, p.number)].append(p)
                self.by_part[p.footprint].append(p)
        self.board_nets = {n.name for n in snap.nets if n.name}
        self.net_alias: Dict[str, str] = {}      # schematic net name -> board net name

    def pads_of(self, pin: str) -> List[Pad]:
        return self.by_pin.get(split_pin(pin), [])

    def learn(self, schematic_net: str, pins: Iterable[str]):
        """Record which board net a schematic net is, from pins the file says are on it."""
        if not schematic_net or schematic_net in self.net_alias:
            return
        nets = {p.net for pin in pins for p in self.pads_of(pin) if p.net}
        if len(nets) == 1:
            self.net_alias[schematic_net] = nets.pop()

    def board_net(self, schematic_net: str) -> Optional[str]:
        """The board's name for a schematic net: learned from pads, or the same name, or
        the one board net ending in ``/<name>`` (a KiCad local label)."""
        if schematic_net in self.net_alias:
            return self.net_alias[schematic_net]
        if schematic_net in self.board_nets:
            return schematic_net
        tail = [n for n in self.board_nets if n.endswith("/" + schematic_net)]
        return tail[0] if len(tail) == 1 else None

    def pin_ref(self, pins: List[str], notes: List[str], what: str) -> Optional[PinRef]:
        """One part's pins on one board net. Pins the board does not have are reported; if
        the pins land on several nets, the net holding most of them is used and the
        others are reported."""
        found = [(pin, p) for pin in pins for p in self.pads_of(pin)]
        missing = [pin for pin in pins if not self.pads_of(pin)]
        if missing:
            notes.append(f"{what}: {', '.join(missing)} not on the board")
        if not found:
            return None
        by_net: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for _pin, pad in found:
            by_net[(pad.footprint, pad.net)].append(pad.number)
        (ref, net), numbers = max(by_net.items(), key=lambda kv: len(kv[1]))
        if len(by_net) > 1:
            others = ", ".join(f"{r}_{n} on {nt}" for (r, nt), ns in by_net.items()
                               if (r, nt) != (ref, net) for n in ns)
            notes.append(f"{what}: pins on more than one net; using {net}, leaving out {others}")
        return PinRef(ref, net, sorted(set(numbers), key=lambda n: (len(n), n)))

    def part_on_net(self, ref: str, board_net: str) -> Optional[PinRef]:
        numbers = [p.number for p in self.by_part.get(ref, []) if p.net == board_net]
        return PinRef(ref, board_net, sorted(numbers, key=lambda n: (len(n), n))) if numbers else None

    def other_side(self, ref: str, board_net: str) -> Optional[PinRef]:
        """A two-sided part's pads on its one other net."""
        nets = {p.net for p in self.by_part.get(ref, []) if p.net and p.net != board_net}
        return self.part_on_net(ref, nets.pop()) if len(nets) == 1 else None
