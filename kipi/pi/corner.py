"""Turn a setup into the plain numbers one solve uses.

A setup can hold a typical and a maximum for each load current (and pin group) and each
DC connection's resistance, and a load current can be not known. The solver works on
numbers only, so before a solve every value is resolved for the chosen corner:

* ``typ``: the typical value.
* ``max``: the maximum where one is given, else the typical, and that substitution is
  listed so the result says which loads were not at their maximum.

A current that is not known is solved as 0 A and listed, so every result that depends on
it can be marked incomplete instead of passing on a current that was left out.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from kipi.pi.model import PISetup


@dataclass
class CornerInfo:
    corner: str
    unknown_current: List[str] = field(default_factory=list)   # loads solved at 0 A
    typical_used: List[str] = field(default_factory=list)      # worst case, no maximum given
    basis: dict = field(default_factory=dict)                  # load name -> "typ" / "max" / ...


class _Picker:
    def __init__(self, corner: str, info: CornerInfo):
        self.worst = corner == "max"
        self.info = info

    def value(self, name: str, typ: Optional[float], mx: Optional[float]) -> Tuple[Optional[float], str]:
        """The value to solve with and where it came from; None when not known."""
        if self.worst and mx is not None:
            return mx, "max"
        if typ is None:
            return None, "not known"
        if self.worst:
            self.info.typical_used.append(name)
            return typ, "typ (no max)"
        return typ, "typ"


def _resolve_loads(setup: PISetup, pick: _Picker, info: CornerInfo):
    for l in setup.loads:
        if l.groups:
            bases = set()
            for g in l.groups:
                label = f"{l.name} [{g.name or ','.join(g.pins)}]"
                value, basis = pick.value(label, g.current, g.current_max)
                g.current, g.current_max = (0.0 if value is None else value), None
                bases.add(basis)
                if value is None:
                    info.unknown_current.append(label)
            info.basis[l.name] = bases.pop() if len(bases) == 1 else "mixed"
            continue
        value, basis = pick.value(l.name, l.current, l.current_max)
        if value is None:
            info.unknown_current.append(l.name)
        l.current, l.current_max = (0.0 if value is None else value), None
        info.basis[l.name] = basis


def _resolve_links(setup: PISetup, pick: _Picker):
    for k in setup.links:
        if pick.worst and k.resistance_max is None and k.resistance == 0:
            continue                    # a short stays a short: nothing to warn about
        value, _ = pick.value(f"DC connection {k.name}", k.resistance, k.resistance_max)
        k.resistance, k.resistance_max = value, None


def resolve(setup: PISetup) -> Tuple[PISetup, CornerInfo]:
    """A copy of ``setup`` with every current and resistance a number for its corner."""
    out = copy.deepcopy(setup)
    info = CornerInfo(setup.corner)
    pick = _Picker(setup.corner, info)
    _resolve_loads(out, pick, info)
    _resolve_links(out, pick)
    return out, info
