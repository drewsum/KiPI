"""A capacitor's or series part's values from its SPICE model (#51): only what the DC, AC
and Coupled Noise solves use is kept."""

import math
import os

import numpy as np
import pytest

from kipi.core.model.board import Footprint
from kipi.core.model.spice import (
    Library, SpiceError, best_match, capacitor_ref, capacitor_values, evaluate, link_ref,
    link_values, number,
)
from synthetic import make_board

LIB = """\
* A vendor's library: a capacitor ladder, a ferrite bead and a resistor
.SUBCKT GRM155R61A105KE15 1 2
C1 1 3 1u
L1 3 4 0.4n
R1 4 2 5m
R2 1 2 100meg          ; leakage
.ENDS
.subckt BLM18PG221 a b params: L0=1u
R1 a m 0.05
L1 m b {L0}
R2 m b 300
C1 m b
+ 1p
.ends
.subckt WSL0805 1 2
R1 1 n 10m $ the element
L1 n 2 0.7n
.ends
.subckt CAP_NESTED 1 2
V1 1 3 0
X1 3 2 GRM155R61A105KE15
.ends
.subckt LT 1 2
C1 1 2 10u Rser=3m Lser=1n
.ends
.subckt THREE 1 2 3
R1 1 2 1
.ends
"""


@pytest.mark.parametrize("text, value", [
    ("10u", 10e-6), ("2.2nF", 2.2e-9), ("1.5MEG", 1.5e6), ("1M", 1e-3), ("1e-6", 1e-6),
    ("100pF", 100e-12), ("4.7k", 4.7e3), ("0.1Ohm", 0.1), (".5", 0.5), ("1F", 1e-15),
])
def test_spice_numbers(text, value):
    assert number(text) == pytest.approx(value)


def test_values_may_be_arithmetic_of_parameters():
    assert evaluate("{2*c0 + 1n}", {"c0": 1e-9}) == pytest.approx(3e-9)
    assert evaluate("'sqrt(4) * (1 - 0.5)^2'") == pytest.approx(0.5)
    for bad in ("{__import__('os')}", "{c0}", "{1/0}", "{1 +}"):
        with pytest.raises(SpiceError):
            evaluate(bad)


def test_a_capacitor_ladder_gives_its_series_values():
    lib = Library(LIB)
    assert lib.two_pin() == ["GRM155R61A105KE15", "BLM18PG221", "WSL0805", "CAP_NESTED", "LT"]
    for name in ("GRM155R61A105KE15", "CAP_NESTED"):
        v = capacitor_values(lib.model(name))
        assert v.capacitance_f == pytest.approx(1e-6, rel=1e-4)
        assert v.esl_h == pytest.approx(0.4e-9, rel=1e-4)
        assert v.esr_ohm == pytest.approx(5e-3, rel=1e-4)
        assert v.srf_hz == pytest.approx(1 / (2 * math.pi * math.sqrt(1e-6 * 0.4e-9)), rel=1e-3)
    v = capacitor_values(lib.model("LT"))                  # LTspice's Rser and Lser
    assert (v.capacitance_f, v.esl_h, v.esr_ohm) == pytest.approx((10e-6, 1e-9, 3e-3), rel=1e-4)


def test_a_series_part_gives_its_dcr_and_low_frequency_inductance():
    lib = Library(LIB)
    bead = link_values(lib.model("BLM18PG221"))
    assert bead.resistance_ohm == pytest.approx(0.05)
    assert bead.esl_h == pytest.approx(1e-6, rel=0.01)
    res = link_values(lib.model("wsl0805"))                # names are not case sensitive
    assert (res.resistance_ohm, res.esl_h) == pytest.approx((10e-3, 0.7e-9), rel=1e-6)


def test_the_wrong_kind_of_model_is_refused():
    lib = Library(LIB)
    with pytest.raises(SpiceError, match="is capacitive"):     # through its leakage
        link_values(lib.model("GRM155R61A105KE15"))
    with pytest.raises(SpiceError, match="does not conduct at DC"):
        link_values(Library(".subckt C 1 2\nC1 1 2 1u\n.ends").model("C"))
    with pytest.raises(SpiceError, match="not capacitive"):
        capacitor_values(lib.model("WSL0805"))
    with pytest.raises(SpiceError, match="3 pins"):
        capacitor_values(lib.model("THREE"))


