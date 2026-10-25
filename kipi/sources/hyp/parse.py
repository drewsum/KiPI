"""The records of a HyperLynx ``.hyp`` file.

A ``.hyp`` file is a list of ``{BLOCK ...}`` records holding ``(ITEM ...)`` records, each a
kind and its ``KEY=value`` fields and plain values:

    {NET="+3.3V"
      (PIN X=1.2 Y=-0.8 R="U1.3" P=7)
      {POLYGON T=POUR L="F.Cu" ID=1 X=1.0 Y=-1.0
        (LINE X=1.0 Y=-1.0)
      }
    }

Text outside a record is a comment. Values may be quoted ("Net-(D1-A)").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

# A block's opening and its header, a block's end, or a whole item.
_RECORD = re.compile(r'\{((?:"[^"]*"|[^(){}"])*)|\}|\(((?:"[^"]*"|[^()"])*)\)')
# A quoted value, KEY=value, or a plain value.
_FIELD = re.compile(r'"([^"]*)"|([^\s,="]+)\s*=\s*(?:"([^"]*)"|([^\s,"]*))|([^\s,"]+)')


class HypError(ValueError):
    """A file KiPI cannot read as a HyperLynx board."""


@dataclass
class Record:
    kind: str                               # "NET", "PIN", "?"; "" for a padstack row
    value: str = ""                         # a block's own value: {NET="GND"} is "GND"
    words: List[str] = field(default_factory=list)    # its plain values, in order
    fields: Dict[str, str] = field(default_factory=dict)   # its KEY=value, keys upper case
    items: List["Record"] = field(default_factory=list)    # the records a block holds

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key, default)

    def num(self, key: str, default: Optional[float] = None) -> Optional[float]:
        try:
            return float(self.fields[key])
        except (KeyError, ValueError):
            return default

    def all(self, kind: str) -> Iterator["Record"]:
        return (r for r in self.items if r.kind == kind)


def _record(text: str) -> Record:
    rec = Record("")
    first = True
    for m in _FIELD.finditer(text):
        quoted, key, kq, kv, plain = m.groups()
        if key is not None:
            key = key.upper()
            val = kq if kq is not None else kv
            rec.fields[key] = val
            if first:
                rec.kind, rec.value = key, val
        else:
            word = quoted if quoted is not None else plain
            rec.words.append(word)
            if first and quoted is None:
                rec.kind = word.upper()
        first = False
    return rec


def _line(text: str) -> Optional[Record]:
    """A ``LINE X=.. Y=..`` item, which most of a file is, without the general parser."""
    parts = text.split()
    if len(parts) == 3 and parts[1][:2] == "X=" and parts[2][:2] == "Y=":
        return Record("LINE", fields={"X": parts[1][2:], "Y": parts[2][2:]})
    return None


def parse(text: str) -> List[Record]:
    """The file's top-level records."""
    top: List[Record] = []
    stack: List[Record] = []
    for m in _RECORD.finditer(text):
        header, item = m.group(1), m.group(2)
        if item is not None:
            rec = (_line(item) if item.startswith("LINE ") else None) or _record(item)
            (stack[-1].items if stack else top).append(rec)
        elif header is not None:
            stack.append(_record(header))
        elif stack:                          # a block's end
            rec = stack.pop()
            (stack[-1].items if stack else top).append(rec)
    if stack:
        raise HypError(f"the file ends inside a {{{stack[-1].kind} block (is it cut short?)")
    return top
