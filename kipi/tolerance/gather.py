"""Each load's stack-up values from the results loaded now (#167): the DC drop from a DC
solve, the transient at its AC probe from that rail's AC solve, and the ripple at that probe
from each Coupled Noise aggressor that shows ripple there. What cannot be found yet comes
back as a hint saying what to run.

A load's probe is the AC probe on its pins: the same supply (and ground) pins, else the same
part on the same net. Several aggressors' ripple at one probe adds as the root sum of
squares, each side apart, as switchers that are not synchronised do on average.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from kipi.tolerance.model import Contribution

BW_LIMIT_HZ = 20e6          # a scope's bandwidth limit
NO_SWITCHER = "no switcher on this rail in Noise mode"


@dataclass
class Loaded:
    """Results held now, each with where it came from: ``(result, source, when)``."""
    dc: Optional[tuple] = None                                  # TreeResult
    ac: Dict[str, tuple] = field(default_factory=dict)          # VRM name -> ACResult
    noise: Dict[str, tuple] = field(default_factory=dict)       # aggressor -> NoiseResult


@dataclass
class Fresh:
    """One load's values from the results loaded now. ``values[kind]`` is a Contribution;
    a kind without one has ``hints[kind]`` saying what to run, or is in ``not_applicable``
    (no switcher can put ripple on the rail), when it counts as nothing."""
    values: Dict[str, Contribution] = field(default_factory=dict)
    hints: Dict[str, str] = field(default_factory=dict)
    short: Dict[str, str] = field(default_factory=dict)      # each hint in a few words
    not_applicable: Dict[str, str] = field(default_factory=dict)

    def hint(self, kind: str, short: str, text: str) -> None:
        self.short[kind], self.hints[kind] = short, text


# ------------------------------------------------------------------ the setup
def driver(setup, load, groups=None):
    """The VRM whose output rail the load's supply is on, or None."""
    from kipi.dc.pi.model import net_groups
    net = load.supply.net
    groups = groups or net_groups(setup, [net])
    group = groups.get(net, frozenset([net]))
    return next((v for v in setup.vrms
                 if any(ph.output.net in group for ph in v.all_phases())), None)


def _pins(r) -> Optional[tuple]:
    return (r.ref, r.net, tuple(sorted(r.pins))) if r is not None else None


def probe_for_load(setup, load):
    """The AC probe on the load's pins: its supply pins (and ground pins, where both have
    them) the same, else the same part on the same supply net; None without one."""
    exact = [p for p in setup.probes if _pins(p.rail) == _pins(load.supply)
             and (load.ground is None or p.ground is None
                  or _pins(p.ground) == _pins(load.ground))]
    if exact:
        return exact[0]
    return next((p for p in setup.probes if p.rail.ref == load.supply.ref
                 and p.rail.net == load.supply.net), None)


# ------------------------------------------------------------------ the results
def _dc(entry, load) -> Optional[Contribution]:
    tree, source, when = entry
    rep = next((r for r in getattr(tree, "loads", []) or [] if r.name == load.name), None)
    if rep is None or not math.isfinite(rep.v_effective) or not rep.nominal:
        return None
    drop = rep.nominal - rep.v_effective              # the mean of its pins (#167)
    return Contribution(max(drop, 0.0), max(-drop, 0.0), None, None, source, when,
                        f"mean of the pins {rep.v_effective:.5g} V of {rep.nominal:g} V")


def transient_peaks(result) -> Dict[str, Tuple[float, float, float, float]]:
    """Probe name -> (undershoot, overshoot, the same through 20 MHz), volts, for the
    probes of an AC result with a load step."""
    from kipi.ac.transient import bandwidth_limited, transients
    out = {}
    for name, tr in transients(result).items():
        bw = bandwidth_limited(tr, BW_LIMIT_HZ)
        out[name] = (tr.undershoot_v, tr.overshoot_v, bw.undershoot_v, bw.overshoot_v)
    return out


def ripple_peaks(result) -> Dict[str, Tuple[float, float, float, float]]:
    """Probe name -> (below, above, the same through 20 MHz), volts: the ripple's lowest
    and highest points from its mean, for the probes a Coupled Noise result shows ripple at."""
    from kipi.noise.waveforms import bandwidth_limited
    wf = result.waveforms
    n = wf.samples_per_period()
    out = {}
    for name, sp in (getattr(wf, "probe_ripple", None) or {}).items():
        s = sp.samples(n) - sp.mean
        b = bandwidth_limited(sp, wf.fsw_hz, BW_LIMIT_HZ).samples(n) - sp.mean
        out[name] = (float(max(-s.min(), 0.0)), float(max(s.max(), 0.0)),
                     float(max(-b.min(), 0.0)), float(max(b.max(), 0.0)))
    return out