@pytest.mark.parametrize("body, why", [
    ("D1 1 2 DMOD", "DMOD is not in the file"), ("Q1 1 2 0 QN", "transistors"),
    ("E1 1 2 3 0 10", "controlled sources"),
    ("B1 1 2 V=V(1)*2", "behavioral"), ("V1 1 2 5", "0 V sources"),
    ("X1 1 2 MISSING", "not in the file"), ("R1 1 2 {nope}", "unknown parameter"),
])
def test_only_passive_networks_are_read(body, why):
    lib = Library(f".subckt P 1 2\n{body}\nC9 1 2 1u\n.ends\n")
    with pytest.raises(SpiceError, match=why):
        lib.model("P")


DIODES = """\
.model D1N4148 D(IS=2.52n RS=0.568 N=1.752 BV=100 IBV=100u CJO=4p M=0.4 TT=20n mfg=Vishay)
.MODEL SS34 D (Is=1u N=1.1
+ Cjo=300p)
.model IDEAL D(Ron=10m Vfwd=0.3)
.subckt SCH A K
L1 A x 1n
D1 x K SS34 2
.ends
"""


def test_a_diode_model_follows_the_junction_equation():
    lib = Library(DIODES)
    assert lib.diodes() == ["D1N4148", "SS34", "IDEAL"] and lib.two_pin() == ["SCH"]
    m = lib.model("D1N4148")
    vt = 1.380649e-23 * 300.15 / 1.602176634e-19
    for amps in (1e-4, 1e-2, 0.3):
        v, r, _ = m.operating_point(amps)
        assert v == pytest.approx(1.752 * vt * math.log1p(amps / 2.52e-9) + 0.568 * amps,
                                  rel=1e-6)
        assert r == pytest.approx(1.752 * vt / (amps + 2.52e-9) + 0.568, rel=1e-4)
        i, g = m.dc_current(v)
        assert i == pytest.approx(amps, rel=1e-6) and g == pytest.approx(1 / r, rel=1e-4)
    assert abs(m.dc_current(-50)[0]) < 1e-8                      # blocks...
    assert m.dc_current(-101.5)[0] < -0.1                         # ... until it breaks down
    # Small signal at 10 mA: its slope resistance, with its junction and diffusion C.
    z = m.impedance([1e3], bias_a=0.01)[0]
    assert z.real == pytest.approx(1.752 * vt / 0.01 + 0.568, rel=1e-3)
    assert abs(m.impedance([1e9], bias_a=0.0)[0]) < abs(m.impedance([1e3], bias_a=0.0)[0])


def test_a_subcircuit_with_a_diode_in_it():
    lib = Library(DIODES)
    sch = lib.model("SCH")
    one = Library(".model X D(Is=2u N=1.1 Cjo=600p)").model("X")      # area 2
    assert sch.operating_point(1.0)[0] == pytest.approx(one.operating_point(1.0)[0], rel=1e-6)
    v = link_values(sch)
    assert v.esl_h == pytest.approx(1e-9, rel=0.01) and v.forward[0][0] == 1e-3
    # Kept in a setup, the subcircuit brings its diode model along.
    ref, _ = link_ref(lib, "SCH")
    assert ".model SS34 D(" in ref.netlist and ref.has_diodes
    assert ref.model().operating_point(0.5)[0] == pytest.approx(sch.operating_point(0.5)[0])
    ref, values = link_ref(lib, "d1n4148")
    assert ref.subckt == "D1N4148" and values.forward[1][1] == pytest.approx(0.85, abs=0.01)
    with pytest.raises(SpiceError, match="piecewise-linear"):
        lib.model("IDEAL")


def test_endless_nesting_is_refused():
    lib = Library(".subckt A 1 2\nX1 1 2 A\n.ends\n")
    with pytest.raises(SpiceError, match="nested"):
        lib.model("A")


