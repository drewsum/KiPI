"""The Coupled Noise solve (#69): the aggressor's waveforms first, then its coupling.

The waveforms are reported as a partial result as soon as they are built, so the GUI
shows them while the coupling paths solve. With the board model on, the rails' and
ground's copper is meshed first: the hot loops are traced through it (#187) before the
waveforms, which need their inductance, and the board model solves on the same mesh
after them, with their harmonics.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import ProgressReporter
from kipi.dc.pi.model import PISetup
from kipi.noise.checks import NoiseError, find_vrm, operating_point, validate_noise
from kipi.noise.parasitics import phase_layout
from kipi.noise.result import NoiseResult
from kipi.noise.waveforms import CapBranch, PhaseParts, harmonic_count, synthesize


def _unique(branches: List[CapBranch]) -> List[CapBranch]:
    seen: Dict[str, CapBranch] = {}
    for b in branches:
        seen.setdefault(b.ref, b)
    return list(seen.values())


def _scan_candidates(snap, setup, agg) -> Dict[str, float]:
    """The nets the scan will look at, when it is on."""
    if not setup.noise.scan:
        return {}
    from kipi.noise.scan import candidates
    exclude = [ph.switch_net for ph in agg.phases] + [v.net for v in setup.victims]
    return candidates(snap, agg, setup.noise.scan_radius_mm, exclude)


def _victims(snap, setup, agg, wf, layouts, result, progress, cand):
    """The victims this solve covers: the setup's (all, one or none, by the settings) and
    the worst nets the scan finds among ``cand``."""
    from kipi.noise.model import scan_victim
    s = setup.noise
    if s.victims == "all":
        chosen = list(setup.victims)
    elif s.victims == "none":
        chosen = []
    else:
        chosen = [v for v in setup.victims if v.name == s.victims]
        if not chosen:
            progress.warn(f"No victim named {s.victims}: solving them all")
            chosen = list(setup.victims)
    if not s.scan:
        return chosen
    from kipi.noise.checks import guess_ends
    from kipi.noise.scan import estimate
    progress.stage("Scan")
    with progress.timed("scan"):
        rows = estimate(snap, setup, agg, wf, layouts, cand)
    for row in rows[:max(s.scan_top_n, 0)]:
        drv, rcv = guess_ends(snap, row.net)
        if drv is None or rcv is None:
            continue
        row.solved = True
        chosen.append(scan_victim(s, row.net, drv, rcv))
    result.scan = rows
    progress.info(f"Scan: {len(rows)} net(s) within {s.scan_radius_mm:g} mm, the worst "
                  f"{sum(r.solved for r in rows)} solved in full")
    return chosen


def _ripple_probes(setup, agg, warnings, progress):
    """The AC probes the aggressor's ripple is wanted at (#157)."""
    by_name = {p.name: p for p in setup.probes}
    for name in agg.ripple_probes:
        if name not in by_name:
            msg = f"No AC probe named {name}: no ripple shown there"
        elif by_name[name].on_die:
            msg = (f"AC probe {name} is on a package's die; its ripple is shown at "
                   "the package part's pins, without the package")
        else:
            continue
        if msg not in warnings:
            warnings.append(msg)
            progress.warn(msg)
    return [by_name[n] for n in agg.ripple_probes if n in by_name]


def _victim_grounds(snap, gnd, victims, cand) -> Dict[str, list]:
    """Each victim's (and scan candidate's) driver and receiver ground pad ids."""
    from kipi.noise.checks import guess_ends
    from kipi.noise.extract.common import ground_pads
    from kipi.noise.victims import ends
    out = {}
    for v in victims:
        d, r, _ = ends(snap, v)
        out[v.name] = (ground_pads(snap, d.ref, gnd) if d else [],
                       ground_pads(snap, r.ref, gnd) if r else [])
    for net in cand:
        d, r = guess_ends(snap, net)
        if d is not None and r is not None:
            out.setdefault(net, (ground_pads(snap, d.ref, gnd), ground_pads(snap, r.ref, gnd)))
    return out


