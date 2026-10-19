"""The 3D view's "PI Models" markers in Coupled Noise mode (#69): AC mode's VRMs, series links
and modelled capacitors (not its probes, which a noise solve does not use), plus each
victim's driver and receiver pins.

A victim gets two markers, "<victim> driver" and "<victim> receiver", both of the setup
item ``victim`` so that selecting the victim marks both ends. Ends not set are the ones a
solve guesses. They show whether or not the victim's net is ticked in the Board panel.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from kipi.ac.viz.markers import ac_markers
from kipi.core.model.board import BoardSnapshot
from kipi.dc.viz.markers import PartMarker, markers_from


def noise_markers(snap: Optional[BoardSnapshot], setup,
                  nets: Iterable[str]) -> List[PartMarker]:
    if snap is None or setup is None:
        return []
    from kipi.noise.victims import ends
    out = [m for m in ac_markers(snap, setup, nets) if m.kind != "probe"]
    parts = []
    for v in setup.victims:
        driver, receiver, _ = ends(snap, v)
        for pin, role in ((driver, "driver"), (receiver, "receiver")):
            if pin is not None:
                parts.append(("victim", v.name, f"{v.name} {role}", [pin], []))
    victim_nets = {v.net for v in setup.victims}
    return out + markers_from(snap, setup, victim_nets, parts)
