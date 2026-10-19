# Copyright (c) 2026 Tomachie LLC.
"""What an import did, in a few lines: the notes with repeats folded together, and how
the imported power tree differs from the one it replaced."""

from __future__ import annotations

from typing import Dict, List, Optional

from kipi.dc.pi.model import PISetup

_KINDS = (("vrms", "VRM"), ("loads", "load"), ("links", "DC connection"))
_MAX_SUBJECT = 40     # a "U3 on +5V:" subject, not a sentence that happens to hold a colon


def group_notes(notes: List[str]) -> List[str]:
    """One line per distinct note: notes that say the same thing about different parts
    ("U3 on +5V: draws what it drives", "U4 on +5V: draws what it drives") become
    "U3 on +5V, U4 on +5V: draws what it drives". Order of first appearance is kept."""
    subjects: Dict[str, List[str]] = {}
    for note in notes:
        subject, sep, said = note.partition(": ")
        if not sep or len(subject) > _MAX_SUBJECT:
            subject, said = "", note
        who = subjects.setdefault(said, [])
        if subject and subject not in who:
            who.append(subject)
    return [f"{', '.join(who)}: {said}" if who else said for said, who in subjects.items()]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def counts(setup: PISetup) -> str:
    return ", ".join(_plural(len(getattr(setup, kind)), word) for kind, word in _KINDS)


def setup_changes(old: Optional[PISetup], new: PISetup) -> str:
    """How `new`'s power tree differs from `old`'s, by name: "the same as before", or
    e.g. "2 loads added, 1 DC connection changed"."""
    if old is None or not (old.vrms or old.loads or old.links):
        return "a new power tree"
    said = []
    for kind, word in _KINDS:
        a = {x.name: x for x in getattr(old, kind)}
        b = {x.name: x for x in getattr(new, kind)}
        for n, how in ((len(b.keys() - a.keys()), "added"), (len(a.keys() - b.keys()), "removed"),
                       (sum(a[k] != b[k] for k in a.keys() & b.keys()), "changed")):
            if n:
                said.append(f"{_plural(n, word)} {how}")
    return ", ".join(said) if said else "the same as before"
