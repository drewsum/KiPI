"""One part's load split across several of its supply nets (#182)."""

import os

import pytest

from kipi.dc.pi.model import Load, PinRef, PISetup
from kipi.dc.pi.split import PER_NET, existing_load, split_current, supply_nets


def _board():
    """U1 with three pins on +3V3, two on a ferrite bead filtered +3V3_PLL and a ground
    pin."""
    from kipi.core.model.board import Net, Pad
    from synthetic import make_board
    snap = make_board()
    snap.nets.append(Net(4, "+3V3_PLL"))
    for k, (net, num) in enumerate((("+3V3", "4"), ("+3V3", "5"), ("+3V3_PLL", "6"),
                                    ("+3V3_PLL", "7"), ("unconnected-(U1-Pad8)", "8"))):
        snap.pads.append(Pad(f"u{k}", net, "U1", num, (1.0 + k, 0.0), "smd"))
        snap.footprints[0].pad_ids.append(f"u{k}")
    return snap


def test_the_total_is_shared_by_pin_count_or_equally():
    pins = {"A": ["1", "2", "3"], "B": ["4"]}
    assert split_current(pins, 2.0) == {"A": 1.5, "B": 0.5}
    assert split_current(pins, 2.0, PER_NET) == {"A": 1.0, "B": 1.0}
    assert split_current({}, 2.0) == {}


def test_a_parts_supply_nets_leave_out_grounds_and_unconnected_pads():
    snap = _board()
    nets = supply_nets(snap, PISetup(), "U1")
    assert nets == {"+3V3": ["1", "4", "5"], "+3V3_PLL": ["6", "7"]}
    setup = PISetup()
    setup.loads.append(Load("U1", PinRef("U1", "+3V3", ["4"]), 0.1))
    assert supply_nets(snap, setup, "U1")["+3V3"] == ["4"]     # the load's own pins
    assert existing_load(setup, "U1", "+3V3") is setup.loads[0]
    assert existing_load(setup, "U1", "+3V3_PLL") is None


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_panel_splits_a_load_across_the_ticked_nets(app):
    from qtpy.QtCore import Qt
    from kipi.dc.gui.pi_panel import PIPanel
    setup = PISetup()
    setup.loads.append(Load("U1 core", PinRef("U1", "+3V3"), 0.3,
                            ground=PinRef("U1", "GND")))
    panel = PIPanel()
    panel.set_board(_board(), setup)
    panel.set_nets(["+3V3", "+3V3_PLL", "GND"])
    seen = {}

    def run(dlg):
        t = dlg.table
        seen["nets"] = [t.item(r, 0).text() for r in range(t.rowCount())]
        seen["loads"] = [t.item(r, 3).text() for r in range(t.rowCount())]
        assert dlg.ref() == "U1" and dlg.total.value() == pytest.approx(0.3)
        dlg.total.setValue(1.0)
        seen["shares"] = [t.item(r, 2).text() for r in range(t.rowCount())]
        return True

    panel.split_load_across_nets(dialog=run)
    assert seen == {"nets": ["+3V3", "+3V3_PLL"], "loads": ["U1 core", "new"],
                    "shares": ["0.6 A", "0.4 A"]}
    assert [(ld.name, ld.supply.net, ld.current) for ld in setup.loads] == [
        ("U1 core", "+3V3", pytest.approx(0.6)), ("U1 +3V3_PLL", "+3V3_PLL", pytest.approx(0.4))]
    assert setup.loads[1].ground == PinRef("U1", "GND")
    assert panel.load_list.lst.count() == 2

    def equal_one_net(dlg):                   # untick +3V3, share equally: all on the PLL
        dlg.per.setCurrentIndex(1)
        dlg.table.item(0, 0).setCheckState(Qt.Unchecked)
        assert dlg.chosen() == {"+3V3_PLL": pytest.approx(1.0)}
        return True

    panel.split_load_across_nets("U1", dialog=equal_one_net)
    assert [ld.current for ld in setup.loads] == [pytest.approx(0.6), pytest.approx(1.0)]
    assert len(setup.loads) == 2
    assert not panel.split_load_across_nets(dialog=lambda dlg: False)   # cancelled
    assert len(setup.loads) == 2
    panel.close()
