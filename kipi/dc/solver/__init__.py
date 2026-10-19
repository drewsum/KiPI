"""DC solver entry points: mesh -> solve -> post-process for one net."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from kipi.core.mesh.mesher import NetMesh, mesh_nets
from kipi.core.model.board import BoardSnapshot, Pad
from kipi.core.progress import NULL, ProgressReporter
from kipi.dc.solver.fem import Solution, SolveError, solve
from kipi.dc.solver.post import Fields, compute_fields
from kipi.dc.solver.setup import CurrentLoad, NetProblem, SeriesLink, SolveSettings, VoltageSource

__all__ = ["NetResult", "run", "pads_for", "NetProblem", "VoltageSource", "CurrentLoad",
           "SeriesLink", "SolveSettings", "SolveError"]


@dataclass
class NetResult:
    mesh: NetMesh
    solution: Solution
    fields: Fields


def run(snap: BoardSnapshot, problem: NetProblem,
        progress: ProgressReporter = NULL) -> NetResult:
    s = problem.settings
    with progress.timed(f"Meshing {problem.net}"):
        mesh = mesh_nets(snap, problem.nets, problem.terminal_pad_ids(), name=problem.net,
                         max_edge_mm=s.max_edge_mm,
                        min_angle_deg=s.min_angle_deg, simplify_mm=s.simplify_mm,
                        circle_quad_segs=s.circle_quad_segs, min_edge_mm=s.min_edge_mm,
                        grading=s.grading, progress=progress)
    sol = solve(snap, mesh, problem, progress)
    with progress.timed("Post-processing"):
        fields = compute_fields(sol)
        sm = fields.summary
        boost = f", max boost {sm['max_boost'] * 1e3:.3f} mV" if sm["max_boost"] > 0 else ""
        progress.info(f"Max drop {sm['max_drop'] * 1e3:.3f} mV{boost}, peak current density "
                      f"{sm['j_max']:.2f} A/mm^2, copper loss {sm['power_loss'] * 1e3:.3f} mW "
                      f"(energy balance error {sm['energy_balance_error']:.1e})")
    return NetResult(mesh, sol, fields)


def pads_for(snap: BoardSnapshot, net: str, reference: str,
             numbers: Optional[Iterable[str]] = None) -> List[Pad]:
    """Pads of footprint ``reference`` on ``net`` (optionally only the given pad numbers)."""
    _, _, pads, _ = snap.items_on_net(net)
    wanted = set(numbers) if numbers is not None else None
    out = [p for p in pads if p.footprint == reference and (wanted is None or p.number in wanted)]
    # A pin may be several pads (a microphone's ground ring in four parts). One with no
    # copper shape at all carries nothing; it is left out while another pad of the pin
    # has copper, so the mesh is not asked for copper that is not there.
    shaped = {p.number for p in out if p.shapes}
    out = [p for p in out if p.shapes or p.number not in shaped]
    if not out:
        which = f" pins {sorted(wanted)}" if wanted else ""
        raise SolveError(f"{reference}{which} has no pads on net {net}")
    return sorted(out, key=lambda p: (len(p.number), p.number))
