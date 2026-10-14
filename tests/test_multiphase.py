"""Multi-phase VRMs against closed-form results.

The board is three 1 mm wide, 20 mm long strips, so every drop is a strip resistance:

    VOUT (F, y 0..1):  L1.2 at x 0..1 (phase 1), U2.1 load at 5..6, L2.2 at 19..20 (phase 2)
    VIN  (F, y 3..4):  J1.1 at 0..1, Q1.1 at 9..10 (phase 1 in), Q2.1 at 19..20 (phase 2 in)
    GND  (B, y 0..1):  J1.2 at 0..1, U2.2 at 5..6, Q1.2 at 9..10, Q2.2 at 19..20
"""

import importlib.util
import json
from pathlib import Path

import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Stackup, Zone,
)
from kipi.dc.pi.model import Load, PinRef, PISetup, Vrm, VrmPhase, validate
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver.setup import SolveSettings
from test_tree import B, F, T, R, by_name, pad, poly

I_LOAD = 2.0


def board():
    copper = [CopperLayer(F, "F.Cu", 0, T, 1.5 + T), CopperLayer(B, "B.Cu", 1, T, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * T)
    pads = [pad("l1_2", "VOUT", "L1", "2", box(0, 0, 1, 1)),
            pad("u2_1", "VOUT", "U2", "1", box(5, 0, 6, 1)),
            pad("l2_2", "VOUT", "L2", "2", box(19, 0, 20, 1)),
            pad("j1_1", "VIN", "J1", "1", box(0, 3, 1, 4)),
            pad("q1_1", "VIN", "Q1", "1", box(9, 3, 10, 4)),
            pad("q2_1", "VIN", "Q2", "1", box(19, 3, 20, 4)),
            pad("j1_2", "GND", "J1", "2", box(0, 0, 1, 1), B),
            pad("u2_2", "GND", "U2", "2", box(5, 0, 6, 1), B),
            pad("q1_2", "GND", "Q1", "2", box(9, 0, 10, 1), B),
            pad("q2_2", "GND", "Q2", "2", box(19, 0, 20, 1), B)]
    zones = [Zone("a", "VOUT", "", 0, {F: [poly(box(0, 0, 20, 1))]}),
             Zone("b", "VIN", "", 0, {F: [poly(box(0, 3, 20, 4))]}),
             Zone("c", "GND", "", 0, {B: [poly(box(0, 0, 20, 1))]})]
    nets = [Net(1, "VOUT"), Net(2, "VIN"), Net(3, "GND")]
    return BoardSnapshot("multiphase", "", st, nets, [], [], pads, [], zones, [])


def two_phase(kind="ldo", eff=0.9, iq=0.0, sense=False, ground=False):
    gnd = (lambda ref: PinRef(ref, "GND", ["2"])) if ground else (lambda ref: None)
    src = Vrm("J1", PinRef("J1", "VIN", ["1"]), 12.0, "source", ground=gnd("J1"))
    reg = Vrm("U1", PinRef("L1", "VOUT", ["2"]), 1.0, kind, efficiency=eff, quiescent_a=iq,
              input=PinRef("Q1", "VIN", ["1"]), ground=gnd("Q1"),
              sense=PinRef("U2", "VOUT", ["1"]) if sense else None,
              phases=[VrmPhase(PinRef("L2", "VOUT", ["2"]), PinRef("Q2", "VIN", ["1"]),
                               gnd("Q2"))])
    load = Load("U2", PinRef("U2", "VOUT", ["1"]), I_LOAD, ground=gnd("U2"))
    return PISetup([src, reg], [load], [], SolveSettings(max_edge_mm=0.3))


def test_phases_share_the_output_current_equally():
    res = solve_tree(board(), two_phase())
    u1 = by_name(res.vrms, "U1")
    assert [p.i_out for p in u1.phases] == pytest.approx([1.0, 1.0], rel=1e-9)
    assert u1.i_out == pytest.approx(I_LOAD, rel=1e-9)


def test_phase_voltages_follow_their_copper_and_the_mean_is_regulated():
    res = solve_tree(board(), two_phase())
    u1, ld = by_name(res.vrms, "U1"), by_name(res.loads, "U2")
    # 1 A reaches the load over 4 mm from phase 1 and over 13 mm from phase 2.
    v_load = 1.0 - (R(4) + R(13)) / 2
    assert ld.v_supply == pytest.approx(v_load, rel=1e-9)
    p1, p2 = u1.phases
    assert p1.v_out == pytest.approx(v_load + R(4), rel=1e-9)
    assert p2.v_out == pytest.approx(v_load + R(13), rel=1e-9)
    assert u1.v_out == pytest.approx(1.0, rel=1e-9)


def test_remote_sense_with_phases():
    res = solve_tree(board(), two_phase(sense=True))
    u1, ld = by_name(res.vrms, "U1"), by_name(res.loads, "U2")
    assert ld.v_supply == pytest.approx(1.0, abs=1e-9)
    assert u1.v_sense == pytest.approx(1.0, abs=1e-9)
    assert [p.v_out for p in u1.phases] == pytest.approx([1.0 + R(4), 1.0 + R(13)], rel=1e-9)


def test_each_phase_draws_its_own_input_current():
    iq = 0.02
    res = solve_tree(board(), two_phase("ldo", iq=iq))
    u1, j1 = by_name(res.vrms, "U1"), by_name(res.vrms, "J1")
    i_ph = 1.0 + iq / 2
    assert [p.i_in for p in u1.phases] == pytest.approx([i_ph, i_ph], rel=1e-9)
    assert u1.i_in == pytest.approx(2 * i_ph, rel=1e-9)
    assert j1.i_out == pytest.approx(2 * i_ph, rel=1e-9)
    # Both phases' current flows through VIN from J1 to Q1 (8 mm); phase 2's continues 9 mm.
    v1 = 12.0 - 2 * i_ph * R(8)
    assert u1.phases[0].v_in == pytest.approx(v1, rel=1e-9)
    assert u1.phases[1].v_in == pytest.approx(v1 - i_ph * R(9), rel=1e-9)


def test_switcher_phases_conserve_power_per_phase():
    eff = 0.85
    res = solve_tree(board(), two_phase("smps", eff=eff))
    u1, j1 = by_name(res.vrms, "U1"), by_name(res.vrms, "J1")
    assert not any("converge" in w for w in res.warnings)
    for p in u1.phases:
        assert p.p_in == pytest.approx(p.p_out / eff, rel=1e-8)
    # Phase 2 sits further down VIN and further from the load, so it draws more.
    assert u1.phases[1].i_in > u1.phases[0].i_in
    assert j1.i_out == pytest.approx(u1.i_in, rel=1e-9)
    assert u1.p_loss == pytest.approx(sum(p.p_loss for p in u1.phases), rel=1e-12)


def test_ground_return_per_phase():
    iq = 0.02
    res = solve_tree(board(), two_phase("ldo", iq=iq, ground=True))
    ld = by_name(res.loads, "U2")
    # The load returns 2 A at x 5..6 and each phase returns its iq/2 at its ground pins;
    # all of it flows back to J1 through x 1..5.
    assert ld.v_ground == pytest.approx((I_LOAD + iq) * R(4), rel=1e-9)


def test_source_vrm_phases_return_current_at_their_own_grounds():
    # A two-phase source: phase 1's ground is the 0 V reference; phase 2's ground takes
    # its half of the output current back.
    vrm = Vrm("U1", PinRef("L1", "VOUT", ["2"]), 1.0, "source",
              ground=PinRef("Q1", "GND", ["2"]),
              phases=[VrmPhase(PinRef("L2", "VOUT", ["2"]), ground=PinRef("Q2", "GND", ["2"]))])
    load = Load("U2", PinRef("U2", "VOUT", ["1"]), I_LOAD, ground=PinRef("U2", "GND", ["2"]))
    res = solve_tree(board(), PISetup([vrm], [load], [], SolveSettings(max_edge_mm=0.3)))
    # Ground: 2 A from U2 to Q1 over 3 mm; Q1 keeps 1 A, the other 1 A goes on to Q2.
    ld = by_name(res.loads, "U2")
    assert ld.v_ground == pytest.approx(2 * R(3), rel=1e-9)
    gnd = res.groups["GND"].result.solution
    q2 = gnd.node_v[next(iter(gnd.mesh.pad_nodes["q2_2"].values()))[0]]
    assert q2 == pytest.approx(0.0 - 1.0 * R(9), rel=1e-9)    # 1 A drawn from Q1 at 0 V
    assert by_name(res.vrms, "U1").i_out == pytest.approx(I_LOAD, rel=1e-9)


def test_single_phase_reports_no_phase_breakdown():
    s = two_phase()
    s.vrms[1].phases = []
    res = solve_tree(board(), s)
    u1 = by_name(res.vrms, "U1")
    assert u1.phases == []
    assert u1.v_out == pytest.approx(1.0, abs=1e-12)    # phase 1's pins are held directly


def test_phase_validation():
    s = two_phase(ground=True)
    assert validate(s) == []
    u1 = s.vrms[1]

    u1.phases[0].output = PinRef("L1", "VOUT", ["2"])
    assert any("phases 1 and 2 share output pins" in e for e in validate(s))
    u1.phases[0].output = PinRef("L1", "VOUT")        # all of L1's pads: overlaps too
    assert any("share output pins" in e for e in validate(s))
    u1.phases[0].output = PinRef("L2", "VIN", ["2"])
    assert any("phase 2 output must be on the same rail" in e for e in validate(s))
    u1.phases[0].output = PinRef("L2", "VOUT", ["2"])

    u1.phases[0].input = None
    assert any("phase 2 needs its input pins" in e for e in validate(s))
    u1.phases[0].input = PinRef("Q2", "VIN", ["1"])

    u1.phases[0].ground = None
    assert any("every phase or on none" in e for e in validate(s))


def test_phases_round_trip_through_json(tmp_path):
    s = two_phase(ground=True, sense=True)
    back = PISetup.load(s.save(tmp_path / "s.json"))
    assert back.vrms[1].phases == s.vrms[1].phases
    assert back.vrms[1].all_phases()[1].input == PinRef("Q2", "VIN", ["1"])


def test_phase_csv_and_html_report(tmp_path):
    from kipi.dc.pi.report import export_csv
    snap, s = board(), two_phase("smps", ground=True)
    tree = solve_tree(snap, s)
    (csv,) = [p for p in export_csv(tree, snap, tmp_path, stem="t")
              if p.name == "t_vrm_phases.csv"]
    lines = csv.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("vrm,phase,v_out_V,i_out_A")
    assert [ln.split(",")[:2] for ln in lines[1:]] == [["U1", "1"], ["U1", "2"]]

    pytest.importorskip("plotly")
    pytest.importorskip("matplotlib")
    from kipi.dc.report.html import write_report
    from kipi.dc.viz.results import build_result_scene
    scenes = {n: build_result_scene(snap, g.result) for n, g in tree.groups.items()}
    text = write_report(tmp_path / "r.html", tree, scenes, snap, s).read_text(encoding="utf-8")
    assert "VRM phases" in text


def test_single_phase_results_skip_the_phase_csv(tmp_path):
    from kipi.dc.pi.report import export_csv
    s = two_phase()
    s.vrms[1].phases = []
    paths = export_csv(solve_tree(board(), s), board(), tmp_path, stem="t")
    assert not any("phases" in p.name for p in paths)


def test_results_saved_before_phases_still_load():
    import pickle
    from kipi.dc.pi.tree import VrmReport
    r = VrmReport("U1", "ldo", "VOUT", 1.0, 1.0, None, 2.0, None, None, 2.0, None)
    blob = pickle.dumps(r)
    old = pickle.loads(blob.replace(b"phases", b"phasez"))    # as if the field never existed
    assert old.phases == []


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "multiphase_buck"


def test_example_setup_matches_its_generator():
    # Catches a generator change that was not regenerated, and GUI autosaves of edits
    # made while trying the example. (The board is not compared: zone fills come from
    # shapely, whose output can differ slightly between versions and platforms.)
    spec = importlib.util.spec_from_file_location("make_board", EXAMPLE / "make_board.py")
    make_board = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_board)
    committed = json.loads((EXAMPLE / "multiphase_buck.kipi-setup.json").read_text("utf-8"))
    assert make_board.make_setup().to_dict() == committed, (
        "examples/multiphase_buck/multiphase_buck.kipi-setup.json differs from "
        "make_board.py; run `python examples/multiphase_buck/make_board.py`")


