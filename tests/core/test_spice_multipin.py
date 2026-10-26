"""Series parts with more than two pins from their SPICE models (#193), and SPICE models at
the copper's temperature (#196)."""

import os

import numpy as np
import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Pad, Poly, Stackup, Zone,
)
from kipi.core.model.spice import Library, SpiceError, link_ref
from kipi.dc.pi.model import DcLink, Load, PinRef, PISetup, Vrm, net_groups, warmed_link
from kipi.dc.solver.setup import RHO_CU_OHM_MM as RHO, SolveSettings

PARTS = """\
* A common-mode choke: two 25 mOhm windings, coupled, with some capacitance between them
.SUBCKT CMC 1 2 3 4
R1 1 a 25m
L1 a 2 10u
R2 3 b 25m
L2 b 4 10u
K1 L1 L2 0.99
C1 1 3 5p
.ENDS
* A feedthrough capacitor: its through path, and 1 uF to its ground pin
.SUBCKT FEEDTHRU in out gnd
R1 in m 2m
R2 m out 2m
C1 m gnd 1u Rser=5m Lser=0.1n
.ENDS
* Two 40 mOhm paths from pin 1, to pin 2 and to pin 3
.SUBCKT SPLIT 1 2 3
R1 1 2 40m
R2 1 3 40m
.ENDS
.SUBCKT NOCOUPLE 1 2 3
L1 1 2 1u
K1 L1 L9 0.9
R1 1 3 1
.ENDS
"""


def test_a_common_mode_choke_reads_as_its_windings_and_their_coupling():
    lib = Library(PARTS)
    assert lib.multi_pin() == ["CMC", "FEEDTHRU", "SPLIT", "NOCOUPLE"]
    assert "CMC" not in lib.two_pin()
    m = lib.model("CMC")
    blocks = m.dc_blocks()                      # the windings are apart at DC
    assert [groups for groups, _g in blocks] == [[[0], [1]], [[2], [3]]]
    for _groups, g in blocks:
        assert g == pytest.approx(np.array([[40.0, -40.0], [-40.0, 40.0]]))
    f = np.array([1e3, 1e6, 1e8])
    y = m.admittance(f)
    assert np.abs(y.sum(axis=2)).max() < 1e-9 * np.abs(y).max()     # it floats
    assert np.allclose(y, y.transpose(0, 2, 1))                     # reciprocal
    # Common mode: both windings driven together see L (1 + k) / 2 at 1 MHz.
    i_cm = y[1][:, [0, 2]].sum(axis=1)
    z_cm = 1.0 / (i_cm[0] + i_cm[2])
    assert z_cm.imag == pytest.approx(2 * np.pi * 1e6 * 10e-6 * 1.99 / 2, rel=1e-3)
    # Differential mode: the coupling cancels nearly all of it, L (1 - k) * 2.
    v = np.array([0.5, 0, -0.5, 0])
    i = y[1] @ v
    assert (1.0 / i[0]).imag == pytest.approx(2 * np.pi * 1e6 * 10e-6 * 0.01 * 2, rel=1e-2)


def test_a_multi_pin_part_says_what_it_joins():
    lib = Library(PARTS)
    ref, v = link_ref(lib, "CMC")
    assert v.pins == 4 and [(a, b) for a, b, _r in v.paths] == [(0, 1), (2, 3)]
    assert v.resistance_ohm == pytest.approx(0.025) and ref.pins == 4
    assert ref.values["resistance"] == pytest.approx(0.025)
    ref, v = link_ref(lib, "FEEDTHRU")
    assert [(a, b) for a, b, _r in v.paths] == [(0, 1)]           # the ground pin is open
    assert v.paths[0][2] == pytest.approx(0.004)
    assert ref.admittance(2 * np.pi * np.array([1e6])).shape == (1, 3, 3)
    with pytest.raises(SpiceError, match="L9, which is not an inductor"):
        lib.model("NOCOUPLE")
    with pytest.raises(SpiceError, match="two-pin models only"):
        Library(".model DX D(IS=1n)\n.subckt T 1 2 3\nD1 1 2 DX\nR1 2 3 1\n.ends\n"
                ).model("T")


def _choke_link(lib=None):
    ref, _v = link_ref(lib or Library(PARTS), "CMC")
    return DcLink("L1", PinRef("L1", "VBUS_IN", ["1"]), PinRef("L1", "VBUS", ["2"]), 0.025,
                  spice=ref, more_sides=[PinRef("L1", "GND_IN", ["3"]),
                                         PinRef("L1", "GND", ["4"])])


