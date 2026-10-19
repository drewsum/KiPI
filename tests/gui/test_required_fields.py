"""The setup editors show which fields a solve needs (#136): bold if required, gray if
optional, gray italic if only the other analysis uses them."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.dc.pi.model import DcLink, Load, PinRef, Vrm  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _needs(editor, *widgets):
    return [editor.form.labelForField(w).property("kipi_need") for w in widgets]


def test_a_dc_vrm_needs_its_pins_voltage_and_for_a_converter_its_input(app):
    from kipi.dc.gui.pi_panel import PIPanel
    p = PIPanel()
    ed = p.vrm_list.editor
    ed.load(Vrm("U1", PinRef("U1", "3V3", ["1"]), 3.3, kind="source"))
    assert _needs(ed, ed.name, ed.kind, ed.voltage, ed.output) == ["required"] * 4
    assert _needs(ed, ed.input, ed.ground, ed.sense, ed.iq, ed.vin_row) == ["optional"] * 5
    assert _needs(ed, ed.bandwidth, ed.acc_row) == ["unused"] * 2
    assert "font-weight: bold" in ed.form.labelForField(ed.voltage).styleSheet()
    ed.load(Vrm("U2", PinRef("U2", "1V8", ["1"]), 1.8, kind="ldo"))
    assert _needs(ed, ed.input) == ["required"]
    assert "needed to solve" in ed.lbl_legend.text() and "other analysis" in ed.lbl_legend.text()
    p.close()


def test_an_ac_vrm_marks_the_dc_fields_unused(app):
    from kipi.ac.gui.panel import ACPanel
    p = ACPanel()
    ed = p.vrm_list.editor
    ed.load(Vrm("U2", PinRef("U2", "1V8", ["1"]), 1.8, kind="smps"))
    assert _needs(ed, ed.name, ed.voltage, ed.output) == ["required"] * 3
    assert _needs(ed, ed.bandwidth, ed.acc_row, ed.ground) == ["optional"] * 3
    assert _needs(ed, ed.kind, ed.eff, ed.input, ed.vin_row, ed.dropout) == ["unused"] * 5
    link = p.link_list.editor
    assert _needs(link, link.a, link.b, link.r) == ["required"] * 3
    assert _needs(link, link.esl) == ["optional"]
    cap = p.cap_list.editor
    assert _needs(cap, cap.cap, cap.esl, cap.esr) == ["required"] * 3
    assert _needs(cap, cap.rated, cap.dielectric, cap.mfg_tol) == ["optional"] * 3
    probe = p.probe_list.editor
    assert _needs(probe, probe.rail, probe.ground) == ["required"] * 2
    assert _needs(probe, probe.target) == ["optional"]
    p.close()


def test_dc_loads_and_links(app):
    from kipi.dc.gui.pi_panel import PIPanel
    p = PIPanel()
    ed = p.load_list.editor
    ed.load(Load("U3", PinRef("U3", "3V3", ["4"]), 0.5))
    assert _needs(ed, ed.supply, ed.current) == ["required"] * 2
    assert _needs(ed, ed.ground, ed.tol) == ["optional"] * 2
    link = p.link_list.editor
    link.load(DcLink("R1", PinRef("R1", "A", ["1"]), PinRef("R1", "B", ["2"]), 0.01))
    assert _needs(link, link.r, link.esl) == ["required", "unused"]
    assert "other analysis" in link.lbl_legend.text()
    p.close()