def test_a_file_is_read_and_too_big_ones_refused(tmp_path, monkeypatch):
    p = tmp_path / "parts.lib"
    p.write_bytes(LIB.replace("0.05", "0.05 ; 5 µm²").encode("latin-1"))
    assert "WSL0805" in Library.read(p).two_pin()
    monkeypatch.setattr("kipi.core.model.spice.MAX_FILE_BYTES", 10)
    with pytest.raises(SpiceError, match="too large"):
        Library.read(p)
    with pytest.raises(SpiceError, match="no .ENDS"):
        Library(".subckt A 1 2\nR1 1 2 1\n")


def test_impedance_of_a_series_rlc():
    m = Library(LIB).model("GRM155R61A105KE15")
    f = np.array([1e3, 1e8])
    w = 2 * np.pi * f
    zs = 5e-3 + 1j * w * 0.4e-9 + 1 / (1j * w * 1e-6)
    want = 1 / (1 / zs + 1 / 100e6)
    assert m.impedance(f) == pytest.approx(want, rel=1e-9)


def test_the_subcircuit_named_like_the_part_is_picked():
    names = ["GRM155R61A105KE15", "GRM188R61A106KE69", "BLM18PG221SN1"]
    assert best_match(names, ["Model1", "GRM188R61A106KE69D"]) == "GRM188R61A106KE69"
    assert best_match(names, ["blm18pg221sn1"]) == "BLM18PG221SN1"
    assert best_match(names, ["10u"]) is None


# ------------------------------------------------------------------ GUI
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_dialog_shows_the_values_or_why_not(app):
    from qtpy.QtWidgets import QDialogButtonBox
    from kipi.gui.spice_dialog import CAPACITOR, SpiceModelDialog
    d = SpiceModelDialog(Library(LIB, "parts.lib"), CAPACITOR, ["LT"])
    assert d.name == "LT" and "10 µF" in d.result.text()
    d.list.setCurrentRow(2)                                  # the resistor
    assert d.values is None and "not capacitive" in d.result.text()
    assert not d.buttons.button(QDialogButtonBox.Ok).isEnabled()
    d.filter.setText("blm")
    assert [d.list.item(i).isHidden() for i in range(d.list.count())] == [
        True, False, True, True, True]


def test_the_link_editor_imports_the_model_and_locks_dcr_and_esl(app, monkeypatch):
    from kipi.dc.gui import pi_panel
    from kipi.dc.pi.model import DcLink, PinRef
    seen = {}

    def fake(parent, kind, hints, pins=()):
        seen["hints"] = hints
        return (*link_ref(Library(LIB, "parts.lib"), "BLM18PG221"), None)
    monkeypatch.setattr(pi_panel, "import_spice", fake)
    snap = make_board()
    snap.footprints.append(Footprint("f9", "FB1", "BLM18PG221SN1", (0, 0), "top"))
    p = pi_panel.PIPanel()
    p.snapshot = snap
    ed = p.link_list.editor
    k = DcLink("FB1", PinRef("FB1", "A", ["1"]), PinRef("FB1", "B", ["2"]), 0.0)
    ed.load(k)
    assert ed.chk_spice.isHidden()                          # no model yet
    ed._from_spice()
    assert seen["hints"] == ["FB1", "BLM18PG221SN1"]
    assert k.uses_spice and k.spice.subckt == "BLM18PG221"
    assert k.resistance == pytest.approx(0.05)
    assert k.esl_h == pytest.approx(1e-6, rel=0.01)
    assert ed.chk_spice.isChecked() and not ed.r.isEnabledTo(ed)
    assert "BLM18PG221 from parts.lib" in ed.lbl_spice.text()
    ed.chk_spice.setChecked(False)                           # back to the typed values
    assert not k.uses_spice and ed.r.isEnabledTo(ed) and k.spice is not None
    ed.r.setValue(80.0)
    assert k.resistance == pytest.approx(0.08)
    ed.chk_spice.setChecked(True)                            # the fitted ones come back
    assert k.resistance == pytest.approx(0.05)
    p.close()