class Gatherer:
    """Each load's fresh values from ``loaded``. The peaks of each result are worked out
    once and kept by result, as finding a transient takes a while."""

    def __init__(self):
        self._cache: Dict[int, tuple] = {}

    def _peaks(self, result, fn):
        key = id(result)
        hit = self._cache.get(key)
        if hit is None or hit[0] is not result:
            hit = self._cache[key] = (result, fn(result))
        return hit[1]

    def forget(self, keep=()) -> None:
        """Drop the peaks of results not in ``keep``."""
        ids = {id(r) for r in keep}
        self._cache = {k: v for k, v in self._cache.items() if k in ids}

    def fresh(self, setup, load, loaded: Loaded, groups=None) -> Fresh:
        out = Fresh()
        vrm = driver(setup, load, groups)
        # DC
        dc = _dc(loaded.dc, load) if loaded.dc else None
        if dc is not None:
            out.values["dc"] = dc
        else:
            out.hint("dc", "Run DC", "Run the DC solve (or open DC results)"
                     if loaded.dc is None else "Not in the DC results: solve DC again")
        probe = probe_for_load(setup, load)
        if probe is None:
            hint = f"Add an AC probe on {load.supply.ref} (Probe from load in AC mode)"
            out.hint("transient", "Add AC probe", hint)
            if any(vrm is not None and a.vrm == vrm.name for a in setup.aggressors):
                out.hint("ripple", "Add AC probe", hint)
            else:
                out.not_applicable["ripple"] = NO_SWITCHER
            return out
        self._transient(out, probe, vrm, loaded)
        self._ripple(out, setup, probe, vrm, loaded)
        return out

    def _transient(self, out: Fresh, probe, vrm, loaded: Loaded):
        rail = vrm.name if vrm is not None else ""
        found = [(r, s, w) for name, (r, s, w) in loaded.ac.items()
                 if any(c.name == probe.name for c in r.probes)]
        found.sort(key=lambda x: x[0].rail != rail)          # the load's own rail first
        for r, source, when in found:
            peaks = self._peaks(r, transient_peaks).get(probe.name)
            if peaks is None:
                continue
            under, over, under_bw, over_bw = peaks
            if vrm is not None and vrm.voltage < 0:          # a step pulls a negative rail up
                under, over, under_bw, over_bw = over, under, over_bw, under_bw
            out.values["transient"] = Contribution(under, over, under_bw, over_bw, source,
                                                   when, f"probe {probe.name} on {r.rail}")
            return
        if not probe.step_a or not probe.slew_a_per_us:
            out.hint("transient", "Set load step", f"Set a load step and slew rate on probe "
                     f"{probe.name}, then solve its rail in AC mode")
        elif found:
            out.hint("transient", "Set VRM bandwidth", f"Solve {found[0][0].rail} in AC mode "
                     "again: give its VRM a bandwidth for the transient")
        else:
            out.hint("transient", "Run AC",
                     f"Solve {rail or 'the rail'} in AC mode (or open its results)")

    def _ripple(self, out: Fresh, setup, probe, vrm, loaded: Loaded):
        listing = [a for a in setup.aggressors if probe.name in a.ripple_probes]
        on_rail = [a for a in setup.aggressors if vrm is not None and a.vrm == vrm.name]
        if not listing:
            if on_rail:
                out.hint("ripple", "Add ripple probe", f"Add probe {probe.name} to the ripple "
                         f"probes of {', '.join(a.name for a in on_rail)} in Noise mode")
            else:
                out.not_applicable["ripple"] = NO_SWITCHER
            return
        parts: List[tuple] = []
        missing = []
        for a in listing:
            got = loaded.noise.get(a.name)
            peaks = self._peaks(got[0], ripple_peaks).get(probe.name) if got else None
            if peaks is None:
                missing.append(a.name)
            else:
                parts.append((a.name, got[1], got[2], peaks))
        if not parts:
            out.hint("ripple", "Run Noise",
                     f"Solve {', '.join(missing)} in Noise mode (or open its results)")
            return
        rss = [float(np.sqrt(sum(p[3][i] ** 2 for p in parts))) for i in range(4)]
        detail = "; ".join(f"{name} {pk[0] * 1e3:.3g} / {pk[1] * 1e3:.3g} mV" for name, _, _, pk
                           in parts)
        if len(parts) > 1:
            detail = "root sum of squares of " + detail
        if missing:
            detail += f" (not solved: {', '.join(missing)})"
            out.hint("ripple", "Run Noise", f"Also solve {', '.join(missing)} in Noise mode")
        out.values["ripple"] = Contribution(*rss, ", ".join(sorted({p[1] for p in parts})),
                                            max(p[2] for p in parts), detail)
