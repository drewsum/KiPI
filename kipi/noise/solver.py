"""The Coupled Noise solve (#69): the aggressor's waveforms first, then its coupling.

The waveforms are reported as a partial result as soon as they are built, so the GUI
shows them while the coupling paths solve.
"""

from __future__ import annotations

from typing import Dict, List, Optional

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
    parts = [PhaseParts(ph.inductance_h, r.f_ring_hz, r.zeta)
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

    if setup.victims:
        from kipi.noise.extract.xsection import Cache
        from kipi.noise.victims import couple
        progress.stage("Victims")
        cache = Cache()
        for i, victim in enumerate(setup.victims):
            progress.fraction(i / len(setup.victims))
            with progress.timed(f"victim {victim.name}"):
                vr = couple(snap, setup, victim, agg, wf, cache, progress.check_cancel)
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