def _copper(snap, setup, agg, op, cand, warnings, progress):
    """The board model's mesh of the rails and ground, built before the waveforms so the
    hot loops can be traced through it (#187): (mesh with its node kinds, CopperPaths), or
    Nones with a warning where it cannot be built."""
    from kipi.ac.solver.fem import ACError, mesh_rail_and_ground
    from kipi.core.mesh.mesher import MeshError
    from kipi.dc.solver import SolveError
    from kipi.noise.extract.common import aggressor_ground, board_ports
    from kipi.noise.extract.copper import CopperPaths
    try:
        gnd = aggressor_ground(setup, agg)
        grounds = _victim_grounds(snap, gnd, setup.victims, cand) if gnd else {}
        bp = board_ports(snap, setup, agg, op, grounds,
                         _ripple_probes(setup, agg, warnings, progress))
        progress.stage("Board model: mesh")
        mesh = mesh_rail_and_ground(snap, sorted(bp.rail), bp.ground_nets(),
                                    bp.terminal_ids(), setup.settings, setup.ac, progress)
        copper = CopperPaths(snap, mesh[0], setup.settings, setup.ac)
    except (ACError, MeshError, SolveError) as e:
        msg = (f"Board model left out ({e}); the hot loops are drawn straight between the "
               "pads, the capacitors share the current by their own impedance and there is "
               "no shared-ground coupling")
        warnings.append(msg)
        progress.warn(msg)
        return None, None
    return mesh, copper


def _board_model(snap, setup, agg, op, wf, result, progress, victims, mesh, dc_result):
    """Run the board model and put its capacitor currents and ripple into the waveforms;
    None (with a warning) where it cannot be built."""
    from kipi.ac.solver.fem import ACError
    from kipi.core.mesh.mesher import MeshError
    from kipi.dc.solver import SolveError
    from kipi.noise.extract.common import aggressor_ground, solve_board
    from kipi.noise.waveforms import Spectrum
    gnd = aggressor_ground(setup, agg)
    grounds = _victim_grounds(snap, gnd, victims, {})
    progress.stage("Board model")
    try:
        with progress.timed("board model"):
            bm = solve_board(snap, setup, agg, op, wf, grounds, progress,
                             _ripple_probes(setup, agg, result.warnings, progress), mesh,
                             dc_result)
    except (ACError, MeshError, SolveError) as e:
        msg = f"Board model left out ({e}); the capacitors share the current by their own " \
              "impedance and there is no shared-ground coupling"
        result.warnings.append(msg)
        progress.warn(msg)
        return None
    wf.input_caps = {r: Spectrum(f"I({r})", "A", 0.0, c) for r, c in bm.cap_currents.items()
                     if bm.cap_rail[r] == "in"}
    wf.output_caps = {r: Spectrum(f"I({r})", "A", 0.0, c) for r, c in bm.cap_currents.items()
                      if bm.cap_rail[r] == "out"}
    wf.input_ripple = Spectrum("Vin ripple", "V", 0.0, bm.input_ripple)
    wf.output_ripple = Spectrum("Vout ripple", "V", 0.0, bm.output_ripple)
    wf.probe_ripple = {n: Spectrum(f"V({n})", "V", 0.0, c) for n, c in bm.probe_ripple.items()}
    result.board_note = (f"Board model: {len(bm.cap_currents)} capacitor(s) on the rails, "
                         f"{bm.unknowns} unknowns, {len(bm.field_freqs)} field solves, plane "
                         f"capacitance {bm.plane_capacitance_f * 1e9:.3g} nF")
    if bm.missing_caps:
        bm.notes.append(f"{len(bm.missing_caps)} capacitor(s) on the rails have no model: "
                        f"taken at their value from the board with typical ESR and ESL "
                        f"({', '.join(bm.missing_caps[:12])})")
    for n in bm.notes:
        result.warnings.append(n)
        progress.warn(n)
    progress.info(result.board_note)
    return bm


def _paths(layouts) -> Dict[str, np.ndarray]:
    """Each capacitor loop's drawn path, (N, 2, 3) segment ends as (x, y, z) mm."""
    out = {}
    for i, lay in enumerate(layouts, 1):
        for ref, loop in lay.loops.items():
            segs = [np.stack([p[:-1], p[1:]], axis=1) for p in loop.paths if len(p) >= 2]
            if segs:
                out[f"{ref} (phase {i})"] = np.concatenate(segs)
    return out


