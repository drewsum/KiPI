"""The few lines an import says: its notes with repeats folded, and what it changed."""

import copy

from kipi.dc.pi.importers.import_summary import counts, group_notes, setup_changes
from kipi.dc.pi.model import Load, PinRef, PISetup


def test_the_same_note_on_several_parts_is_one_line():
    notes = ["U3 on +5V: draws what it drives", "U11: on-resistance not known",
             "U4 on +5V: draws what it drives", "U11: on-resistance not known",
             "U8: on-resistance not known",
             "Current not known, imported as 0 A; enter it on the Loads tab: U3, U4"]
    assert group_notes(notes) == [
        "U3 on +5V, U4 on +5V: draws what it drives",
        "U11, U8: on-resistance not known",
        "Current not known, imported as 0 A; enter it on the Loads tab: U3, U4"]


def _setup(*loads):
    s = PISetup()
    s.loads = [Load(name=n, supply=PinRef("U1", "VCC"), current=a) for n, a in loads]
    return s


def test_what_the_import_changed():
    old = _setup(("U1", 1.0), ("U2", 0.5))
    assert setup_changes(old, copy.deepcopy(old)) == "the same as before"
    assert setup_changes(old, _setup(("U1", 2.0), ("U3", 0.1))) == \
        "1 load added, 1 load removed, 1 load changed"
    assert setup_changes(PISetup(), old) == "a new power tree"
    assert counts(old) == "0 VRMs, 2 loads, 0 DC connections"
