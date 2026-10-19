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
# The reverse-recovery charge the hard edge sweeps out of the other switch's body diode: a
# low-voltage MOSFET's of about the default Coss's size, after a short dead time. A
# Schottky or GaN low side has none (enter 0).
DEFAULT_QRR_C = 5e-9


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
    qrr_c: Optional[float] = None       # the other switch's body diode's reverse recovery
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
    # AC probes (by name) to show this aggressor's ripple at (#157): the board model's
    # voltage between each probe's rail and ground pins.
    ripple_probes: List[str] = field(default_factory=list)
    # The least damping of the switch node's ringing. The loop's resistance alone (on-
    # resistance, ESR, copper) damps it far less than measured: the switches' output
    # capacitance, reverse recovery and radiation take energy too, and a switch node's
    # ringing usually dies within five to ten cycles (ζ ≈ 0.05-0.1).
    min_ring_zeta: float = 0.05
    # The least damping of a soft edge: the one the inductor current carries across into
    # a conducting body diode's clamp (a buck's falling edge, a boost's rising one).
    # The clamp takes the ringing's energy, so the edge overshoots once and settles.
    soft_edge_zeta: float = 0.5


# JESD8 logic levels: supply volts -> (VIL, VIH, VOL, VOH), typical CMOS families. Off
# the table the levels are 35/65 % and 20/80 % of the supply, a noise margin of 15 %.
LOGIC_LEVELS = {
    5.0: (1.5, 3.5, 0.5, 4.4),          # 5 V CMOS
    3.3: (0.8, 2.0, 0.4, 2.4),          # LVTTL / LVCMOS, JESD8C
    2.5: (0.7, 1.7, 0.4, 2.0),          # JESD8-5
    1.8: (0.63, 1.17, 0.45, 1.35),      # JESD8-7: 0.35/0.65 VDD, VDD - 0.45
    1.5: (0.525, 0.975, 0.375, 1.125),  # JESD8-11: 0.35/0.65, 0.25/0.75 VDD
    1.2: (0.42, 0.78, 0.3, 0.9),        # JESD8-12
}
OTHER_MARGIN = 0.15
OTHER_LEVELS = (0.35, 0.65, 0.2, 0.8)


def logic_levels(volts: float):
    """(VIL, VIH, VOL, VOH) of a logic family on ``volts``."""
    for v, levels in LOGIC_LEVELS.items():
        if abs(v - volts) < 1e-3:
            return levels
    return tuple(x * volts for x in OTHER_LEVELS)


def logic_margin(volts: float) -> float:
    """The noise margin of a logic family on ``volts``: min(VIL − VOL, VOH − VIH)."""
    vil, vih, vol, voh = logic_levels(volts)
    return min(vil - vol, voh - vih)


@dataclass(frozen=True)
class LogicState:
    """The victim held high or low (#156): the driver's level, the receiver's threshold
    that noise must not cross, and the driver's resistance in that state."""
    name: str                       # "high" or "low"
    level_v: float                  # VOH or VOL
    threshold_v: float              # VIH or VIL
    driver_ohm: float

    @property
    def margin_v(self) -> float:
        return abs(self.level_v - self.threshold_v)

    @property
    def toward(self) -> int:
        """The sign of the noise that moves the line toward the threshold."""
        return -1 if self.threshold_v < self.level_v else 1


@dataclass(frozen=True)
class VictimPreset:
    label: str
    driver_ohm: float               # source impedance of the driver (or pull-up)
    receiver_ohm: float
    receiver_pf: float
    termination_ohm: Optional[float]    # parallel termination at the receiver
    margin_v: Optional[float] = None    # None: from the logic levels
    note: str = ""
    driver_low_ohm: Optional[float] = None  # pulling low, where it differs from driver_ohm