def test_the_capacitor_editor_imports_the_model(app, monkeypatch):
    from kipi.ac.gui import panel
    from kipi.ac.model import CapModel

    monkeypatch.setattr(panel, "import_spice",
                        lambda parent, kind, hints: capacitor_ref(Library(LIB), "LT"))
    p = panel.ACPanel()
    m = CapModel("Model1", rated_v=6.3)
    p.setup.caps.append(m)
    ed = p.cap_list.editor
    ed.load(m)
    ed._from_spice()
    assert (m.capacitance_f, m.esl_h, m.esr_ohm) == pytest.approx((10e-6, 1e-9, 3e-3),
                                                                  rel=1e-4)
    assert m.uses_spice and m.name == "LT" and m.rated_v == 6.3    # the rest stays
    assert not ed.cap.isEnabledTo(ed) and ed.rated.isEnabledTo(ed)
    named = CapModel("10u bulk")
    ed.load(named)
    ed._from_spice()
    assert named.name == "10u bulk"                          # a name the user gave stays
    p.close()


# ------------------------------------------------------------------ solves
# A capacitor no series RLC describes: two branches, so two resonances.
TWO_BRANCH = """.subckt TWO 1 2
C1 1 a 10u
L1 a b 1n
R1 b 2 5m
C2 1 c 100n
L2 c d 0.2n
R2 d 2 20m
.ends
"""


def test_a_kept_model_reads_back_alone_and_is_saved_with_the_setup():
    from kipi.ac.model import CapModel
    from kipi.dc.pi.model import DcLink, PinRef, PISetup
    lib = Library(".param lval=0.4n\n" + LIB.replace("L1 3 4 0.4n", "L1 3 4 {lval}"))
    ref, _ = capacitor_ref(lib, "CAP_NESTED")
    assert ".SUBCKT GRM155R61A105KE15" in ref.netlist and "LT" not in ref.netlist
    f = np.logspace(3, 9, 7)
    assert ref.model().impedance(f) == pytest.approx(lib.model("CAP_NESTED").impedance(f))
    setup = PISetup()
    setup.caps.append(CapModel("C", 1e-6, 0.4e-9, 5e-3, ["C1"], spice=ref))
    bead, _ = link_ref(Library(LIB), "BLM18PG221")
    setup.links.append(DcLink("FB1", PinRef("FB1", "A"), PinRef("FB1", "B"), 0.05,
                              spice=bead))
    back = PISetup.from_dict(setup.to_dict())
    assert back.caps[0].spice == ref and back.links[0].spice == bead
    assert PISetup.from_dict({"caps": [{"name": "old"}]}).caps[0].spice is None


def test_a_capacitor_takes_its_impedance_from_its_model():
    import dataclasses
    from kipi.ac.model import CapModel
    ref, v = capacitor_ref(Library(TWO_BRANCH), "TWO")
    m = CapModel("TWO", v.capacitance_f, v.esl_h, v.esr_ohm, spice=ref)
    w = 2 * np.pi * np.logspace(4, 9, 300)
    z1 = 5e-3 + 1j * w * 1e-9 + 1 / (1j * w * 10e-6)
    z2 = 20e-3 + 1j * w * 0.2e-9 + 1 / (1j * w * 100e-9)
    both = 1 / (1 / z1 + 1 / z2)
    assert m.impedance(w) == pytest.approx(both, rel=1e-9)
    rlc = dataclasses.replace(m, spice=None).impedance(w)
    assert np.max(np.abs(np.abs(rlc) / np.abs(both) - 1)) > 0.5    # the fit alone is off
    ref.use = False
    assert m.impedance(w) == pytest.approx(rlc)
    ref.use = True
    # A derating (a copy with less capacitance) scales every capacitor in the model.
    half = dataclasses.replace(m, capacitance_f=m.capacitance_f / 2).impedance(w)
    z1h = 5e-3 + 1j * w * 1e-9 + 1 / (1j * w * 5e-6)
    z2h = 20e-3 + 1j * w * 0.2e-9 + 1 / (1j * w * 50e-9)
    assert half == pytest.approx(1 / (1 / z1h + 1 / z2h), rel=1e-9)


