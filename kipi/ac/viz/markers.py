"""Where the parts of an impedance sweep sit on the board, for the 3D view's "PI Models"
markers (#84) in AC mode: a point on each pad of a VRM, series link, probe or modelled
capacitor, with the part's name beside them.

The pieces are DC mode's (:mod:`kipi.dc.viz.markers`); only the list of parts differs. A
sweep leaves the DC loads out — their current says nothing about impedance — and marks the
probes instead, which is where the impedance is looked into.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from kipi.core.model.board import BoardSnapshot
from kipi.dc.viz.markers import PartMarker, link_part, markers_from, vrm_part


def ac_markers(snap: Optional[BoardSnapshot], setup,
               nets: Iterable[str]) -> List[PartMarker]:
    """AC mode's markers: the setup's VRMs, series links, probes and modelled capacitors,
    on the pads of ``nets`` only."""
    if snap is None or setup is None:
        return []
    parts = [vrm_part(v) for v in setup.vrms]
    parts += [link_part(k) for k in setup.links]
    parts += [("probe", p.name, f"Probe {p.name}", [p.rail], [p.ground])
              for p in setup.probes]
    return markers_from(snap, setup, nets, parts)
