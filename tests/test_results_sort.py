"""Results tables sort by a clicked column header, alternating ascending and descending."""

import os
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.dc.pi.tree import solve_tree  # noqa: E402
from kipi.dc.solver.post import ViaResult  # noqa: E402
from test_multiphase import board, two_phase  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def click(t, col):
    t.horizontalHeader().sectionClicked.emit(col)


def column(t, col):
    return [t.item(r, col).text() for r in range(t.rowCount())]


def test_numbers_sort_by_value_and_clicks_alternate(app):
    from kipi.dc.gui.pi_panel import PIPanel
    currents = [0.009, 0.1, 0.02, 0.0005]           # as text, "9.00" > "100.00"
    vias = [ViaResult(f"v{i}", "via", (float(i), 0.0), 0.3, a, 1e-6)
            for i, a in enumerate(currents)]
    p = PIPanel()
    p.snapshot = board()
    p._tree = NS(groups={"G": NS(name="G", result=NS(fields=NS(vias=vias)))})
    p._fill_vias()
    t = p.tbl_vias
    p.spin_via_limit.setValue(15)                   # mA
    click(t, 5)
    assert column(t, 5) == ["0.50", "9.00", "20.00", "100.00"]
    click(t, 5)
    assert column(t, 5) == ["100.00", "20.00", "9.00", "0.50"]
    click(t, 5)
    assert column(t, 5)[0] == "0.50"
    # PASS / FAIL follows each row's own current after sorting and a limit change.
    p.spin_via_limit.setValue(10)
    assert column(t, 7) == ["PASS", "PASS", "FAIL", "FAIL"]
    assert t.item(0, 0).data(0x0100) == "v3"       # the row keeps its via id
    p.close()


def test_phases_stay_under_their_vrm_and_totals_stay_last(app):
    from kipi.dc.gui.pi_panel import PIPanel
    snap = board()
    tree = solve_tree(snap, two_phase())
    p = PIPanel()
    p.snapshot = snap
    p.show_results(tree, next(iter(tree.groups)))
    t = p.tbl_vrms
    by_name = {v.name: v for v in tree.vrms}
    assert any(v.phases for v in tree.vrms)
    orders = []
    for _ in range(2):                              # ascending, then descending
        click(t, 3)                                 # I out
        names = column(t, 0)
        vrms = [n for n in names if not n.startswith(" ")]
        orders.append(vrms)
        for n in vrms:                              # each VRM's phases, in order, under it
            k, ph = names.index(n), by_name[n].phases
            assert [x.strip() for x in names[k + 1:k + 1 + len(ph)]] == \
                [f"phase {x.index}" for x in ph]
    assert orders[0] == orders[1][::-1]             # the VRMs themselves did sort

    t = p.tbl_losses
    pinned = ["All copper + parts"] + [f"VRM {v.name} (conversion)" for v in tree.vrms
                                       if v.p_loss is not None]
    for _ in range(2):
        click(t, t.columnCount() - 1)               # Total
        assert column(t, 0)[-len(pinned):] == pinned
    p.close()