def test_a_choke_joins_each_winding_s_nets_not_the_rails():
    k = _choke_link()
    assert k.is_multi and [p.net for p in k.sides] == ["VBUS_IN", "VBUS", "GND_IN", "GND"]
    assert k.joins() == [("VBUS_IN", "VBUS"), ("GND_IN", "GND")]
    groups = net_groups(PISetup(links=[k]))
    assert groups["VBUS"] == {"VBUS_IN", "VBUS"} and groups["GND"] == {"GND_IN", "GND"}
    k.spice.use = False                         # its typed values, between A and B only
    assert not k.is_multi and k.sides == [k.side_a, k.side_b]
    assert "GND" not in net_groups(PISetup(links=[k]))


def test_a_multi_pin_link_goes_in_a_setup_file(tmp_path):
    s = PISetup(links=[_choke_link()])
    s.links[0].temp_c = 60.0                    # never saved
    path = s.save(tmp_path / "x.kipi-setup.yml")
    back = PISetup.load(path)
    assert back.links[0].more_sides == s.links[0].more_sides and back.links[0].is_multi
    assert back.links[0].temp_c is None


# ------------------------------------------------------------------ DC
F, B = 3, 34
T = 0.035


def _poly(g):
    return Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])


def _pad(pid, net, ref, num, g):
    return Pad(pid, net, ref, num, (g.centroid.x, g.centroid.y), "smd", shapes={F: [_poly(g)]})


def _board():
    """Net A: strip x 0..10; net B: strip x 12..22 (1 mm wide). R1 bridges the gap, its
    pins 2 and 3 one pad's place on B."""
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [_pad("src", "A", "J1", "1", box(0, 0, 1, 1)),
            _pad("r1a", "A", "R1", "1", box(9, 0, 10, 1)),
            _pad("r1b", "B", "R1", "2", box(12, 0, 13, 1)),
            _pad("r1c", "B", "R1", "3", box(12, 0, 13, 1)),
            _pad("ld", "B", "U1", "1", box(21, 0, 22, 1))]
    zones = [Zone("za", "A", "", 0, {F: [_poly(box(0, 0, 10, 1))]}),
             Zone("zb", "B", "", 0, {F: [_poly(box(12, 0, 22, 1))]})]
    return BoardSnapshot("multi", "", st, [Net(1, "A"), Net(2, "B")], [], [], pads, [],
                         zones, [])


def _dc_setup(link):
    return PISetup([Vrm("J1", PinRef("J1", "A", ["1"]), 1.0, "source")],
                   [Load("U1", PinRef("U1", "B", ["1"]), 2.0)], [link],
                   SolveSettings(max_edge_mm=0.3))


def test_the_dc_solve_takes_the_part_s_conductance_between_its_sides():
    from kipi.dc.pi.tree import solve_tree
    ref, _v = link_ref(Library(PARTS), "SPLIT")
    link = DcLink("R1", PinRef("R1", "A", ["1"]), PinRef("R1", "B", ["2"]), 0.04,
                  spice=ref, more_sides=[PinRef("R1", "B", ["3"])])
    res = solve_tree(_board(), _dc_setup(link))
    (ld,) = res.loads
    strip = RHO * 8 / (1.0 * T)
    expected = 2.0 * (strip + 0.02 + strip)     # the two 40 mΩ paths side by side
    assert 1.0 - ld.v_supply == pytest.approx(expected, rel=1e-6)
    (group,) = res.groups.values()
    (sr,) = group.result.fields.series
    assert sr.current == pytest.approx(2.0, rel=1e-9)
    assert sr.power == pytest.approx(2.0 ** 2 * 0.02, rel=1e-6)
    assert group.result.fields.summary["energy_balance_error"] < 1e-9


# ------------------------------------------------------------------ AC
def test_an_ac_solve_terminates_a_multi_pin_part_by_its_admittance():
    """A three-pin part that is a series RLC from pin 1 to pin 2 (its pin 3 all but open)
    solves as that RLC as a capacitor does."""
    from ac_boards import plane_pair, plane_setup
    from kipi.ac.model import CapModel
    from kipi.ac.solver import solve_ac
    snap = plane_pair(20.0, caps=[("C1", "10u X5R", (10.0, 10.0))])

    def setup():
        return plane_setup(snap, f_start_hz=1e4, f_stop_hz=1e8, points_per_decade=5,
                           field_points_per_decade=2, max_edge_mm=2.0, min_edge_mm=0.15,
                           grading=0.6)
    s = setup()
    s.caps.append(CapModel("C", 10e-6, 1e-9, 3e-3, ["C1"]))
    rlc = solve_ac(snap, s, "VRM1", "GND").probes[0].z
    lib = Library(".subckt C3 1 2 3\nC1 1 a 10u\nL1 a b 1n\nR1 b 2 3m\nR2 2 3 1e9\n.ends\n")

    from kipi.core.model.spice import SpiceRef
    ref = SpiceRef("C3", lib.netlist("C3"))
    s = setup()
    s.links.append(DcLink("C1", PinRef("C1", "+3V3", ["1"]), PinRef("C1", "GND", ["2"]), 1.0,
                          spice=ref, more_sides=[PinRef("U1", "GND", ["2"])]))
    res = solve_ac(snap, s, "VRM1", "GND")
    assert res.links == ["C1"]
    assert res.probes[0].z == pytest.approx(rlc, rel=1e-4)