def test_example_board_solves():
    from kipi.core.kicad_io.snapshot import load
    folder = EXAMPLE
    snap = load(folder / "multiphase_buck.kipi.json.gz")
    s = PISetup.load(folder / "multiphase_buck.kipi-setup.json")
    tree = solve_tree(snap, s)
    u1, ld = by_name(tree.vrms, "U1"), by_name(tree.loads, "U10")
    assert [p.i_out for p in u1.phases] == pytest.approx([10.0] * 4, rel=1e-9)
    # Remote sense and ground sense hold the sense balls at the set point.
    assert u1.v_sense - u1.v_ground_sense == pytest.approx(0.9, abs=1e-9)
    # The phases far from the load have to sit higher to push the same current.
    v = [p.v_out for p in u1.phases]
    assert v[0] > v[1] > v[3]
    assert ld.passed is True
    assert not tree.warnings


def test_old_setups_load_as_single_phase():
    d = two_phase().to_dict()
    for v in d["vrms"]:
        del v["phases"]
    assert all(v.phases == [] for v in PISetup.from_dict(d).vrms)


def test_input_range_is_judged_on_the_worst_phase():
    s = two_phase("ldo")
    v1 = 12.0 - 2 * R(8)                   # phase 1's input; phase 2's is 9 mm further on
    v2 = v1 - R(9)
    s.vrms[1].vin_min = (v1 + v2) / 2
    res = solve_tree(board(), s)
    assert f"{v2:.4f} V is below" in by_name(res.vrms, "U1").regulation
    assert by_name(res.loads, "U2").verdict == "VRM OUT OF REGULATION"


def test_ldo_dropout_is_judged_on_the_worst_phase():
    s = two_phase("ldo")
    v1 = 12.0 - 2 * R(8)
    v_load = 1.0 - (R(4) + R(13)) / 2
    head1, head2 = v1 - (v_load + R(4)), v1 - R(9) - (v_load + R(13))
    s.vrms[1].dropout_v = (head1 + head2) / 2  # phase 1 has the headroom, phase 2 does not
    res = solve_tree(board(), s)
    assert f"leaves {head2:.4f} V" in by_name(res.vrms, "U1").regulation
    s.vrms[1].dropout_v = head2 * 0.99
    assert by_name(solve_tree(board(), s).vrms, "U1").regulation == ""
