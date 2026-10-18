"""Tables sort by a clicked column header, alternating ascending and descending (#85):
results tables, the setup tables and the HTML reports' tables."""

import json
import os
import shutil
import subprocess

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QPoint, Qt  # noqa: E402
from qtpy.QtWidgets import QApplication, QTableWidget  # noqa: E402

from kipi.gui.sortable import make_sortable, set_row, sort_key  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def column(t, col):
    return [t.item(r, col).text() for r in range(t.rowCount())]


def mouse_click(t, col):
    """A real click on the header, which also flips Qt's own sort indicator."""
    from qtpy.QtTest import QTest
    h = t.horizontalHeader()
    QTest.mouseClick(h.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(h.sectionViewportPosition(col) + 5, 5))


def test_mouse_clicks_alternate_the_direction(app):
    t = QTableWidget(3, 1)
    for r, v in enumerate(["2", "1", "3"]):
        set_row(t, r, [v])
    make_sortable(t)
    t.resize(200, 200)
    t.show()
    orders = []
    for _ in range(3):
        mouse_click(t, 0)
        orders.append(column(t, 0))
    assert orders == [["1", "2", "3"], ["3", "2", "1"], ["1", "2", "3"]]
    t.close()


def test_keys_take_si_prefixes_and_natural_text():
    values = ["100nF", "10 µF", "4u7", "1n", "", "2.2uF"]
    assert sorted((v for v in values if v), key=sort_key) == \
        ["1n", "100nF", "2.2uF", "4u7", "10 µF"]
    assert sorted(["C10", "C9", "c1", "R2"], key=sort_key) == ["c1", "C9", "C10", "R2"]
    assert sort_key("1.5 kHz") < sort_key("900 kHz") < sort_key("1 MHz")
    assert sort_key("") > sort_key("zzz") > sort_key("1e9")     # numbers, text, blanks
    assert sort_key("15,28") == (0, 15.0)                        # pin lists by first pin


# ---------------------------------------------------------------- setup tables

def _cap_board():
    from kipi.core.model.board import Footprint, Pad
    from synthetic import make_board
    snap = make_board()
    for k, (ref, value) in enumerate((("C10", "100nF"), ("C2", "10uF"), ("C9", "1uF"))):
        snap.pads += [Pad(f"{ref}a", "+3V3", ref, "1", (k, 0.0), "smd"),
                      Pad(f"{ref}b", "GND", ref, "2", (k, 1.0), "smd")]
        snap.footprints.append(Footprint(f"f{ref}", ref, value, (k, 0.5), "top",
                                         [f"{ref}a", f"{ref}b"]))
    return snap


def test_capacitor_parts_sort_and_keep_their_order_when_ticked(app):
    from kipi.ac.gui.panel import ACPanel
    from kipi.dc.pi.model import PISetup
    setup = PISetup()
    panel = ACPanel()
    panel.set_board(_cap_board(), setup)
    panel.set_nets(["+3V3", "GND"])
    panel.cap_list._add()
    t, h = panel.cap_list.editor.table, panel.cap_list.editor.table.horizontalHeader()
    assert column(t, 1) == ["C2", "C9", "C10"]
    h.sectionClicked.emit(2)                                # value: 100n < 1µ < 10µ
    assert column(t, 2) == ["100nF", "1uF", "10uF"]
    h.sectionClicked.emit(2)
    assert column(t, 2) == ["10uF", "1uF", "100nF"]
    t.item(1, 0).setCheckState(Qt.Checked)                  # tick C9: the table refills ...
    assert setup.caps[0].refs == ["C9"]
    assert column(t, 2) == ["10uF", "1uF", "100nF"]         # ... in the same order
    h.sectionClicked.emit(0)                                # ticked first
    assert column(t, 1)[0] == "C9"
    h.sectionClicked.emit(1)
    assert column(t, 1) == ["C2", "C9", "C10"]
    panel.close()


