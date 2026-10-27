"""Read a HyperLynx PI model file (``.hlpimodel``, #149) as a load model.

The file gives a part's DC sink current pin by pin, and names no nets:

.. code-block:: text

    * HL PI Model File
    VERSION=1.0
    15 DC I=0.5 R=1e+06
    28 DC I=0.5 R=1e+06

Only the ``DC`` records are read. ``VRM`` records are skipped, and the notes say so. A
record's ``R`` (HyperLynx's sink resistance) is not modeled, because KiPI's loads draw a
fixed current. A record without ``I`` draws nothing, and the notes name it.

The pins become one supply, split by the net each pin is on when the model is applied to
a part (:func:`kipi.dc.pi.load_model.on_pins`). Pins drawing the same current share a pin
group, so one load on one net gets one group per current, or just a total when every pin
draws the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kipi.core.model.spice import number as spice_number
from kipi.dc.pi.load_model import LoadModel, ModelError, SupplyModel, groups_of

SUFFIX = ".hlpimodel"
MAX_FILE_BYTES = 16 << 20       # a part's pins are kilobytes; this is not one
OPEN_OHMS = 1e6                 # HyperLynx's own "no resistance" value


def is_pimodel(path) -> bool:
    return Path(path).suffix.lower() == SUFFIX


def _value(text: str) -> Optional[float]:
    sign = -1.0 if text.startswith("-") else 1.0
    v = spice_number(text.lstrip("+-"))
    return None if v is None else sign * v


def read_sinks(text: str, name: str = "the file") -> Tuple[Dict[str, float], List[str]]:
    """({pin: amps}, notes) from the text of a ``.hlpimodel``."""
    amps: Dict[str, float] = {}
    notes: List[str] = []
    vrms: List[str] = []
    no_current: List[str] = []
    resistive: List[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("*") or line.upper().startswith("VERSION"):
            continue
        words = line.split()
        if len(words) < 2:
            notes.append(f"Line {n}: not a pin record, skipped ({line[:40]})")
            continue
        pin, kind = words[0], words[1].upper()
        if kind == "VRM":
            vrms.append(pin)
            continue
        if kind != "DC":
            notes.append(f"Line {n}: unknown record {words[1]!r} for pin {pin}, skipped")
            continue
        params: Dict[str, str] = {}
        for w in words[2:]:
            key, sep, val = w.partition("=")
            if sep:
                params[key.upper()] = val
        if pin in amps:
            notes.append(f"Pin {pin}: listed twice; the last record is used")
        current = _value(params["I"]) if "I" in params else None
        if "I" in params and current is None:
            raise ModelError(f"{name}, line {n}: I={params['I']} is not a number")
        if current is None:
            no_current.append(pin)
        r = _value(params["R"]) if "R" in params else None
        if r is not None and 0 < r < OPEN_OHMS:
            resistive.append(pin)
        amps[pin] = current or 0.0
    if vrms:
        notes.append(f"VRM records skipped (KiPI reads only the DC sinks): {', '.join(vrms)}")
    if no_current:
        notes.append("No current given, imported as 0 A: " + ", ".join(no_current))
    if resistive:
        notes.append("Sink resistance R not modeled (KiPI's loads draw a fixed current): "
                     + ", ".join(resistive))
    return amps, notes


def load_pimodel(path) -> LoadModel:
    """The load model of a ``.hlpimodel``: one supply of every DC sink pin, to be split by
    net on the part it is applied to."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ModelError(f"{path.name} is too large to be a HyperLynx PI model")
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ModelError(f"cannot read {path.name}: {e}") from e
    if "HL PI MODEL" not in text[:200].upper() and "VERSION" not in text[:2000].upper():
        raise ModelError(f"{path.name} is not a HyperLynx PI model (no 'HL PI Model File' "
                         "header)")
    amps, notes = read_sinks(text, path.name)
    model = LoadModel(path.name[:-len(SUFFIX)] if is_pimodel(path) else path.stem,
                      notes=notes)
    if amps:
        pins = list(amps)
        model.supplies.append(SupplyModel("DC sink", pins, groups=groups_of(amps),
                                          split_by_net=True))
    return model
