"""Capacitor DC bias (#74): models with a voltage rating can be derated by the DC voltage
across each part, from the last DC solve or else the rail's VRM set point, with a switch
in the sweep settings to turn it off."""

import os

import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.checks import validate_ac
from kipi.ac.html_report import write_ac_report
from kipi.ac.model import CapModel
from kipi.ac.solver import solve_ac
from kipi.core.model.parts import is_class2, parse_voltage_rating
from kipi.dc.pi.model import Load, PinRef, PISetup

# A small board with a coarse mesh: these tests are about the capacitor models, and the
# field solve on a bigger one takes ~20 s without PARDISO.
SMALL = dict(max_edge_mm=2.0, min_edge_mm=0.15, grading=0.6)


def test_derating_curve():
    m = CapModel("X5R", 10e-6, 0.5e-9, 5e-3, dc_bias=True, rated_v=6.3, kept_at_rated_pct=40)
    assert m.capacitance_at(0.0) == 10e-6
    assert m.capacitance_at(6.3) == pytest.approx(4e-6)
    assert m.capacitance_at(-6.3) == pytest.approx(4e-6)
    cs = [m.capacitance_at(v) for v in (1.0, 2.0, 3.3, 5.0, 10.0)]
    assert all(a > b for a, b in zip(cs, cs[1:]))            # less the higher the bias
    assert 4e-6 < m.capacitance_at(3.3) < 10e-6
    for off in (CapModel("C0G", 1e-9, dc_bias=False, rated_v=50),
                CapModel("no rating", 1e-6, dc_bias=True)):
        assert off.capacitance_at(5.0) == off.capacitance_f


@pytest.mark.parametrize("value, volts, class2", [
    ("10uF 6.3V X5R", 6.3, True), ("100n/50V", 50.0, False), ("4.7u 16 V", 16.0, False),
    ("1u 6V3", 6.3, False), ("10u_25V_X7R", 25.0, True), ("1u X7R", None, True),
    ("100n", None, False), ("47p 50V C0G", 50.0, False), ("22u 10V x6s", 10.0, True),
])
def test_rating_and_dielectric_from_the_value(value, volts, class2):
    assert parse_voltage_rating(value) == volts
    assert is_class2(value) is class2


def test_bias_fields_are_saved_and_old_setups_load():
    setup = PISetup()
    setup.caps.append(CapModel("A", 1e-6, 1e-9, 1e-3, ["C1"], True, 16.0, 35.0))
    setup.ac.dc_bias = False
    back = PISetup.from_dict(setup.to_dict())
    assert back.caps[0].dc_bias and back.caps[0].rated_v == 16.0
    assert back.caps[0].kept_at_rated_pct == 35.0 and back.ac.dc_bias is False
    d = setup.to_dict()
    for k in ("dc_bias", "rated_v", "kept_at_rated_pct"):
        del d["caps"][0][k]
    del d["ac"]["dc_bias"]
    old = PISetup.from_dict(d)
    assert not old.caps[0].dc_bias and old.caps[0].rated_v is None and old.ac.dc_bias


def test_a_model_estimating_bias_needs_a_rating():
    snap = plane_pair(caps=[("C1", "10u", (25.0, 25.0))])
    setup = _setup(snap, rated_v=None)
    assert any("voltage rating" in e for e in validate_ac(setup, "VRM1", "GND"))
    setup.ac.dc_bias = False                       # switched off: the rating isn't needed
    assert not any("voltage rating" in e for e in validate_ac(setup, "VRM1", "GND"))


def _setup(snap, rated_v=6.3, **ac):
    setup = plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e8, points_per_decade=5,
                        field_points_per_decade=2, **SMALL, **ac)
    setup.caps.append(CapModel("10u", 10e-6, 0.5e-9, 5e-3, ["C1"], dc_bias=True,
                               rated_v=rated_v, kept_at_rated_pct=40.0))
    return setup


@pytest.fixture(scope="module")
def board():
    return plane_pair(20.0, caps=[("C1", "10u", (10.0, 10.0)), ("C2", "LOAD", (15.0, 15.0))])


def test_without_a_dc_solve_the_vrm_set_point_is_used(board):
    setup = _setup(board)
    r = solve_ac(board, setup, "VRM1", "GND")
    assert r.cap_bias["C1"] == (3.3, pytest.approx(setup.caps[0].capacitance_at(3.3)))
    assert "set point" in r.bias_note and "VRM1" in r.bias_note and "aged" not in r.bias_note
    assert setup.caps[0].capacitance_f == 10e-6           # the setup's model is untouched
    setup.ac.dc_bias = False
    off = solve_ac(board, setup, "VRM1", "GND")
    assert off.cap_bias == {} and off.bias_note == ""
    # Less capacitance: a higher impedance below the capacitor's resonance.
    assert abs(r.probes[0].z[0]) > abs(off.probes[0].z[0]) * 1.5


def test_the_dc_solve_gives_each_parts_voltage(board):
    from kipi.dc.pi.tree import solve_tree
    setup = _setup(board)
    setup.loads.append(Load("C2", PinRef("C2", "+3V3", ["1"]), 20.0,
                            PinRef("C2", "GND", ["2"])))
    tree = solve_tree(board, setup)
    r = solve_ac(board, setup, "VRM1", "GND", dc_result=tree)
    v, c = r.cap_bias["C1"]
    assert 3.0 < v < 3.3 - 1e-4                      # the plane's drop under 20 A
    assert c == pytest.approx(setup.caps[0].capacitance_at(v))
    assert "last DC solve" in r.bias_note


def test_the_report_shows_the_derated_capacitance(board, tmp_path):
    setup = _setup(board)
    r = solve_ac(board, setup, "VRM1", "GND")
    html = write_ac_report(tmp_path / "r.html", r, board, setup).read_text(encoding="utf-8")
    assert "C derated" in html and "at 3.3 V" in html


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

@pytest.fixture
def panel():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.ac.gui.panel import ACPanel
    app = QApplication.instance() or QApplication([])   # noqa: F841
    p = ACPanel()
    snap = plane_pair(caps=[("C1", "10uF 6.3V X5R", (25.0, 25.0)),
                            ("C2", "100n 50V C0G", (30.0, 30.0))])
    p.set_board(snap, PISetup(), "test")
    p.set_nets(["+3V3", "GND"])
    yield p
    p.close()


def test_models_from_values_read_the_rating(panel):
    panel._models_from_values()
    by_name = {m.name: m for m in panel.setup.caps}
    x5r, c0g = by_name["10uF 6.3V X5R"], by_name["100n 50V C0G"]
    assert x5r.dc_bias and x5r.rated_v == 6.3
    assert not c0g.dc_bias and c0g.rated_v == 50.0


def test_the_editor_and_the_sweep_switch(panel):
    panel.cap_list._add()
    ed = panel.cap_list.editor
    m = panel.setup.caps[-1]
    ed.rated.setValue(16.0)
    ed.chk_bias.setChecked(True)
    ed.kept.setValue(30.0)
    assert m.dc_bias and m.rated_v == 16.0 and m.kept_at_rated_pct == 30.0
    assert "DC bias 16 V rated" in panel._describe_cap(m)
    panel.chk_bias.setChecked(False)
    assert panel.setup.ac.dc_bias is False
    assert np.isclose(m.capacitance_at(16.0), m.capacitance_f * 0.3)