def test_target_points_sort_by_frequency_or_impedance(app):
    from kipi.ac.gui.panel import ACPanel
    from kipi.ac.model import Probe
    from kipi.dc.pi.model import PinRef
    panel = ACPanel()
    probe = Probe("U1", PinRef("U1", "+1V0"), PinRef("U1", "GND"), target_custom=True,
                  target_points=[(1e4, 0.01), (1e5, 0.03), (1e6, 0.02)])
    panel.setup.probes.append(probe)
    panel.setEnabled(True)
    panel.probe_list.reload()
    panel.probe_list.lst.setCurrentRow(0)
    ed = panel.probe_list.editor
    h = ed.points.horizontalHeader()
    h.sectionClicked.emit(1)                                # |Z| ascending
    assert probe.target_points == pytest.approx([(1e4, 0.01), (1e6, 0.02), (1e5, 0.03)])
    h.sectionClicked.emit(0)                                # frequency, then reversed
    h.sectionClicked.emit(0)
    assert probe.target_points == pytest.approx([(1e6, 0.02), (1e5, 0.03), (1e4, 0.01)])
    assert ed.points.cellWidget(0, 0).value() == pytest.approx(1e3)      # kHz
    h.sectionClicked.emit(2)                                # the remove buttons don't sort
    assert probe.target_points[0] == pytest.approx((1e6, 0.02))
    assert h.sortIndicatorSection() == 0
    panel.close()


def test_load_pin_groups_sort(app):
    from kipi.dc.gui.pi_panel import PIPanel
    from kipi.dc.pi.model import Load, PinGroup, PinRef, PISetup
    from phase_boards import board
    setup = PISetup()
    setup.loads.append(Load("U9", PinRef("U9", "VOUT"), 0.0, groups=[
        PinGroup(["12"], 0.2, "io"), PinGroup(["3"], 1.5, "core"), PinGroup(["7"], 0.5, "")]))
    panel = PIPanel()
    panel.set_board(board(), setup)
    panel.load_list.lst.setCurrentRow(0)
    h = panel.load_list.editor.groups.horizontalHeader()
    names = lambda: [g.name for g in setup.loads[0].groups]      # noqa: E731
    h.sectionClicked.emit(0)
    assert names() == ["core", "io", ""]                    # a blank name goes last
    h.sectionClicked.emit(0)
    assert names() == ["io", "core", ""]
    h.sectionClicked.emit(1)                                # pins by number: 3, 7, 12
    assert [g.pins for g in setup.loads[0].groups] == [["3"], ["7"], ["12"]]
    h.sectionClicked.emit(2)
    h.sectionClicked.emit(2)
    assert [g.current for g in setup.loads[0].groups] == [1.5, 0.5, 0.2]
    panel.close()


# ---------------------------------------------------------------- HTML reports

_DOM = """
const handlers = [];
global.document = {addEventListener: (type, f) => handlers.push(f)};
function cell(text) { return {textContent: text}; }
const rows = %(rows)s.map(r => ({cells: r.map(cell)}));
const body = {rows: rows, appendChild(r) { rows.splice(rows.indexOf(r), 1); rows.push(r); }};
const ths = %(heads)s.map((h, i) => {
  const attrs = {};
  return {cellIndex: i, getAttribute: k => attrs[k] === undefined ? null : attrs[k],
          setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: k => { delete attrs[k]; },
          closest: sel => sel === "table" ? table : th(i)};
});
function th(i) { return ths[i]; }
const table = {tBodies: [body], querySelectorAll: () => ths};
%(js)s
const out = [];
for (const col of %(clicks)s) {
  handlers[0]({target: ths[col]});
  out.push(rows.map(r => r.cells[col].textContent));
}
console.log(JSON.stringify(out));
"""


def test_report_tables_sort_in_the_browser():
    from kipi.dc.report.html import SORT_JS
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    rows = [["C10", "100nF", ""], ["C9", "10 µF", "2"], ["C2", "", "1.5"], ["R1", "1n", "-3"]]
    script = _DOM % dict(rows=json.dumps(rows), heads=json.dumps(["Ref", "C", "x"]),
                         js=SORT_JS, clicks=json.dumps([0, 0, 1, 1, 2]))
    run = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True,
                         encoding="utf-8", stdin=subprocess.DEVNULL)
    out = json.loads(run.stdout)
    assert out == [["C2", "C9", "C10", "R1"], ["R1", "C10", "C9", "C2"],
                   ["1n", "100nF", "10 µF", ""], ["10 µF", "100nF", "1n", ""],
                   ["-3", "1.5", "2", ""]]
