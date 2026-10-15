"""Assigning capacitors to a model: the parts table shows each part's footprint, and parts
can be ticked by value, footprint or both (#64)."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.core.kicad_io.snapshot import from_dict, to_dict  # noqa: E402

C0402 = "Capacitor_SMD:C_0402_1005Metric"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
PARTS = (("C1", "100nF", C0402), ("C2", "100nF", C0603), ("C3", "1uF", C0402),
         ("C4", "100nF", C0402), ("C5", "10uF", ""))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _board():
    from kipi.core.model.board import Footprint, Pad
    from synthetic import make_board
    snap = make_board()
    for k, (ref, value, fp) in enumerate(PARTS):
        snap.pads += [Pad(f"{ref}a", "+3V3", ref, "1", (k, 0.0), "smd"),
                      Pad(f"{ref}b", "GND", ref, "2", (k, 1.0), "smd")]
        snap.footprints.append(Footprint(f"f{ref}", ref, value, (k, 0.5), "top",
                                         [f"{ref}a", f"{ref}b"], footprint=fp))
    return snap


def test_footprint_round_trips_and_old_snapshots_load():
    snap = _board()
    d = to_dict(snap)
    assert from_dict(d).footprints[-2].footprint == C0402
    for f in d["footprints"]:
        del f["footprint"]                                  # saved before 0.7.2
    assert {f.footprint for f in from_dict(d).footprints} == {""}


@pytest.fixture
def editor(app):
    from kipi.ac.gui.panel import ACPanel
    from kipi.dc.pi.model import PISetup
    setup = PISetup()
    panel = ACPanel()
    panel.set_board(_board(), setup, "test")
    panel.set_nets(["+3V3", "GND"])
    panel.cap_list._add()
    yield setup, panel.cap_list.editor
    panel.close()


def _row(ed, ref):
    t = ed.table
    return next(r for r in range(t.rowCount()) if t.item(r, 1).text() == ref)


def test_table_shows_the_footprint(editor):
    _, ed = editor
    t = ed.table
    assert t.horizontalHeaderItem(ed.C_FOOTPRINT).text() == "Footprint"
    it = t.item(_row(ed, "C2"), ed.C_FOOTPRINT)
    assert it.text() == "C_0603_1608Metric" and it.toolTip() == C0603
    assert t.item(_row(ed, "C5"), ed.C_FOOTPRINT).text() == ""
    ed.filter.setText("0603")
    assert [t.item(r, 1).text() for r in range(t.rowCount()) if not t.isRowHidden(r)] == ["C2"]


@pytest.mark.parametrize("label, refs", [
    ("Tick same value", ["C1", "C2", "C4"]),
    ("Tick same footprint", ["C1", "C3", "C4"]),
    ("Tick same value + footprint", ["C1", "C4"]),
])
def test_tick_same(editor, label, refs):
    from qtpy.QtWidgets import QPushButton
    setup, ed = editor
    ed.table.setCurrentCell(_row(ed, "C1"), 1)
    next(b for b in ed.findChildren(QPushButton) if b.text() == label).click()
    assert setup.caps[0].refs == refs
    for r in range(ed.table.rowCount()):
        ticked = ed.table.item(r, 0).checkState() == Qt.Checked
        assert ticked == (ed.table.item(r, 1).text() in refs)
