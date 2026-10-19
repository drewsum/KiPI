"""What a Coupled Noise solve returns (#69)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from kipi.noise.checks import OperatingPoint
from kipi.noise.parasitics import HotLoopReport
from kipi.noise.scan import ScanRow
from kipi.noise.waveforms import SmpsWaveforms, Spectrum

MECHANISMS = ("capacitive", "inductive", "common")
TOTAL = "total"


@dataclass
class StateResult:
    """The victim held high or low (#156): its receiver's noise in that state, around the
    driver's level, against the threshold the noise must not cross."""
    name: str                           # "high" or "low"
    level_v: float                      # VOH or VOL
    threshold_v: float                  # VIH or VIL
    driver_ohm: float
    receiver_v: Optional[Spectrum] = None   # the superposition, about the level
    excursion_v: float = 0.0            # the noise's largest step toward the threshold

    @property
    def margin_v(self) -> float:
        return abs(self.level_v - self.threshold_v)

    @property
    def ratio(self) -> float:
        return self.excursion_v / self.margin_v if self.margin_v > 0 else float("inf")


@dataclass
class VictimResult:
    """The noise coupled onto one victim net, at its receiver and driver."""
    name: str
    net: str
    driver: str                         # pin labels, as shown
    receiver: str
    margin_v: float
    receiver_v: Dict[str, Spectrum] = field(default_factory=dict)   # mechanism or TOTAL
    driver_v: Dict[str, Spectrum] = field(default_factory=dict)
    peak_v: float = 0.0                 # the receiver's largest |v| over a period
    peak_by: Dict[str, float] = field(default_factory=dict)          # per mechanism
    cm_f: Dict[str, float] = field(default_factory=dict)             # switch net -> Cm
    m_h: Dict[str, float] = field(default_factory=dict)              # loop -> M (at fsw)
    length_mm: float = 0.0
    coupled_mm: float = 0.0             # of it, the length beside a switch node
    z0_ohm: float = 0.0                 # length-weighted
    solves: int = 0                     # cross-sections solved (the rest were cached)
    warnings: List[str] = field(default_factory=list)
    scanned: bool = False               # found by the scan (default terminations)
    # The line held high and held low, each with the driver's resistance in that state;
    # receiver_v and driver_v above are the high state's.
    states: List[StateResult] = field(default_factory=list)
    # Where the peak couples in (#152): the victim's segments, (S, 2, 3) ends as (x, y, z)
    # mm, and each one's part of the receiver's voltage at the peak's instant, by
    # mechanism (V, signed: positive adds to the peak). The common-ground part enters at
    # the driver.
    segments_xyz: Optional[np.ndarray] = None
    segment_v: Dict[str, np.ndarray] = field(default_factory=dict)
    common_v: float = 0.0
    driver_xyz: Optional[np.ndarray] = None
    peak_t_s: float = 0.0

    @property
    def ratio(self) -> float:
        return self.peak_v / self.margin_v if self.margin_v > 0 else float("inf")

    @property
    def passed(self) -> bool:
        return self.ratio <= 1.0

    @property
    def dominant(self) -> str:
        return max(self.peak_by, key=self.peak_by.get) if self.peak_by else ""


@dataclass
class NoiseResult:
    aggressor: str
    vrm: str
    operating: OperatingPoint
    waveforms: SmpsWaveforms
    hot_loops: List[HotLoopReport] = field(default_factory=list)   # per phase
    warnings: List[str] = field(default_factory=list)
    report_path: str = ""
    victims: List[VictimResult] = field(default_factory=list)
    board_note: str = ""                # what the board model did, "" without one
    scan: List["ScanRow"] = field(default_factory=list)              # every net estimated
    # Each capacitor loop's path, (N, 2, 3) segment ends as (x, y, z) mm (#152)
    loop_paths: Dict[str, np.ndarray] = field(default_factory=dict)

    def victim(self, name: str) -> Optional[VictimResult]:
        return next((v for v in self.victims if v.name == name), None)

    def summary_lines(self) -> List[str]:
        """The operating point and each phase's parasitics, for the log and the panel."""
        op, wf = self.operating, self.waveforms
        lines = list(op.notes)
        lines.append(f"Inductor ripple {', '.join(f'{r:.3g}' for r in wf.ripple_a)} A p-p; "
                     f"harmonics to {wf.f_max_hz / 1e6:.4g} MHz ({wf.harmonics})")
        for h in self.hot_loops:
            floor = (f" (least damping; the loop alone gives {h.zeta_loop:.3g})"
                     if h.zeta > h.zeta_loop * 1.0001 else "")
            ring = (f"rings at {h.f_ring_hz / 1e6:.4g} MHz, ζ = {h.zeta:.3g}{floor}"
                    if h.f_ring_hz else "no ringing")
            lines.append(
                f"Phase {h.phase}: hot loop {h.loop_l_h * 1e9:.3g} nH "
                f"(package {h.package_l_h * 1e9:.3g} nH), switch node "
                f"{h.c_sw_f * 1e12:.4g} pF to ground (Coss {h.coss_f * 1e12:.4g} pF"
                f"{' assumed' if h.coss_default else ''} + copper "
                f"{h.c_copper_f * 1e12:.3g} pF), R {h.r_loop_ohm * 1e3:.3g} mΩ; {ring}")
            if 0 < h.phase <= len(wf.phases) and wf.phases[h.phase - 1].edges:
                lines.append(f"Phase {h.phase}: {wf.phases[h.phase - 1].edges}")
        if self.board_note:
            lines.append(self.board_note)
        if self.scan:
            solved = sum(r.solved for r in self.scan)
            lines.append(f"Scan: {len(self.scan)} net(s) near the aggressor estimated, the "
                         f"worst {solved} solved in full")
        for v in self.victims:
            cm = sum(v.cm_f.values())
            m = sum(abs(x) for x in v.m_h.values())
            by = ", ".join(f"{k} {p * 1e3:.3g}" for k, p in v.peak_by.items())
            lines.append(f"Victim {v.name}: {v.peak_v * 1e3:.4g} mV peak at {v.receiver} "
                         f"({v.ratio * 100:.3g} % of its {v.margin_v * 1e3:.4g} mV margin, "
                         f"{'PASS' if v.passed else 'FAIL'}; {by} mV); {cm * 1e15:.4g} fF "
                         f"to the switch node over {v.coupled_mm:.3g} of {v.length_mm:.3g} "
                         f"mm, {m * 1e12:.4g} pH to the capacitor loops")
            for st in v.states:
                lines.append(f"Victim {v.name} held {st.name} ({st.level_v:.3g} V, driver "
                             f"{st.driver_ohm:.4g} Ω): {st.excursion_v * 1e3:.4g} mV toward "
                             f"its {st.threshold_v:.3g} V threshold, {st.ratio * 100:.3g} % "
                             f"of {st.margin_v * 1e3:.4g} mV")
        return lines