def test_the_termination_handles_a_choke_s_floating_windings():
    """Admittances in place of impedances: the same answer where both exist, and an
    answer where a part's ports have no impedance matrix."""
    from kipi.ac.solver.fem import PortDef
    from kipi.ac.solver.sweep import LinkPin, terminate
    rng = np.random.default_rng(3)
    a = rng.normal(size=(4, 4)) + 1j * rng.normal(size=(4, 4))
    zb = (a @ a.conj().T + 4 * np.eye(4))[None]          # a passive board, one frequency
    freqs = np.array([1e6])
    split = DcLink("R1", PinRef("R1", "A", ["1"]), PinRef("R1", "B", ["2"]), 0.04,
                   spice=link_ref(Library(PARTS), "SPLIT")[0],
                   more_sides=[PinRef("R1", "B", ["3"])])

    class R:
        def __init__(self, ohms):
            self.ohms = ohms

        def impedance(self, omega):
            return np.full(np.shape(omega), self.ohms, complex)
    probe = PortDef("P", "probe", [], [])
    plain = [probe, PortDef("a", "link", [], [], R(0.04)), PortDef("b", "link", [], [], R(0.04)),
             PortDef("c", "cap", [], [], R(2.0))]
    multi = [probe, PortDef("a", "link", [], [], LinkPin(split, 0)),
             PortDef("b", "link", [], [], LinkPin(split, 1)), plain[3]]
    assert terminate(zb, freqs, multi) == pytest.approx(terminate(zb, freqs, plain))
    choke = _choke_link()
    ports = [probe] + [PortDef(f"{i}", "link", [], [], LinkPin(choke, i)) for i in range(3)]
    z = terminate(zb, freqs, ports)
    assert np.all(np.isfinite(z)) and z[0, 0].real > 0


# ------------------------------------------------------------------ temperature
def test_a_model_follows_its_temperature():
    lib = Library(".subckt R 1 2\nR1 1 2 1 tc1=0.004 tc2=1e-6\n.ends\n"
                  ".subckt RT 1 2\nR1 1 2 {1 + 0.01 * (temp - 27)}\n.ends\n"
                  ".subckt L 1 2\nR1 1 m 1 TC=0.002,0\nL1 m 2 1u\n.ends\n"
                  ".model DX D(IS=1n N=1.5 RS=0.1 TRS1=0.005 BV=10 TBV1=-0.001)\n")
    assert lib.model("R").dc_resistance() == pytest.approx(1.0)
    assert lib.model("R", 127.0).dc_resistance() == pytest.approx(1.0 + 0.4 + 0.01)
    assert lib.model("RT", 77.0).dc_resistance() == pytest.approx(1.5)
    assert lib.model("L", 77.0).impedance([1.0])[0].real == pytest.approx(1.1, rel=1e-6)
    cold, hot = lib.model("DX", 27.0), lib.model("DX", 85.0)
    v_cold, v_hot = cold.operating_point(1e-3)[0], hot.operating_point(1e-3)[0]
    assert -3e-3 < (v_hot - v_cold) / 58 < -1e-3       # about -2 mV/°C
    rs = [e.value for e in hot.elements if e.kind == "R"]
    assert rs == pytest.approx([0.1 * (1 + 0.005 * 58)])
    assert [e.diode.bv for e in hot.elements if e.kind == "D"] == pytest.approx(
        [10 * (1 - 0.001 * 58)])