def solve_noise(snap: BoardSnapshot, setup: PISetup, aggressor: str,
                progress: Optional[ProgressReporter] = None, dc_result=None) -> NoiseResult:
    """``dc_result``, the last DC power-tree solve if any, gives the board model the DC
    voltage across the capacitors and the current through diode links, as AC mode's
    solve takes them."""
    progress = progress or ProgressReporter()
    agg = next((a for a in setup.aggressors if a.name == aggressor), None)
    if agg is None:
        raise NoiseError(f"No aggressor named {aggressor}")
    errors = validate_noise(setup, snap, agg)
    if errors:
        raise NoiseError("; ".join(errors))
    vrm = find_vrm(setup, agg)
    op = operating_point(setup, agg)
    vin_net = vrm.input.net if vrm.input else ""
    vout_net = vrm.output.net
    gnd = vrm.ground.net if vrm.ground else ""
    warnings: List[str] = []
    cand = _scan_candidates(snap, setup, agg)

    mesh = copper = None
    if setup.noise.board_model:
        mesh, copper = _copper(snap, setup, agg, op, cand, warnings, progress)
    progress.stage("Hot loop")
    layouts = []
    for i, ph in enumerate(agg.phases, 1):
        progress.check_cancel()
        with progress.timed(f"phase {i} layout"):
            ground = gnd or (ph.low_side.net if ph.low_side else "")
            layouts.append(phase_layout(snap, setup, ph, i, op.topology, op.vin, op.vout,
                                        vin_net, vout_net, ground,
                                        agg.min_ring_zeta, copper))
    reports = [lay.report for lay in layouts]

    progress.stage("Waveforms")
    rings = [r.f_ring_hz for r in reports if r.f_ring_hz]
    k, f_max, capped = harmonic_count(agg.fsw_hz, agg.rise_s, agg.fall_s,
                                      max(rings) if rings else None,
                                      setup.noise.f_max_hz, setup.noise.max_harmonics)
    parts = [PhaseParts(ph.inductance_h, r.f_ring_hz, r.zeta, r.c_sw_f,
                        agg.soft_edge_zeta, r.qrr_c,
                        setup.noise.dead_time_ns * 1e-9,
                        ph.rds_on_hs_ohm or 0.0, ph.rds_on_ls_ohm or 0.0)
             for ph, r in zip(agg.phases, reports)]
    wf = synthesize(op.topology, op.vin, op.vout, op.iout, op.iin, op.duty, agg.fsw_hz,
                    agg.rise_s, agg.fall_s, parts, k,
                    _unique([b for lay in layouts for b in lay.input_caps]),
                    _unique([b for lay in layouts for b in lay.output_caps]), f_max)
    if capped:
        warnings.append(f"Harmonics stop at {f_max / 1e6:.4g} MHz ({k}): raise the "
                        "harmonic limit for the full edge content")
    for r in reports:
        warnings.extend(f"Phase {r.phase}: {n}" for n in r.notes)
    wf.warnings = list(warnings)
    result = NoiseResult(agg.name, vrm.name, op, wf, reports, warnings)
    result.loop_paths = _paths(layouts)
    for line in result.summary_lines():
        progress.info(line)
    for w in warnings:
        progress.warn(w)
    progress.partial(result)

    victims = _victims(snap, setup, agg, wf, layouts, result, progress, cand)

    board = None
    if mesh is not None:
        board = _board_model(snap, setup, agg, op, wf, result, progress, victims, mesh,
                             dc_result)
        if board is not None:
            for line in result.probe_lines():
                progress.info(line)
            progress.partial(result)
    elif agg.ripple_probes and not setup.noise.board_model:
        msg = "The ripple at AC probes needs the board model (Settings tab): not shown"
        result.warnings.append(msg)
        progress.warn(msg)

    if victims:
        from kipi.noise.extract.xsection import Cache
        from kipi.noise.victims import couple
        progress.stage("Victims")
        cache = Cache()
        scanned = {r.net for r in result.scan if r.solved}
        for i, victim in enumerate(victims):
            progress.fraction(i / len(victims))
            progress.debug(f"Victim {i + 1}/{len(victims)}: {victim.name}")
            with progress.timed(f"victim {victim.name}"):
                vr = couple(snap, setup, victim, agg, wf, cache, progress.check_cancel,
                            layouts, board.ground_diff.get(victim.name)
                            if board is not None else None)
            vr.scanned = victim.name in scanned and victim not in setup.victims
            result.victims.append(vr)
            ok = "PASS" if vr.passed else "FAIL"
            progress.info(f"Victim {vr.name}: {vr.peak_v * 1e3:.4g} mV peak "
                          f"({vr.ratio * 100:.3g} % of its margin, {ok}); "
                          f"{vr.solves} cross-section(s) solved")
            for w in vr.warnings:
                progress.warn(f"Victim {vr.name}: {w}")
        progress.fraction(1.0)
        progress.debug(f"Cross-sections: {cache.misses} solved, {cache.hits} reused")
    return result
