"""What a tolerance file keeps of each load's stack-up (#167): per load, the DC drop, the
ripple and the transient as accepted from a solve or a results file, each with where it came
from, and which loads the table shows. The VRM's DC accuracy is a setup value and is read
from the VRM, not kept here.

Every value is how far the load's voltage goes from nominal, in volts and never negative:
``below_v`` under it, ``above_v`` over it. The ripple and the transient also keep the same
seen through a 20 MHz bandwidth limit, as a scope's limit shows them, so the table can show
either without the results that gave them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional

KINDS = ("dc", "ripple", "transient")
#: What each kind is called in tables and the report.
KIND_NAMES = {"dc": "DC drop", "ripple": "Ripple", "transient": "Transient"}


@dataclass
class Contribution:
    below_v: float = 0.0
    above_v: float = 0.0
    below_bw_v: Optional[float] = None      # through a 20 MHz limit; None for DC
    above_bw_v: Optional[float] = None
    source: str = ""                        # "DC solve", or the results file's name
    when: str = ""                          # when it was solved or saved
    detail: str = ""                        # e.g. "probe U5 on VCORE"

    def __post_init__(self):
        # Six figures (well under a µV here) keep the file readable.
        for name in ("below_v", "above_v", "below_bw_v", "above_bw_v"):
            x = getattr(self, name)
            if x is not None:
                setattr(self, name, float(f"{float(x):.6g}"))

    def sides(self, bw: bool = False):
        """(below, above) volts, through the bandwidth limit if ``bw`` and it has one."""
        if bw and self.below_bw_v is not None and self.above_bw_v is not None:
            return self.below_bw_v, self.above_bw_v
        return self.below_v, self.above_v

    def same_values(self, other: Optional["Contribution"], tol: float = 1e-9) -> bool:
        """Whether ``other`` holds the same voltages (to ``tol`` V), wherever it came from."""
        if other is None:
            return False
        for a, b in ((self.below_v, other.below_v), (self.above_v, other.above_v),
                     (self.below_bw_v, other.below_bw_v), (self.above_bw_v, other.above_bw_v)):
            if (a is None) != (b is None) or (a is not None and abs(a - b) > tol):
                return False
        return True

    @classmethod
    def from_dict(cls, d) -> Optional["Contribution"]:
        if not isinstance(d, dict):
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class LoadEntry:
    dc: Optional[Contribution] = None
    ripple: Optional[Contribution] = None
    transient: Optional[Contribution] = None

    def get(self, kind: str) -> Optional[Contribution]:
        return getattr(self, kind)

    def empty(self) -> bool:
        return all(self.get(k) is None for k in KINDS)

    @classmethod
    def from_dict(cls, d) -> "LoadEntry":
        d = d if isinstance(d, dict) else {}
        return cls(**{k: Contribution.from_dict(d.get(k)) for k in KINDS})


@dataclass
class ToleranceTable:
    entries: Dict[str, LoadEntry] = field(default_factory=dict)    # by load name
    bw_limit: bool = False          # show the ripple and transient through a 20 MHz limit
    loads: List[str] = field(default_factory=list)      # the loads the table shows, by name

    def entry(self, load: str) -> LoadEntry:
        """The load's entry, made if it has none yet."""
        return self.entries.setdefault(load, LoadEntry())

    def accept(self, load: str, kind: str, value: Contribution) -> None:
        setattr(self.entry(load), kind, value)

    # ------------------------------------------------------------------ file
    def to_dict(self) -> dict:
        d = asdict(self)
        d["entries"] = {k: v for k, v in d["entries"].items()
                        if not self.entries[k].empty()}
        d["format_version"] = FORMAT_VERSION
        return d

    @classmethod
    def from_dict(cls, d) -> "ToleranceTable":
        """From a tolerance file; empty entries are left out. Entries of loads the setup
        no longer has are kept: the load may be back, or the file is for another setup."""
        d = d if isinstance(d, dict) else {}
        if d.get("format_version", 1) > FORMAT_VERSION:
            raise ValueError("the tolerance file is from a newer version of KiPI")
        entries = {str(k): LoadEntry.from_dict(v) for k, v in (d.get("entries") or {}).items()}
        return cls({k: e for k, e in entries.items() if not e.empty()},
                   bool(d.get("bw_limit", False)), [str(x) for x in d.get("loads") or []])

    def save(self, path) -> Path:
        """Write the table as YAML (JSON without PyYAML, as setups are); the path written
        is returned."""
        from kipi.core import yaml_io
        path = Path(path)
        if yaml_io.available():
            path = path.with_suffix(".yml") if path.suffix.lower() == ".json" else path
            path.write_text(yaml_io.dumps(self.to_dict()), encoding="utf-8")
        else:
            path = path.with_suffix(".json") if _is_yaml(path) else path
            path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path) -> "ToleranceTable":
        from kipi.core import yaml_io
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        if not _is_yaml(path):
            return cls.from_dict(json.loads(text))
        if not yaml_io.available():
            raise ValueError("reading a .yml file needs PyYAML; reinstall KiPI's dependencies")
        return cls.from_dict(yaml_io.loads(text) or {})


FORMAT_VERSION = 1
SUFFIX = ".kipi-tolerance.yml"
JSON_SUFFIX = ".kipi-tolerance.json"            # where PyYAML is missing
FILE_FILTER = "KiPI tolerance (*.kipi-tolerance.yml *.kipi-tolerance.json)"


def _is_yaml(path: Path) -> bool:
    return path.suffix.lower() in (".yml", ".yaml")


def default_path(board_file_or_name: str, project_dir: str) -> Path:
    """Where a board's tolerance file goes by default: beside its setup,
    ``<project dir>/<board stem>.kipi-tolerance.yml``."""
    from kipi.core import yaml_io
    stem = Path(board_file_or_name).stem
    return Path(project_dir or ".") / f"{stem}{SUFFIX if yaml_io.available() else JSON_SUFFIX}"


def find(board_file_or_name: str, project_dir: str) -> Optional[Path]:
    """The board's tolerance file in its default place, YAML or JSON, else None."""
    stem = Path(board_file_or_name).stem
    for suffix in (SUFFIX, JSON_SUFFIX):
        p = Path(project_dir or ".") / f"{stem}{suffix}"
        if p.is_file():
            return p
    return None
