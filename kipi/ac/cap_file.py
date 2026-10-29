"""Capacitor model files (#229): one :class:`~kipi.ac.model.CapModel` in a YAML file named
after it, to share a part's model between boards and setups.

The file holds everything KiPI knows about the part: its capacitance, ESL and ESR, DC bias
rating and the capacitance kept at it, dielectric and aging rate, manufacturing tolerance,
and its SPICE model (the subcircuit's text and the values fitted to it). Which footprints
use it is the board's business, not the part's, so the references are left out; an
imported model starts with none.

Without PyYAML (an environment set up before KiPI needed it) the file is written as JSON,
which YAML reads too, and read back as JSON.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from kipi import __version__
from kipi.ac.model import CapModel
from kipi.core import yaml_io

SUFFIX = ".yml"
KIND = "kipi-capacitor-model"
FORMAT = 1


def file_name(m: CapModel) -> str:
    """The model's file name: its name, with what a file name cannot hold replaced."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", m.name).strip(" ._") or "capacitor"
    return stem + SUFFIX


def to_dict(m: CapModel) -> dict:
    """The model as plain data, without its references or solve-time temperature."""
    d = dataclasses.asdict(m)
    for k in ("refs", "temp_c"):
        d.pop(k)
    return {"kind": KIND, "format": FORMAT, "kipi": __version__, **d}


def from_dict(d: dict) -> CapModel:
    """A model from :func:`to_dict`'s data (no references)."""
    if not isinstance(d, dict) or d.get("kind") != KIND:
        raise ValueError("Not a KiPI capacitor model file")
    if int(d.get("format", 1)) > FORMAT:
        raise ValueError("This capacitor model is from a newer KiPI")
    from kipi.dc.pi.model import cap_from_dict
    return cap_from_dict({**d, "refs": []})


def write(path, m: CapModel) -> Path:
    path = Path(path)
    data = to_dict(m)
    text = (yaml_io.dumps(data) if yaml_io.available()
            else json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    path.write_text(text, encoding="utf-8")
    return path


def read(path) -> CapModel:
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = yaml_io.loads(text) if yaml_io.available() else json.loads(text)
    except Exception as e:             # noqa: BLE001 — any parse error: not a model file
        raise ValueError(f"Not a KiPI capacitor model file ({e})") from None
    return from_dict(data)
