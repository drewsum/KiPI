# Portions Copyright (c) 2026 Tomachie LLC.
"""The setup panel must only change the setup when the user edits it."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.dc.gui.pi_panel import PIPanel  # noqa: E402
from phase_boards import board, two_phase  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_ticking_nets_does_not_edit_the_selected_items(app):
    setup = two_phase(ground=True, sense=True)
    before = setup.to_dict()
    panel = PIPanel()
    panel.set_board(board(), setup)
    for row in (0, 1):                       # the source, then the 2-phase VRM on phase 2
        panel.vrm_list.lst.setCurrentRow(row)
        if row == 1:
            panel.vrm_list.editor.cmb_phase.setCurrentIndex(1)
        for nets in (["VOUT"], ["GND"], ["VIN", "VOUT", "GND"]):
            panel.set_nets(nets)
    panel.load_list.lst.setCurrentRow(0)
    panel.set_nets(["VIN"])
    assert setup.to_dict() == before


def test_vrm_current_limit_edits_and_shows_in_results(app):
    from kipi.dc.pi.tree import solve_tree
    setup = two_phase()
    panel = PIPanel()
    panel.set_board(board(), setup)
    panel.vrm_list.lst.setCurrentRow(1)
    ed = panel.vrm_list.editor
    assert ed.i_limit.value() == 0 and ed.i_limit.text() == "no limit"
    ed.i_limit.setValue(1.5)
    assert setup.vrms[1].current_limit_a == 1.5
    tree = solve_tree(board(), setup)
    panel.show_results(tree, next(iter(tree.groups)))
    t = panel.tbl_vrms
    row = {t.item(r, 0).text(): r for r in range(t.rowCount())}
    assert t.item(row["U1"], 9).text() == "1.5" and t.item(row["U1"], 10).text() == "FAIL"
    assert t.item(row["J1"], 10).text() == ""
    # The rail's lowest voltage (#188), from its target in mV, in % or in volts (#177).
    for unit, shown in (("mV", lambda d, ref: f"{-d * 1e3:+.3f}"),
                        ("%", lambda d, ref: f"{-100 * d / ref:+.3f}"),
                        ("V", lambda d, ref: f"{ref - d:.5f}")):
        panel.cmb_vunit.setCurrentIndex(panel.cmb_vunit.findData(unit))
        assert t.horizontalHeaderItem(8).text() == f"Rail lowest ({unit})"
        for v in tree.vrms:
            drop, _pct = tree.rail_drop(v)
            assert drop > 0
            assert t.item(row[v.name], 8).text() == shown(drop, tree.rail_target(v))
    ed.i_limit.setValue(0)
    assert setup.vrms[1].current_limit_a is None
    panel.close()


def _board_with_bridges():
    from kipi.core.model.board import Footprint, Net, Pad
    from synthetic import make_board
    snap = make_board()
    snap.nets.append(Net(4, "VSNS"))
    for ref, value, x in (("R5", "10m 1%", 4.0), ("L1", "BLM18", 6.0)):
        snap.pads += [Pad(f"{ref}a", "+3V3", ref, "1", (x, 0.0), "smd"),
                      Pad(f"{ref}b", "VSNS", ref, "2", (x, 1.0), "smd")]
        snap.footprints.append(Footprint(f"f{ref}", ref, value, (x, 0.5), "top",
                                         [f"{ref}a", f"{ref}b"]))
    return snap


def test_dc_connection_takes_a_resistors_value(app):
    from kipi.dc.pi.model import PISetup
    setup = PISetup()
    panel = PIPanel()
    panel.set_board(_board_with_bridges(), setup)
    panel.set_nets(["+3V3", "VSNS"])
    panel.link_list._add()
    k = setup.links[0]
    # A new connection uses a part bridging the two nets; a ferrite gets no resistance.
    assert (k.name, k.side_a.ref, k.side_b.ref, k.resistance) == ("L1", "L1", "L1", 0.0)
    ed = panel.link_list.editor

    def pick(ref):
        for picker in (ed.a, ed.b):
            picker.cmb_part.setCurrentIndex(picker.cmb_part.findData(ref))

    pick("R5")
    assert k.side_b.ref == "R5" and k.resistance == pytest.approx(0.01)
    assert ed.r.value() == pytest.approx(10.0)
    # A typed resistance is kept when switching to a non-resistor ...
    ed.r.setValue(25.0)
    pick("L1")
    assert k.resistance == pytest.approx(0.025)
    # ... and picking the resistor again re-reads its value.
    pick("R5")
    assert k.resistance == pytest.approx(0.01) and ed.r.value() == pytest.approx(10.0)
    ed.diode_vf.setValue(0.45)
    assert k.diode_vf == pytest.approx(0.45)
    ed.diode_vf.setValue(0)
    assert k.diode_vf == 0
    panel.close()


def test_solve_tab_excludes_unticked_groups(app):
    from qtpy.QtCore import Qt

    from kipi.dc.pi.model import Load, PinRef
    setup = two_phase(ground=True, sense=True)
    setup.loads.append(Load("U9", PinRef("U9", "+5V"), 0.5))   # no VRM drives +5V
    panel = PIPanel()
    panel.set_board(board(), setup)
    assert not panel.btn_solve.isEnabled()
    lst = panel.lst_groups
    row = next(i for i in range(lst.count()) if lst.item(i).text().startswith("+5V"))
    lst.item(row).setCheckState(Qt.Unchecked)
    assert setup.excluded_nets == ["+5V"]
    assert panel.btn_solve.isEnabled(), panel.lbl_errors.text()
    assert panel.btn_solve.text() == f"Solve {lst.count() - 1} of {lst.count()} net groups"
    panel._tick_all_groups(True)
    assert setup.excluded_nets == [] and panel.btn_solve.text() == "Solve all"


def test_mesh_sense_points_edit_one_at_a_time(app):
    setup = two_phase(ground=True, sense=True)
    panel = PIPanel()
    panel.set_board(board(), setup)
    panel.vrm_list.lst.setCurrentRow(1)
    ed, v = panel.vrm_list.editor, setup.vrms[1]
    first = v.sense
    assert not ed.form.isRowVisible(ed.n_points) and not v.mesh_sense
    ed.mesh.setChecked(True)
    ed.n_points.setValue(2)
    assert v.mesh_sense and len(v.sense_points) == 1
    ed.cmb_point.setCurrentIndex(1)
    assert ed.sense.value() is None                   # point 2 starts empty
    ed.cmb_point.setCurrentIndex(0)
    assert ed.sense.value() == first and v.sense == first
    ed.n_points.setValue(1)
    assert v.sense_points == [] and v.sense == first
    panel.close()