def test_a_link_takes_its_impedance_from_its_model_in_the_solves():
    from kipi.ac.solver.fem import PortDef
    from kipi.ac.solver.sweep import element_impedance
    from kipi.dc.pi.model import DcLink, PinRef
    ref, v = link_ref(Library(LIB), "BLM18PG221")
    k = DcLink("FB1", PinRef("FB1", "A"), PinRef("FB1", "B"), v.resistance_ohm,
               v.esl_h, spice=ref)
    w = 2 * np.pi * np.array([1e5, 1e8, 1e9])
    zl = 1j * w * 1e-6
    zp = 1 / (1 / zl + 1 / 300 + 1j * w * 1e-12)
    got = element_impedance(PortDef("FB1", "link", [], [], k), w)
    assert got == pytest.approx(0.05 + zp, rel=1e-9)
    assert abs(got[1]) == pytest.approx(300, rel=0.2)       # the bead's resistive peak
    assert not DcLink("J1", k.side_a, k.side_b, 0.0, spice=ref).is_short()
    assert DcLink("J1", k.side_a, k.side_b, 0.0).is_short()


def test_the_noise_split_uses_the_model():
    from kipi.ac.model import CapModel
    from kipi.noise.waveforms import CapBranch
    ref, v = capacitor_ref(Library(TWO_BRANCH), "TWO")
    m = CapModel("TWO", v.capacitance_f, v.esl_h, v.esr_ohm, spice=ref)
    w = 2 * np.pi * np.array([1e6, 3e7])
    b = CapBranch("C1", v.capacitance_f, v.esr_ohm, v.esl_h, 0.5e-9, 1e-3, model=m)
    assert b.impedance(w) == pytest.approx(m.impedance(w) + 1e-3 + 1j * w * 0.5e-9)


def test_a_setup_with_a_model_goes_in_a_results_file(tmp_path):
    from kipi.ac.model import CapModel
    from kipi.core import results_io
    from kipi.dc.pi.model import PISetup
    ref, v = capacitor_ref(Library(LIB), "LT")
    setup = PISetup(caps=[CapModel("LT", v.capacitance_f, v.esl_h, v.esr_ohm, spice=ref)])
    path = results_io.write(tmp_path / "r.kipires", {}, setup)
    assert results_io.read(path)["data"].caps[0].spice == ref


def test_an_ac_solve_uses_the_model():
    """A SPICE model that is a series RLC solves as that RLC does; derated (DC bias) too."""
    from ac_boards import plane_pair, plane_setup
    from kipi.ac.model import CapModel
    from kipi.ac.solver import solve_ac
    snap = plane_pair(20.0, caps=[("C1", "10u X5R", (10.0, 10.0))])

    def setup(spice):
        s = plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e8, points_per_decade=5,
                        field_points_per_decade=2, max_edge_mm=2.0, min_edge_mm=0.15,
                        grading=0.6)
        s.caps.append(CapModel("LT", 10e-6, 1e-9, 3e-3, ["C1"], rated_v=6.3, spice=spice))
        return s
    ref, _ = capacitor_ref(Library(LIB), "LT")
    rlc = solve_ac(snap, setup(None), "VRM1", "GND").probes[0].z
    got = solve_ac(snap, setup(ref), "VRM1", "GND").probes[0].z
    assert got == pytest.approx(rlc, rel=1e-6)


def test_the_dialog_lists_diode_models_for_a_series_part(app):
    from kipi.gui.spice_dialog import CAPACITOR, LINK, SpiceModelDialog
    d = SpiceModelDialog(Library(DIODES, "diodes.lib"), LINK, ["D1N4148"])
    assert [d.list.item(i).text() for i in range(d.list.count())] == [
        "SCH", "D1N4148", "SS34", "IDEAL"]
    assert d.name == "D1N4148" and "0.85 V at 100 mA" in d.result.text()
    assert SpiceModelDialog(Library(DIODES), CAPACITOR).list.count() == 1
