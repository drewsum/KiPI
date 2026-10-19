"""The Coupled Noise part of the user's setup (#69): aggressors and settings.

These live in the same setup file as the DC power tree (:class:`kipi.dc.pi.model.PISetup`),
which also holds the VRMs and capacitor models this mode uses. An aggressor names a
switching VRM ("smps") and adds what its waveforms need: the switching frequency and
edges, and each phase's inductor, switching device pins and input and output capacitors.
Nothing here imports the DC model, so the setup can import this module.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, Optional

if TYPE_CHECKING:                       # PinRef lives in the setup model, which imports us
    from kipi.dc.pi.model import PinRef

TOPOLOGIES = ("auto", "buck", "boost")

# Used where a phase leaves them blank; the ringing they give is a typical one.
DEFAULT_COSS_F = 200e-12                # switch-node capacitance of the switching device
DEFAULT_RDS_ON_OHM = 0.01


@dataclass
class AggressorPhase:
    """The parts of one phase of a switcher.

    The high-side pins are the switching device's pins on the rail its hot loop is fed
    from: VIN for a buck, the output (synchronous rectifier's drain) for a boost. The
    low-side pins are its power ground pins. A discrete design gives the two switches
    different references; an integrated one gives both the same.
    """
    inductor: str = ""                  # reference of the power inductor
    inductance_h: float = 0.0           # required (> 0) before solving
    dcr_ohm: float = 0.0
    switch_net: str = ""                # the switch node
    high_side: Optional[PinRef] = None
    low_side: Optional[PinRef] = None
    input_caps: List[str] = field(default_factory=list)    # references
    output_caps: List[str] = field(default_factory=list)
    # Optional device data (datasheet); blank uses the defaults above, with a warning.
    coss_f: Optional[float] = None      # device capacitance at the switch node
    rds_on_hs_ohm: Optional[float] = None
    rds_on_ls_ohm: Optional[float] = None
    package_l_h: float = 0.0            # loop inductance inside the device's package


@dataclass
class Aggressor:
    """A switching VRM as a noise source. Input and output voltage, switching frequency
    and edges are shared by every phase; phases are interleaved 360°/N."""
    name: str
    vrm: str = ""                       # Vrm.name; must be an "smps"
    topology: str = "auto"              # "auto" (from the VRM's voltages), "buck", "boost"
    vin_v: Optional[float] = None       # None: from the VRM's input rail
    fsw_hz: float = 500e3
    rise_s: float = 2e-9                # switch-node rising edge, 0-100 %
    fall_s: float = 2e-9
    duty: Optional[float] = None        # None: from the voltages and losses
    load_a: Optional[float] = None      # None: the DC setup's loads on the output rail
    phases: List[AggressorPhase] = field(default_factory=list)


@dataclass
class NoiseSettings:
    aggressor: str = ""                 # the aggressor last solved
    # Highest harmonic frequency; None: from the edges and the ringing (see
    # kipi.noise.waveforms.harmonic_count), at most 3 GHz.
    f_max_hz: Optional[float] = None
    max_harmonics: int = 4000
    periods_shown: int = 2
    # The least damping of the switch node's ringing. The loop's resistance alone (on-
    # resistance, ESR, copper) damps it far less than measured: the switches' output
    # capacitance, reverse recovery and radiation take energy too, and a switch node's
    # ringing usually dies within five to ten cycles (ζ ≈ 0.05-0.1).
    min_ring_zeta: float = 0.05


def phase_from_dict(d: dict, pin: Callable[[Optional[dict]], Optional[PinRef]]
                    ) -> AggressorPhase:
    return AggressorPhase(
        inductor=d.get("inductor", ""), inductance_h=float(d.get("inductance_h") or 0.0),
        dcr_ohm=float(d.get("dcr_ohm") or 0.0), switch_net=d.get("switch_net", ""),
        high_side=pin(d.get("high_side")), low_side=pin(d.get("low_side")),
        input_caps=list(d.get("input_caps", [])), output_caps=list(d.get("output_caps", [])),
        coss_f=d.get("coss_f"), rds_on_hs_ohm=d.get("rds_on_hs_ohm"),
        rds_on_ls_ohm=d.get("rds_on_ls_ohm"),
        package_l_h=float(d.get("package_l_h") or 0.0))


def aggressor_from_dict(d: dict, pin: Callable[[Optional[dict]], Optional[PinRef]]
                        ) -> Aggressor:
    topo = d.get("topology", "auto")
    return Aggressor(
        name=d["name"], vrm=d.get("vrm", ""),
        topology=topo if topo in TOPOLOGIES else "auto",
        vin_v=d.get("vin_v"), fsw_hz=float(d.get("fsw_hz", 500e3)),
        rise_s=float(d.get("rise_s", 2e-9)), fall_s=float(d.get("fall_s", 2e-9)),
        duty=d.get("duty"), load_a=d.get("load_a"),
        phases=[phase_from_dict(p, pin) for p in d.get("phases", [])])


def settings_from_dict(d: dict) -> NoiseSettings:
    known = {f.name for f in dataclasses.fields(NoiseSettings)}
    return NoiseSettings(**{k: v for k, v in (d or {}).items() if k in known})
