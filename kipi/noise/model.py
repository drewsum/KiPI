"""The Coupled Noise part of the user's setup (#69): aggressors, victims and settings.

These live in the same setup file as the DC power tree (:class:`kipi.dc.pi.model.PISetup`),
which also holds the VRMs and capacitor models this mode uses. An aggressor names a
switching VRM ("smps") and adds what its waveforms need: the switching frequency and
edges, and each phase's inductor, switching device pins and input and output capacitors.
A victim is a signal net, with the driver and receiver that terminate it and the logic
levels that set its noise margin.
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


# JESD8 logic levels: supply volts -> (VIL, VIH, VOL, VOH), typical CMOS families. Off
# the table the noise margin is 15 % of the supply.
LOGIC_LEVELS = {
    5.0: (1.5, 3.5, 0.5, 4.4),          # 5 V CMOS
    3.3: (0.8, 2.0, 0.4, 2.4),          # LVTTL / LVCMOS, JESD8C
    2.5: (0.7, 1.7, 0.4, 2.0),          # JESD8-5
    1.8: (0.63, 1.17, 0.45, 1.35),      # JESD8-7: 0.35/0.65 VDD, VDD - 0.45
    1.5: (0.525, 0.975, 0.375, 1.125),  # JESD8-11: 0.35/0.65, 0.25/0.75 VDD
    1.2: (0.42, 0.78, 0.3, 0.9),        # JESD8-12
}
OTHER_MARGIN = 0.15


def logic_margin(volts: float) -> float:
    """The noise margin of a logic family on ``volts``: min(VIL − VOL, VOH − VIH)."""
    for v, (vil, vih, vol, voh) in LOGIC_LEVELS.items():
        if abs(v - volts) < 1e-3:
            return min(vil - vol, voh - vih)
    return OTHER_MARGIN * volts


@dataclass(frozen=True)
class VictimPreset:
    label: str
    driver_ohm: float               # source impedance of the driver (or pull-up)
    receiver_ohm: float
    receiver_pf: float
    termination_ohm: Optional[float]    # parallel termination at the receiver
    margin_v: Optional[float] = None    # None: from the logic levels
    note: str = ""


VICTIM_PRESETS = {
    "cmos": VictimPreset("CMOS push-pull", 33.0, 1e6, 3.0, None,
                         note="a logic output driving a CMOS input"),
    "open_drain": VictimPreset("Open drain, pulled up", 4700.0, 1e6, 5.0, None,
                               note="released: only the pull-up holds the line"),
    "series": VictimPreset("Series terminated", 50.0, 1e6, 3.0, None,
                           note="driver plus series resistor matched to the trace"),
    "parallel": VictimPreset("Parallel terminated (50 Ω)", 25.0, 1e6, 3.0, 50.0,
                             note="a 50 Ω termination at the receiver"),
    "lvds": VictimPreset("Differential leg (LVDS)", 50.0, 1e6, 3.0, 50.0, 0.1,
                         note="one leg, half of a 100 Ω termination; 100 mV threshold"),
    "analog": VictimPreset("High-impedance analog input", 1000.0, 10e6, 5.0, None, 0.01,
                           note="a 1 kΩ source into an ADC or amplifier input"),
}
CUSTOM = "custom"


@dataclass
class Victim:
    """A signal net the aggressor's noise couples onto, terminated by its driver and
    receiver."""
    net: str
    name: str = ""                  # "" is the net's name
    preset: str = "cmos"            # a VICTIM_PRESETS key, or "custom"
    driver: Optional[PinRef] = None     # None: one end of the net, guessed
    receiver: Optional[PinRef] = None
    driver_ohm: float = 33.0
    receiver_ohm: float = 1e6
    receiver_pf: float = 3.0
    termination_ohm: Optional[float] = None
    logic_v: float = 3.3
    noise_margin_v: Optional[float] = None   # None: from the preset or the logic levels

    def __post_init__(self):
        self.name = self.name or self.net

    def margin(self) -> float:
        if self.noise_margin_v is not None:
            return self.noise_margin_v
        preset = VICTIM_PRESETS.get(self.preset)
        if preset is not None and preset.margin_v is not None:
            return preset.margin_v
        return logic_margin(self.logic_v)

    def apply_preset(self, key: str):
        """Take a preset's terminations (custom keeps the values)."""
        self.preset = key
        p = VICTIM_PRESETS.get(key)
        if p is None:
            return
        self.driver_ohm, self.receiver_ohm = p.driver_ohm, p.receiver_ohm
        self.receiver_pf, self.termination_ohm = p.receiver_pf, p.termination_ohm


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
    # Capacitive coupling (cross-sections of the victim): its traces are cut every
    # segment_mm, and copper within window_mm of the cut counts.
    segment_mm: float = 0.5
    window_mm: float = 3.0


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


def victim_from_dict(d: dict, pin: Callable[[Optional[dict]], Optional[PinRef]]) -> Victim:
    preset = d.get("preset", "cmos")
    return Victim(
        net=d["net"], name=d.get("name", ""), preset=preset if preset in VICTIM_PRESETS or preset == CUSTOM
        else CUSTOM, driver=pin(d.get("driver")), receiver=pin(d.get("receiver")),
        driver_ohm=float(d.get("driver_ohm", 33.0)),
        receiver_ohm=float(d.get("receiver_ohm", 1e6)),
        receiver_pf=float(d.get("receiver_pf", 3.0)),
        termination_ohm=d.get("termination_ohm"), logic_v=float(d.get("logic_v", 3.3)),
        noise_margin_v=d.get("noise_margin_v"))


def settings_from_dict(d: dict) -> NoiseSettings:
    known = {f.name for f in dataclasses.fields(NoiseSettings)}
    return NoiseSettings(**{k: v for k, v in (d or {}).items() if k in known})
