"""What a Coupled Noise solve returns (#69)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from kipi.noise.checks import OperatingPoint
from kipi.noise.parasitics import HotLoopReport
from kipi.noise.waveforms import SmpsWaveforms


@dataclass
class NoiseResult:
    aggressor: str
    vrm: str
    operating: OperatingPoint
    waveforms: SmpsWaveforms
    hot_loops: List[HotLoopReport] = field(default_factory=list)   # per phase
    warnings: List[str] = field(default_factory=list)
    report_path: str = ""

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
        return lines