VICTIM_PRESETS = {
    "cmos": VictimPreset("CMOS push-pull", 33.0, 1e6, 3.0, None,
                         note="a logic output driving a CMOS input"),
    "open_drain": VictimPreset("Open drain, pulled up", 4700.0, 1e6, 5.0, None,
                               note="released: only the pull-up holds the line",
                               driver_low_ohm=20.0),
    "series": VictimPreset("Series terminated", 50.0, 1e6, 3.0, None,
                           note="driver plus series resistor matched to the trace"),
    "parallel": VictimPreset("Parallel terminated (50 Ω)", 25.0, 1e6, 3.0, 50.0,
                             note="a 50 Ω termination at the receiver"),
    "lvds": VictimPreset("Differential leg (LVDS)", 50.0, 1e6, 3.0, 50.0, 0.1,
                         note="one leg, half of a 100 Ω termination; 100 mV threshold"),
    "analog": VictimPreset("High-impedance analog input", 1000.0, 10e6, 5.0, None, 0.01,
                           note="a 1 kΩ source into an ADC or amplifier input"),
    # Nothing holds the line (#158): the coupled charge divides between the coupling and
    # the line's own capacitance to ground, the worst capacitive coupling a net whose
    # driver is not known can take. (An EMF along it divides by where that capacitance
    # sits; a low-impedance driver puts it all at the receiver.)
    "hiz": VictimPreset("High impedance (undriven)", 1e6, 1e6, 5.0, None,
                        note="tri-stated or floating: only its inputs' leakage holds it"),
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
    driver_ohm: float = 33.0            # driving high (an open drain: the pull-up)
    receiver_ohm: float = 1e6
    receiver_pf: float = 3.0
    termination_ohm: Optional[float] = None
    logic_v: float = 3.3
    noise_margin_v: Optional[float] = None   # None: from the preset or the logic levels
    driver_low_ohm: Optional[float] = None   # driving low; None: driver_ohm

    def __post_init__(self):
        self.name = self.name or self.net

    def _set_margin(self) -> Optional[float]:
        """The margin set by hand or by the type, None where the logic levels give it."""
        if self.noise_margin_v is not None:
            return self.noise_margin_v
        preset = VICTIM_PRESETS.get(self.preset)
        if preset is not None and preset.margin_v is not None:
            return preset.margin_v
        return None

    def margin(self) -> float:
        m = self._set_margin()
        return logic_margin(self.logic_v) if m is None else m

    def states(self) -> List[LogicState]:
        """The line held high and held low: the logic family's output level, the input
        threshold on the far side of the margin (a margin set by hand or by the type
        moves the threshold) and the driver's resistance in that state."""
        vil, vih, vol, voh = logic_levels(self.logic_v)
        m = self._set_margin()
        if m is not None:
            vih, vil = voh - m, vol + m
        low = self.driver_ohm if self.driver_low_ohm is None else self.driver_low_ohm
        return [LogicState("high", voh, vih, self.driver_ohm),
                LogicState("low", vol, vil, low)]

    def apply_preset(self, key: str):
        """Take a preset's terminations (custom keeps the values)."""
        self.preset = key
        p = VICTIM_PRESETS.get(key)
        if p is None:
            return
        self.driver_ohm, self.receiver_ohm = p.driver_ohm, p.receiver_ohm
        self.receiver_pf, self.termination_ohm = p.receiver_pf, p.termination_ohm
        self.driver_low_ohm = p.driver_low_ohm


@dataclass
class NoiseSettings:
    aggressor: str = ""                 # the aggressor last solved
    # Highest harmonic frequency; None: from the edges and the ringing (see
    # kipi.noise.waveforms.harmonic_count), at most 3 GHz.
    f_max_hz: Optional[float] = None
    max_harmonics: int = 4000
    periods_shown: int = 2
    # The controller's dead time: a soft edge the inductor current cannot carry across in
    # it (light load, a valley just below zero) is finished hard by the next switch.
    dead_time_ns: float = 20.0
    # Capacitive coupling (cross-sections of the victim): its traces are cut every
    # segment_mm, and copper within window_mm of the cut counts.
    segment_mm: float = 0.5
    window_mm: float = 3.0
    # The board model (AC mode's field model of the converter's rails and ground): the
    # capacitor current split, the rails' ripple and shared-ground coupling. Off: the
    # capacitors share the current by their own impedance only, and there is no shared-
    # ground coupling.
    board_model: bool = True
    field_per_decade: int = 10
    # Which victims a solve covers: "all", "none" (the waveforms only) or one's name.
    victims: str = "all"
    # Scan the signal nets near the aggressor (kipi.noise.scan): those within scan_radius_mm
    # of its parts are estimated, and the scan_top_n worst solved in full as victims of
    # type scan_preset (a VICTIM_PRESETS key, or "custom" for scan_driver_ohm), by default
    # undriven: their real drivers are not known (#158). A net that is a victim already
    # keeps its own type.
    scan: bool = False
    scan_radius_mm: float = 10.0
    scan_top_n: int = 10
    scan_preset: str = "hiz"
    scan_driver_ohm: float = 33.0
    scan_logic_v: float = 3.3
    html_report: bool = False           # write a report after each solve
    report_format: str = "html"         # "html" or "pdf"


def scan_victim(s: NoiseSettings, net: str, driver: Optional[PinRef] = None,
                receiver: Optional[PinRef] = None) -> Victim:
    """A net the scan found, as the victim it is solved as."""
    v = Victim(net, driver=driver, receiver=receiver, logic_v=s.scan_logic_v)
    if s.scan_preset in VICTIM_PRESETS:
        v.apply_preset(s.scan_preset)
    else:
        v.preset, v.driver_ohm = CUSTOM, s.scan_driver_ohm
    return v


def phase_from_dict(d: dict, pin: Callable[[Optional[dict]], Optional[PinRef]]
                    ) -> AggressorPhase:
    return AggressorPhase(
        inductor=d.get("inductor", ""), inductance_h=float(d.get("inductance_h") or 0.0),
        dcr_ohm=float(d.get("dcr_ohm") or 0.0), switch_net=d.get("switch_net", ""),
        high_side=pin(d.get("high_side")), low_side=pin(d.get("low_side")),
        input_caps=list(d.get("input_caps", [])), output_caps=list(d.get("output_caps", [])),
        coss_f=d.get("coss_f"), qrr_c=d.get("qrr_c"), rds_on_hs_ohm=d.get("rds_on_hs_ohm"),
        rds_on_ls_ohm=d.get("rds_on_ls_ohm"),
        package_l_h=float(d.get("package_l_h") or 0.0))


def aggressor_from_dict(d: dict, pin: Callable[[Optional[dict]], Optional[PinRef]],
                        legacy: Optional[dict] = None) -> Aggressor:
    """``legacy``: the setup's noise settings, whose solve-wide dampings (before they were
    per aggressor) an aggressor saved without its own takes."""
    topo = d.get("topology", "auto")
    legacy = legacy or {}
    return Aggressor(
        name=d["name"], vrm=d.get("vrm", ""),
        topology=topo if topo in TOPOLOGIES else "auto",
        vin_v=d.get("vin_v"), fsw_hz=float(d.get("fsw_hz", 500e3)),
        rise_s=float(d.get("rise_s", 2e-9)), fall_s=float(d.get("fall_s", 2e-9)),
        duty=d.get("duty"), load_a=d.get("load_a"),
        phases=[phase_from_dict(p, pin) for p in d.get("phases", [])],
        ripple_probes=[str(x) for x in d.get("ripple_probes", [])],
        min_ring_zeta=float(d.get("min_ring_zeta", legacy.get("min_ring_zeta", 0.05))),
        soft_edge_zeta=float(d.get("soft_edge_zeta", legacy.get("soft_edge_zeta", 0.5))))


def victim_from_dict(d: dict, pin: Callable[[Optional[dict]], Optional[PinRef]]) -> Victim:
    preset = d.get("preset", "cmos")
    return Victim(
        net=d["net"], name=d.get("name", ""), preset=preset if preset in VICTIM_PRESETS or preset == CUSTOM
        else CUSTOM, driver=pin(d.get("driver")), receiver=pin(d.get("receiver")),
        driver_ohm=float(d.get("driver_ohm", 33.0)),
        receiver_ohm=float(d.get("receiver_ohm", 1e6)),
        receiver_pf=float(d.get("receiver_pf", 3.0)),
        termination_ohm=d.get("termination_ohm"), logic_v=float(d.get("logic_v", 3.3)),
        noise_margin_v=d.get("noise_margin_v"), driver_low_ohm=d.get("driver_low_ohm"))


def settings_from_dict(d: dict) -> NoiseSettings:
    known = {f.name for f in dataclasses.fields(NoiseSettings)}
    return NoiseSettings(**{k: v for k, v in (d or {}).items() if k in known})
