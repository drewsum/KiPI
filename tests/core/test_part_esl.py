"""A series link's ESL estimated from its part's package, or from a ferrite bead's
impedance (#159)."""

import math
import os

import pytest

from kipi.core.model.board import Footprint, Pad
from kipi.core.model.parts import bead_esl_h, chip_esl_h, chip_length_mm, part_esl
from synthetic import make_board


@pytest.mark.parametrize("name, mm", [
    ("Resistor_SMD:R_0603_1608Metric", 1.6), ("R_0402_1005Metric_Pad0.72x0.64mm_HandSolder", 1.0),
    ("Resistor_SMD:R_2512_6332Metric", 6.3), ("R_0612_1632Metric", 1.6),      # wide terminal
    ("Resistor_SMD:R_0201_0603Metric", 0.6), ("RESC2012X65N", 2.0), ("R0805", 2.0),
    ("Inductor_SMD:L_0603_1608Metric", 1.6), ("my_lib:0805", 2.0), ("Fuse:Fuse_1812", 4.5),
])
def test_chip_lengths_from_footprint_names(name, mm):
    assert chip_length_mm(name) == pytest.approx(mm)


@pytest.mark.parametrize("name", ["", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "R_Axial_DIN0207",
                                  "TestPoint_Pad_D1.0mm", "Fuse:Fuse_Littelfuse_2920"])
def test_names_without_a_chip_size(name):
    assert chip_length_mm(name) is None


def test_the_estimate_goes_through_vishays_0402_and_0603():
    assert chip_esl_h(1.0) == pytest.approx(0.4e-9)
    assert chip_esl_h(1.6) == pytest.approx(0.58e-9)
    assert chip_esl_h(6.3) == pytest.approx(1.99e-9)


def test_part_esl_from_the_footprint_else_the_pads():
    snap = make_board()
    snap.footprints.append(Footprint("f9", "R5", "10m", (0, 0), "top",
                                     footprint="Resistor_SMD:R_1206_3216Metric"))
    esl, how = part_esl(snap, "R5")
    assert esl == pytest.approx(chip_esl_h(3.2)) and "R_1206_3216Metric" in how
    # No size in the name: the two pads' spacing.
    snap.footprints.append(Footprint("f10", "FB2", "BLM18", (0, 0), "top", footprint="lib:FB"))
    snap.pads += [Pad("q1", "A", "FB2", "1", (5.0, 5.0), "smd"),
                  Pad("q2", "B", "FB2", "2", (5.0, 6.5), "smd")]
    esl, how = part_esl(snap, "FB2")
    assert esl == pytest.approx(chip_esl_h(1.5)) and "1.5 mm" in how
    assert part_esl(snap, "J1") is None          # no size, and one pad
    assert part_esl(snap, "R99") is None and part_esl(None, "R5") is None


def test_bead_esl_from_its_impedance():
    # 60 Ω at 10 MHz on the slope: about 0.95 µH.
    assert bead_esl_h(60, 10e6) == pytest.approx(60 / (2 * math.pi * 10e6))
    # Its DCR takes its share of |Z| in quadrature.
    assert bead_esl_h(5, 1e6, 3) == pytest.approx(4 / (2 * math.pi * 1e6))
    assert bead_esl_h(0.05, 1e6, 0.1) is None and bead_esl_h(60, 0) is None


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_link_editor_estimates_from_the_package(app):
    from kipi.dc.gui.pi_panel import PIPanel
    from kipi.dc.pi.model import DcLink, PinRef
    snap = make_board()
    snap.footprints.append(Footprint("f9", "R5", "10m", (0, 0), "top",
                                     footprint="Resistor_SMD:R_0805_2012Metric"))
    p = PIPanel()
    p.snapshot = snap
    ed = p.link_list.editor
    k = DcLink("R5", PinRef("R5", "A", ["1"]), PinRef("R5", "B", ["2"]), 0.01)
    ed.load(k)
    ed._esl_from_package()
    assert k.esl_h == pytest.approx(chip_esl_h(2.0))
    assert "R_0805_2012Metric" in ed.lbl_esl.text()
    p.close()


def test_the_bead_dialog(app):
    from qtpy.QtWidgets import QDialogButtonBox
    from kipi.dc.gui.pi_panel import BeadDialog
    d = BeadDialog(0.1)
    d.z.setValue(60)
    d.freq.setValue(10)
    assert d.esl_h == pytest.approx(math.sqrt(60 ** 2 - 0.01) / (2 * math.pi * 10e6))
    assert "nH" in d.lbl.text()
    d.z.setValue(0.05)                           # below the DCR: nothing to give
    assert d.esl_h is None and not d.buttons.button(QDialogButtonBox.Ok).isEnabled()