def test_a_link_is_at_the_copper_s_temperature_under_its_pads():
    from kipi.dc.solver import pads_for
    lib = Library(".subckt R 1 2\nR1 1 2 0.1 tc1=0.004\n.ends\n")
    ref, _v = link_ref(lib, "R")
    k = DcLink("R1", PinRef("R1", "A", ["1"]), PinRef("R1", "B", ["2"]), 0.1, spice=ref)
    snap = _board()

    def pads(p):
        return [x.id for x in pads_for(snap, p.net, p.ref, p.pins or None)]

    def temperature(xy):                        # 60 °C at R1.1 (x 9.5), 80 °C at R1.2
        return 60.0 + 20.0 * (np.atleast_2d(xy)[:, 0] - 9.5) / 3.0
    w = warmed_link(snap, k, pads, temperature)
    assert w.temp_c == pytest.approx(70.0) and k.temp_c is None
    assert w.dc_resistance() == pytest.approx(0.1 * (1 + 0.004 * 43))
    assert k.dc_resistance() == 0.1 and warmed_link(snap, k, pads, None) is k
    z_hot = w.impedance(np.array([1.0]))[0]
    assert z_hot.real == pytest.approx(0.1 * (1 + 0.004 * 43))


def test_the_dc_solve_warms_a_link_s_model_with_the_copper():
    from kipi.dc.pi.tree import solve_tree
    lib = Library(".subckt R 1 2\nR1 1 2 0.05 tc1=0.004\n.ends\n")
    ref, _v = link_ref(lib, "R")
    link = DcLink("R1", PinRef("R1", "A", ["1"]), PinRef("R1", "B", ["2"]), 0.05, spice=ref)
    cold = solve_tree(_board(), _dc_setup(link))
    hot = solve_tree(_board(), _dc_setup(link),
                     temperature=lambda xy: np.full(len(np.atleast_2d(xy)), 102.0))
    sr = [next(iter(r.groups.values())).result.fields.series[0] for r in (cold, hot)]
    assert sr[0].resistance == pytest.approx(0.05)
    assert sr[1].resistance == pytest.approx(0.05 * (1 + 0.004 * 75))


# ------------------------------------------------------------------ GUI
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_dialog_maps_a_multi_pin_model_s_pins_to_the_part_s(app):
    from qtpy.QtWidgets import QDialogButtonBox
    from kipi.gui.spice_dialog import LINK, SpiceModelDialog
    pins = [("1", "VBUS_IN"), ("2", "VBUS"), ("3", "GND_IN"), ("4", "GND")]
    d = SpiceModelDialog(Library(PARTS, "parts.lib"), LINK, ["CMC"], pins=pins)
    names = [d.list.item(i).text() for i in range(d.list.count())]
    assert "CMC" in names and "SPLIT" in names
    assert d.name == "CMC" and "4 pins, joined at DC: 1–2 25 mΩ, 3–4 25 mΩ" in d.result.text()
    assert d.pins == ["1", "2", "3", "4"] and not d.pin_box.isHidden()
    d.combos[3].setCurrentIndex(0)              # two model pins on one part pin
    assert not d.buttons.button(QDialogButtonBox.Ok).isEnabled()
    d.combos[3].setCurrentIndex(3)
    assert d.buttons.button(QDialogButtonBox.Ok).isEnabled()
    # Without the part's pins (or with too few), only two-pin models are offered.
    d = SpiceModelDialog(Library(PARTS), LINK, pins=pins[:2])
    assert d.list.count() == 0


def test_the_link_editor_sets_the_part_s_sides_from_the_mapping(app, monkeypatch):
    from kipi.dc.gui import pi_panel
    from kipi.core.model.board import Footprint
    from synthetic import make_board

    def fake(parent, kind, hints, pins=()):
        assert ("3", "GND_IN") in pins
        return (*link_ref(Library(PARTS, "parts.lib"), "CMC"), ["1", "2", "3", "4"])
    monkeypatch.setattr(pi_panel, "import_spice", fake)
    snap = make_board()
    snap.footprints.append(Footprint("f9", "L1", "ACM2012", (0, 0), "top"))
    for i, net in enumerate(["VBUS_IN", "VBUS", "GND_IN", "GND"], 1):
        snap.pads.append(Pad(f"l1_{i}", net, "L1", str(i), (float(i), 0.0), "smd"))
    p = pi_panel.PIPanel()
    p.snapshot = snap
    ed = p.link_list.editor
    k = DcLink("L1", PinRef("L1", "VBUS_IN", ["1"]), PinRef("L1", "VBUS", ["2"]), 0.0)
    ed.load(k)
    ed._from_spice()
    assert k.is_multi and [p.net for p in k.sides] == ["VBUS_IN", "VBUS", "GND_IN", "GND"]
    assert k.more_sides[0] == PinRef("L1", "GND_IN", ["3"])
    assert "C L1[3] · GND_IN" in ed.lbl_spice.text()
    assert "VBUS_IN ↔ VBUS ↔ GND_IN ↔ GND (4 pins" in p._describe_link(k)
    p.close()
