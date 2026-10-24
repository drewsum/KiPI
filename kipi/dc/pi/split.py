"""One part's load split across several of its supply nets (#182): when only the part's
total current is known, say a chip drawing 2 A over its core pins, some of them on a
ferrite bead filtered net of their own.

The total is shared out over the nets, by their pin counts (every supply pin drawing the
same current) or equally (each net the same), and each net gets a load of its own.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from kipi.core.model.board import BoardSnapshot
from kipi.dc.pi.autofill import looks_like_ground, part_nets, setup_grounds
from kipi.dc.pi.model import Load, PISetup

PER_PIN, PER_NET = "pin", "net"


def supply_nets(snap: Optional[BoardSnapshot], setup: Optional[PISetup],
                ref: str) -> Dict[str, List[str]]:
    """{net: [pin numbers]} for part ``ref``'s nets that could supply it: neither a
    ground nor a pad left unconnected. A net the setup already loads this part on counts
    the pins that load names, when it names some."""
    grounds = setup_grounds(setup)
    out = {}
    for net, pins in part_nets(snap, ref).items():
        if net.lower().startswith("unconnected") or looks_like_ground(snap, net, grounds):
            continue
        load = existing_load(setup, ref, net)
        out[net] = list(load.supply.pins) if load is not None and load.supply.pins else pins
    return out


def split_current(pins: Dict[str, Iterable[str]], total: float,
                  per: str = PER_PIN) -> Dict[str, float]:
    """``total`` amps shared over the nets of ``pins`` ({net: [pin numbers]}): by their
    pin counts (``per`` = :data:`PER_PIN`) or equally (:data:`PER_NET`)."""
    counts = {net: len(list(p)) if per == PER_PIN else 1 for net, p in pins.items()}
    n = sum(counts.values())
    return {net: total * c / n if n else 0.0 for net, c in counts.items()}


def existing_load(setup: Optional[PISetup], ref: str, net: str) -> Optional[Load]:
    """The setup's first load on part ``ref``'s pins on ``net``, or None."""
    if setup is None:
        return None
    return next((ld for ld in setup.loads
                 if ld.supply.ref == ref and ld.supply.net == net), None)
