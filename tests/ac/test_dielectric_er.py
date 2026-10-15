"""The AC solve uses the stackup's εr, and Default εr only where the stackup has none (#65)."""

import pytest

from kipi.ac.model import ACSettings
from kipi.ac.solver.fem import dielectric_summary
from kipi.core.model.board import CopperLayer, DielectricLayer, Stackup

F, IN1, B = 3, 4, 34


def stackup(er_top, er_bottom, from_board=True):
    copper = [CopperLayer(F, "F.Cu", 0, 0.035, 1.235), CopperLayer(IN1, "In1.Cu", 1, 0.035, 1.0),
              CopperLayer(B, "B.Cu", 2, 0.035, 0.0)]
    diels = [DielectricLayer(0.2, "PP", er_top, F, IN1), DielectricLayer(0.965, "Core", er_bottom, IN1, B)]
    return Stackup(copper, diels, 1.27, from_board)


def test_summary_names_each_dielectrics_er():
    text = dielectric_summary(stackup(3.9, 4.6), 4.3)
    assert text == "PP 200 µm εr 3.9, Core 965 µm εr 4.6"


def test_summary_marks_the_default():
    text = dielectric_summary(stackup(3.9, 0.0, from_board=False), 4.3)
    assert "Core 965 µm εr 4.3 (Default εr)" in text
    assert "PP 200 µm εr 3.9" in text
    assert text.endswith("[stackup not read from the board]")
    assert "Default εr" in dielectric_summary(Stackup([], [], 1.6), 4.3)


def _gap(st, i, j, default_er=4.3):
    from kipi.ac.solver.fem import ACSystem
    sys_ = ACSystem.__new__(ACSystem)              # _gap only reads the stackup and settings
    sys_.snap = type("S", (), {"stackup": st})()
    sys_.ac = ACSettings(default_er=default_er)
    return sys_._gap(i, j)


def test_gap_uses_the_stackup_er():
    gap, er = _gap(stackup(3.9, 4.6), 0, 1)
    assert gap == pytest.approx(0.2) and er == pytest.approx(3.9)
    _, er = _gap(stackup(3.9, 4.6), 1, 2)
    assert er == pytest.approx(4.6)


def test_gap_falls_back_to_default_er():
    _, er = _gap(stackup(3.9, 0.0), 1, 2, default_er=4.0)
    assert er == pytest.approx(4.0)


def test_gap_combines_layers_in_series():
    # F.Cu to B.Cu crosses PP, In1's copper (counted as Default εr) and the core.
    gap, er = _gap(stackup(3.9, 4.6), 0, 2, default_er=4.3)
    assert gap == pytest.approx(1.2)
    assert er == pytest.approx(1.2 / (0.2 / 3.9 + 0.965 / 4.6 + 0.035 / 4.3))
