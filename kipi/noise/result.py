"""What a Coupled Noise solve returns (#69)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kipi.noise.checks import OperatingPoint
from kipi.noise.parasitics import HotLoopReport
from kipi.noise.waveforms import SmpsWaveforms, Spectrum

MECHANISMS = ("capacitive", "inductive")
TOTAL = "total"


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
        for v in self.victims:
            cm = sum(v.cm_f.values())
            m = sum(abs(x) for x in v.m_h.values())
            by = ", ".join(f"{k} {p * 1e3:.3g}" for k, p in v.peak_by.items())
            lines.append(f"Victim {v.name}: {v.peak_v * 1e3:.4g} mV peak at {v.receiver} "
                         f"({v.ratio * 100:.3g} % of its {v.margin_v * 1e3:.4g} mV margin, "
                         f"{'PASS' if v.passed else 'FAIL'}; {by} mV); {cm * 1e15:.4g} fF "
                         f"to the switch node over {v.coupled_mm:.3g} of {v.length_mm:.3g} "
                         f"mm, {m * 1e12:.4g} pH to the capacitor loops")
        return lines
