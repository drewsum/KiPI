"""The Coupled Noise solve (#69): the aggressor's waveforms first, then its coupling.

The waveforms are reported as a partial result as soon as they are built, so the GUI
shows them while the coupling paths solve.
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


def _victims(snap, setup, agg, wf, layouts, result, progress):
    """The victims this solve covers: the setup's (all, one or none, by the settings) and
    the worst nets the scan finds."""
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
    from kipi.noise.scan import candidates, estimate
    progress.stage("Scan")
    exclude = [ph.switch_net for ph in agg.phases] + [v.net for v in setup.victims]
    with progress.timed("scan"):
        rows = estimate(snap, setup, agg, wf, layouts,
                        candidates(snap, agg, s.scan_radius_mm, exclude))
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


def _board_model(snap, setup, agg, op, wf, result, progress, victims=None):
    """Run the board model and put its capacitor currents and ripple into the waveforms;
    None (with a warning) where it cannot be built."""
    from kipi.ac.solver.fem import ACError
    from kipi.core.mesh.mesher import MeshError
    from kipi.dc.solver import SolveError
    from kipi.noise.extract.common import ground_pads, solve_board
    from kipi.noise.victims import ends
    from kipi.noise.waveforms import Spectrum
    vrm = find_vrm(setup, agg)
    gnd = vrm.ground.net if vrm.ground else next(
        (ph.low_side.net for ph in agg.phases if ph.low_side), "")
    grounds = {}
    for v in (setup.victims if victims is None else victims):
        d, r, _ = ends(snap, v)
        grounds[v.name] = (ground_pads(snap, d.ref, gnd) if d else [],
                           ground_pads(snap, r.ref, gnd) if r else [])
    progress.stage("Board model")
    try:
        with progress.timed("board model"):
            bm = solve_board(snap, setup, agg, op, wf, grounds, progress)
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


def solve_noise(snap: BoardSnapshot, setup: PISetup, aggressor: str,
                progress: Optional[ProgressReporter] = None) -> NoiseResult:
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

    progress.stage("Hot loop")
    layouts = []
    for i, ph in enumerate(agg.phases, 1):
        progress.check_cancel()
        with progress.timed(f"phase {i} layout"):
            ground = gnd or (ph.low_side.net if ph.low_side else "")
            layouts.append(phase_layout(snap, setup, ph, i, op.topology, op.vin, op.vout,
                                        vin_net, vout_net, ground,
                                        setup.noise.min_ring_zeta))
    reports = [lay.report for lay in layouts]

    progress.stage("Waveforms")
    rings = [r.f_ring_hz for r in reports if r.f_ring_hz]
    k, f_max, capped = harmonic_count(agg.fsw_hz, agg.rise_s, agg.fall_s,
                                      max(rings) if rings else None,
                                      setup.noise.f_max_hz, setup.noise.max_harmonics)
    parts = [PhaseParts(ph.inductance_h, r.f_ring_hz, r.zeta, r.c_sw_f,
                        setup.noise.soft_edge_zeta, r.qrr_c)
             for ph, r in zip(agg.phases, reports)]
    wf = synthesize(op.topology, op.vin, op.vout, op.iout, op.iin, op.duty, agg.fsw_hz,
                    agg.rise_s, agg.fall_s, parts, k,
                    _unique([b for lay in layouts for b in lay.input_caps]),
                    _unique([b for lay in layouts for b in lay.output_caps]), f_max)
    warnings: List[str] = []
    if capped:
        warnings.append(f"Harmonics stop at {f_max / 1e6:.4g} MHz ({k}): raise the "
                        "harmonic limit for the full edge content")
    for r in reports:
        warnings.extend(f"Phase {r.phase}: {n}" for n in r.notes)
    wf.warnings = list(warnings)
    result = NoiseResult(agg.name, vrm.name, op, wf, reports, warnings)
    for line in result.summary_lines():
        progress.info(line)
    for w in warnings:
        progress.warn(w)
    progress.partial(result)

    victims = _victims(snap, setup, agg, wf, layouts, result, progress)
    result.loop_paths = {f"{ref} (phase {i})": np.array([[sg.a, sg.b] for sg in segs])
                         for i, lay in enumerate(layouts, 1) for ref, segs in lay.loops.items()
                         if segs}

    board = None
    if setup.noise.board_model:
        board = _board_model(snap, setup, agg, op, wf, result, progress, victims)
        if board is not None:
            progress.partial(result)

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
