"""What the setup keeps of each load's stack-up (#167): per load, the DC drop, the ripple and
the transient as accepted from a solve or a results file, each with where it came from. The
VRM's DC accuracy is a setup value and is read from the VRM, not kept here.

Every value is how far the load's voltage goes from nominal, in volts and never negative:
``below_v`` under it, ``above_v`` over it. The ripple and the transient also keep the same
seen through a 20 MHz bandwidth limit, as a scope's limit shows them, so the table can show
either without the results that gave them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Dict, Optional

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
        # Six figures (well under a µV here) keep the setup file readable.
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

    def entry(self, load: str) -> LoadEntry:
        """The load's entry, made if it has none yet."""
        return self.entries.setdefault(load, LoadEntry())

    def accept(self, load: str, kind: str, value: Contribution) -> None:
        setattr(self.entry(load), kind, value)

    @classmethod
    def from_dict(cls, d, loads=None) -> "ToleranceTable":
        """From a setup file; entries of loads not in ``loads`` (names), if given, are left
        out, as are empty ones."""
        d = d if isinstance(d, dict) else {}
        entries = {str(k): LoadEntry.from_dict(v) for k, v in (d.get("entries") or {}).items()}
        keep = None if loads is None else set(loads)
        return cls({k: e for k, e in entries.items()
                    if not e.empty() and (keep is None or k in keep)},
                   bool(d.get("bw_limit", False)))
