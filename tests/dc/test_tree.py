"""Power-tree solves against closed-form results."""

import math

import pytest
from shapely.geometry import box

from kipi.dc.pi.model import (
    DcLink, Load, PinRef, PISetup, Vrm, net_groups, validate, vin_range,
)
from kipi.dc.pi.tree import NOT_CONVERGED, OUT_OF_REGULATION, solve_tree
from kipi.dc.solver.fem import SolveError

from tree_boards import R, board, by_name, pad, setup


def test_ldo_chain():
    res = solve_tree(board(), setup("ldo", iq=0.01))
    u1, j1 = by_name(res.vrms, "U1"), by_name(res.vrms, "J1")
    assert u1.i_out == pytest.approx(1.0, rel=1e-9)
    assert u1.i_in == pytest.approx(1.01, rel=1e-9)
    assert j1.i_out == pytest.approx(1.01, rel=1e-9)
    assert u1.v_in == pytest.approx(12.0 - 1.01 * R(8), rel=1e-9)
    ld = by_name(res.loads, "U2")
    assert ld.v_supply == pytest.approx(3.3 - 1.0 * R(8), rel=1e-9)
    assert ld.passed is True


def test_switcher_input_current_follows_input_voltage():
    eff = 0.8
    res = solve_tree(board(), setup("smps", eff=eff))
    u1 = by_name(res.vrms, "U1")
    p_out = u1.v_out * u1.i_out
    r = R(8)
    # I_in (12 - r I_in) = P_out / eff  ->  r I^2 - 12 I + P/eff = 0
    i_in = (12 - math.sqrt(144 - 4 * r * p_out / eff)) / (2 * r)
    assert u1.i_in == pytest.approx(i_in, rel=1e-8)
    assert u1.p_in == pytest.approx(p_out / eff, rel=1e-8)
    assert by_name(res.vrms, "J1").i_out == pytest.approx(i_in, rel=1e-8)


def test_remote_sense_regulates_at_the_sense_pins():
    res = solve_tree(board(), setup("ldo", sense=True))
    u1 = by_name(res.vrms, "U1")
    assert u1.v_sense == pytest.approx(3.3, abs=1e-9)
    assert u1.v_out == pytest.approx(3.3 + 1.0 * R(8), rel=1e-9)
    assert by_name(res.loads, "U2").v_supply == pytest.approx(3.3, abs=1e-9)


def test_ground_return_drop():
    res = solve_tree(board(), setup("ldo", iq=0.01, ground=True))
    ld = by_name(res.loads, "U2")
    # Ground: 1.01 A from U1's pad back to J1 over 9 mm, then 1 A from U2 to U1 over 10 mm.
    v_u1_gnd = 1.01 * R(9)
    assert ld.v_ground == pytest.approx(v_u1_gnd + 1.0 * R(10), rel=1e-9)
    assert ld.v_effective == pytest.approx(ld.v_supply - ld.v_ground, abs=1e-15)
    assert "GND" in res.groups and res.groups["GND"].kind == "ground"


def test_ground_sense_regulates_against_the_load_ground():
    res = solve_tree(board(), setup("ldo", iq=0.01, sense=True, ground=True, gsense=True))
    u1, ld = by_name(res.vrms, "U1"), by_name(res.loads, "U2")
    v_gnd = 1.01 * R(9) + 1.0 * R(10)
    assert u1.v_ground_sense == pytest.approx(v_gnd, rel=1e-9)
    assert u1.v_out == pytest.approx(3.3 + 1.0 * R(8) + v_gnd, rel=1e-9)
    assert ld.v_effective == pytest.approx(3.3, abs=1e-9)
    assert ld.drop == pytest.approx(0.0, abs=1e-9)


def test_ground_sense_without_positive_sense():
    res = solve_tree(board(), setup("ldo", iq=0.01, ground=True, gsense=True))
    u1 = by_name(res.vrms, "U1")
    assert u1.v_sense is None
    assert u1.v_out == pytest.approx(3.3 + 1.01 * R(9) + 1.0 * R(10), rel=1e-9)


def test_ground_sense_with_switcher_converges():
    # A switcher's ground current depends on its output voltage, which depends on the
    # ground sense voltage: the rails and grounds must be iterated together.
    res = solve_tree(board(), setup("smps", eff=0.8, sense=True, ground=True, gsense=True))
    assert not any("converge" in w for w in res.warnings)
    assert by_name(res.loads, "U2").v_effective == pytest.approx(3.3, abs=1e-8)


def test_validation_rules():
    s = setup()
    assert validate(s) == []
    # Joining VIN and +3V3 with a DC connection puts two VRMs on one group.
    s.links.append(DcLink("R9", PinRef("U1", "VIN", ["1"]), PinRef("U1", "+3V3", ["2"]), 0.1))
    errs = validate(s)
    assert any("Only one VRM" in e for e in errs)
    assert net_groups(s)["VIN"] == net_groups(s)["+3V3"]
    s = setup()
    s.vrms[1].input = None
    assert any("needs its input pins" in e for e in validate(s))
    s = setup()
    s.vrms[0].kind = "ldo"
    s.vrms[0].input = PinRef("U2", "+3V3", ["1"])          # J1 fed from U1's output: a loop
    assert any("loop" in e for e in validate(s))
    s = setup(ground=True)
    s.vrms[0].ground = None                                # ground without a reference
    assert any("needs a reference" in e for e in validate(s))
    with pytest.raises(SolveError, match="Setup problems"):
        solve_tree(board(), s)
    s = setup(ground=True, gsense=True)
    assert validate(s) == []
    s.vrms[1].ground = None
    assert any("ground sense needs" in e for e in validate(s))
    s = setup(ground=True, gsense=True)
    s.vrms[1].ground_sense = PinRef("U2", "+3V3", ["1"])
    assert any("ground sense pins must be on" in e for e in validate(s))


def test_setup_file_round_trip(tmp_path):
    s = setup(kind="smps", eff=0.85, sense=True, ground=True, gsense=True)
    s.links.append(DcLink("FB1", PinRef("U2", "+3V3", ["1"]), PinRef("U2", "+3V3", ["1"]), 0.05))
    from kipi.dc.pi.model import PinGroup
    s.loads[0].groups = [PinGroup(["1"], 0.4, "a")]
    s.settings.rho_by_layer = {"In1.Cu": 1.9e-5}
    s.settings.tcr_per_c = 0.0038
    path = s.save(tmp_path / "x.kipi-setup.json")
    assert path.name == "x.kipi-setup.yml"           # YAML only from 0.8.0 (#76)
    assert PISetup.load(path) == s


def test_legacy_json_setups_still_load(tmp_path):
    import json
    s = setup(kind="smps", eff=0.85, sense=True, ground=True)
    legacy = tmp_path / "x.kipi-setup.json"
    legacy.write_text(json.dumps(s.to_dict(), indent=2), encoding="utf-8")
    assert PISetup.load(legacy) == s
    from kipi.dc.pi.model import find_setup
    assert find_setup("x.kicad_pcb", str(tmp_path)) == legacy
    s.save(legacy)
    assert find_setup("x.kicad_pcb", str(tmp_path)) == tmp_path / "x.kipi-setup.yml"


def test_hand_edited_yaml_reads_numbers_as_numbers(tmp_path):
    from kipi.core import yaml_io
    d = yaml_io.loads("a: 1e-9\nb: 2.5E+3\nc: 3\nd: '1e-9'\ne: .5\nf: 1_000.5\n")
    assert d == {"a": 1e-9, "b": 2500.0, "c": 3, "d": "1e-9", "e": 0.5, "f": 1000.5}


def test_without_pyyaml_setups_stay_json(tmp_path, monkeypatch):
    from kipi.core import yaml_io
    from kipi.dc.pi.model import setup_path
    monkeypatch.setattr(yaml_io, "yaml", None)
    s = setup()
    assert setup_path("x.kicad_pcb", str(tmp_path)).name == "x.kipi-setup.json"
    path = s.save(tmp_path / "x.kipi-setup.yml")
    assert path.name == "x.kipi-setup.json" and PISetup.load(path) == s
    (tmp_path / "y.kipi-setup.yml").write_text("nets: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="PyYAML"):
        PISetup.load(tmp_path / "y.kipi-setup.yml")


def test_resistivity_must_be_positive():
    s = setup()
    s.settings.rho_by_layer = {"F.Cu": 0.0}
    assert any("positive on F.Cu" in e for e in validate(s))
    s.settings.rho_by_layer = {}
    s.settings.rho_ohm_mm = -1.0
    assert any("resistivity must be positive" in e for e in validate(s))


def board_two_pin():
    """board() plus a second +3V3 pin on U2 halfway along the strip (x 16..17)."""
    b = board()
    b.pads.append(pad("u2_3", "+3V3", "U2", "3", box(16, 0, 17, 1)))
    b.invalidate_index()
    return b


def test_per_pin_group_currents():
    from kipi.dc.pi.model import PinGroup
    s = setup("ldo")
    s.loads[0] = Load("U2", PinRef("U2", "+3V3", []), 0.0, tol_min=-0.05, tol_max=0.05,
                      groups=[PinGroup(["3"], 0.3, "mid"), PinGroup(["1"], 0.7, "end")])
    res = solve_tree(board_two_pin(), s)
    ld = by_name(res.loads, "U2")
    assert ld.current == pytest.approx(1.0)
    v_mid = 3.3 - 1.0 * R(3)              # all 1 A flows from U1.2 (x=13) to the mid pin (x=16)
    v_end = v_mid - 0.7 * R(4)            # 0.7 A continues to the end pin (x=21)
    groups = {g[0]: g for g in ld.pin_groups}
    assert groups["mid"][2] == pytest.approx(v_mid, rel=1e-9)
    assert groups["end"][2] == pytest.approx(v_end, rel=1e-9)
    assert ld.v_worst == pytest.approx(v_end, rel=1e-9)
    assert ld.drop_worst > ld.drop                      # mean hides the worst pin
    assert ld.passed == (100 * (3.3 - v_end) / 3.3 <= 0.05)
    assert by_name(res.vrms, "U1").i_out == pytest.approx(1.0, rel=1e-9)

    # The same total split equally gives a different (and wrong for this part) answer.
    s2 = setup("ldo")
    s2.loads[0] = Load("U2", PinRef("U2", "+3V3", []), 1.0)
    eq = by_name(solve_tree(board_two_pin(), s2).loads, "U2")
    assert eq.v_worst == pytest.approx(v_mid - 0.5 * R(4), rel=1e-9)


def _sensed_at_end(*loads):
    """board_two_pin() with U1 sensing at U2's end pin (x 21) and the given loads."""
    s = setup("ldo", sense=True)
    s.loads[:] = loads
    return solve_tree(board_two_pin(), s)


def test_pins_before_the_sense_point_are_boosted():
    from kipi.dc.pi.tree import fmt_drop
    boost = 1.0 * R(4)               # END's 1 A flows from the mid pin (x 17) to the end (x 21)
    pct = 100 * boost / 3.3
    for tol, ok in ((0.9 * pct, False), (1.1 * pct, True)):
        res = _sensed_at_end(Load("END", PinRef("U2", "+3V3", ["1"]), 1.0),
                                Load("MID", PinRef("U2", "+3V3", ["3"]), 0.2, tol_min=-tol, tol_max=tol))
        mid = by_name(res.loads, "MID")
        assert by_name(res.loads, "END").v_supply == pytest.approx(3.3, abs=1e-9)
        assert mid.v_supply == pytest.approx(3.3 + boost, rel=1e-9)
        assert mid.v_worst == mid.v_high == mid.v_low       # one pin
        assert mid.drop == pytest.approx(-boost, rel=1e-6)
        assert mid.drop_worst_pct == pytest.approx(-pct, rel=1e-6)
        assert fmt_drop(mid.drop) == f"{boost * 1e3:.3f} mV boost"
        assert mid.passed is ok                             # judged on +/- the tolerance


def test_worst_pin_is_furthest_from_nominal_either_way():
    # One load on both pins, sensed at the end pin: the end sits at 3.3 V, the mid pin above.
    res = _sensed_at_end(Load("U2", PinRef("U2", "+3V3", ["1", "3"]), 1.0, tol_min=-1.0, tol_max=1.0))
    ld = by_name(res.loads, "U2")
    assert ld.v_low == pytest.approx(3.3, abs=1e-9)
    assert ld.v_high == pytest.approx(3.3 + 0.5 * R(4), rel=1e-9)
    assert ld.v_worst == ld.v_high and ld.drop_worst < 0


def test_results_saved_before_boosts_still_load():
    import pickle
    from kipi.dc.pi.tree import LoadReport
    old = LoadReport("U2", "+3V3", 1.0, 3.3, 3.29, 3.28, None, 3.29, 0.01, 1.0, True, 3.28,
                     [("a", 1.0, 3.29, 3.28)])
    del old.v_low, old.v_high                               # as a file from before them
    back = pickle.loads(pickle.dumps(old))
    assert back.v_low == 3.28 and back.v_high is None
    assert back.pin_groups == [("a", 1.0, 3.29, 3.28, None)]


def test_pin_group_validation():
    from kipi.dc.pi.model import PinGroup
    s = setup()
    s.loads[0].groups = [PinGroup(["1"], 0.5), PinGroup(["1"], 0.5)]
    assert any("more than one group" in e for e in validate(s))
    s.loads[0].groups = [PinGroup([], 0.5)]
    assert any("has no pins" in e for e in validate(s))
    s.loads[0].supply = PinRef("U2", "+3V3", ["1"])
    s.loads[0].groups = [PinGroup(["3"], 0.5)]
    assert any("not among its supply pins" in e for e in validate(s))


def test_negative_rail_is_judged_on_its_magnitude():
    # -3.3 V rail; the load returns 1 A, so it sits R(8) * 1 A (~3.9 mV, 0.12 %) off.
    s = setup()
    s.vrms = [Vrm("U1", PinRef("U1", "+3V3", ["2"]), -3.3, "source")]
    s.loads[0].current = -1.0
    for tol, verdict in ((0.1, False), (0.2, True)):
        s.loads[0].tol_min, s.loads[0].tol_max = -tol, tol
        ld = by_name(solve_tree(board(), s).loads, "U2")
        assert abs(ld.nominal - ld.v_worst) == pytest.approx(R(8), rel=1e-6)
        assert ld.passed is verdict
    assert abs(ld.drop_worst_pct) == pytest.approx(100 * R(8) / 3.3, rel=1e-6)
    assert math.copysign(1, ld.drop_worst_pct) == math.copysign(1, ld.drop_worst)


def test_terminals_report_the_currents_the_tree_solved_with():
    # VRM input, phase and ground-return loads start at 0 A and get their currents per
    # solve; the per-group summaries must balance with those.
    res = solve_tree(board(), setup("smps", eff=0.85, iq=0.01, ground=True))
    for g in res.groups.values():
        sm = g.result.fields.summary
        assert sm["kcl_error"] < 1e-9, g.name
        assert sm["energy_balance_error"] < 1e-6, g.name
    u1 = by_name(res.vrms, "U1")
    vin = res.groups["VIN"].result.fields
    assert by_name(vin.terminals, "U1 (input)").current == pytest.approx(u1.i_in, rel=1e-12)
    gnd = res.groups["GND"].result.fields
    assert by_name(gnd.terminals, "U1 (gnd)").current == pytest.approx(u1.i_out - u1.i_in,
                                                                         rel=1e-12)


def test_load_above_nominal_fails_its_limit():
    # Remote sense at U2 lifts U1's output by the strip drop; a load on U1's output pad
    # sits above nominal by 1 A x R(8) (0.12 %), outside a 0.05 % limit.
    s = setup("ldo", sense=True)
    s.loads.append(Load("U1out", PinRef("U1", "+3V3", ["2"]), 0.0, tol_min=-0.05, tol_max=0.05))
    ld = by_name(solve_tree(board(), s).loads, "U1out")
    assert ld.drop_worst_pct == pytest.approx(-100 * R(8) / 3.3, rel=1e-6)
    assert ld.passed is False


def test_ldo_in_dropout_fails_the_loads_it_feeds():
    s = setup("ldo")
    s.vrms[1].dropout_v = 9.0                  # 12 V in, 3.3 V out: 8.7 V of headroom
    res = solve_tree(board(), s)
    assert "dropout" in by_name(res.vrms, "U1").regulation
    ld = by_name(res.loads, "U2")
    assert ld.passed is False and ld.verdict == OUT_OF_REGULATION


def test_switcher_below_its_input_minimum_fails_the_loads_it_feeds():
    s = setup("smps", eff=0.9)
    s.vrms[1].vin_min = 13.0
    res = solve_tree(board(), s)
    assert "minimum" in by_name(res.vrms, "U1").regulation
    assert by_name(res.loads, "U2").verdict == OUT_OF_REGULATION


def test_input_range_in_percent_is_of_the_input_rails_vrm():
    s = setup("smps", eff=0.9)
    u1 = s.vrms[1]
    u1.vin_pct, u1.vin_min, u1.vin_max = True, -10.0, 5.0          # of J1's 12 V
    assert vin_range(s, u1) == pytest.approx((10.8, 12.6))
    assert not validate(s)
    assert by_name(solve_tree(board(), s).vrms, "U1").regulation == ""
    u1.vin_min, u1.vin_max = 10.0, 15.0                            # 13.2 V: above 12 V
    res = solve_tree(board(), s)
    assert "below its 13.2 V minimum" in by_name(res.vrms, "U1").regulation
    assert by_name(res.loads, "U2").verdict == OUT_OF_REGULATION
    s.vrms[0].voltage = -12.0                  # -10 % of a negative rail is its high end
    u1.vin_min, u1.vin_max = -10.0, 5.0
    assert vin_range(s, u1) == pytest.approx((-12.6, -10.8))
    assert PISetup.from_dict(s.to_dict()).vrms[1].vin_pct is True


def test_load_tolerance_min_and_max_apart_in_percent_or_volts():
    from kipi.dc.pi.model import tolerance_label
    s = setup("ldo")                           # U2 sees a little under 3.3 V
    u2 = s.loads[0]
    v = by_name(solve_tree(board(), s).loads, "U2").v_low
    assert 3.2 < v < 3.3
    pct = (v / 3.3 - 1) * 100
    for lo, hi, unit, passed in ((pct - 0.01, 0.0, True, True),       # just inside
                                 (pct + 0.01, 10.0, True, False),     # min just above it
                                 (None, 0.0, True, True), (None, -50.0, True, False),
                                 (v - 1e-4, None, False, True), (v + 1e-4, 5.0, False, False),
                                 (None, 3.0, False, False)):
        u2.tol_min, u2.tol_max, u2.tol_pct = lo, hi, unit
        ld = by_name(solve_tree(board(), s).loads, "U2")
        assert ld.passed is passed, (lo, hi, unit)
        assert ld.tolerance == u2.tolerance_label()
    assert (ld.limit_low, ld.limit_high) == (None, 3.0)
    u2.tol_min = u2.tol_max = None
    assert by_name(solve_tree(board(), s).loads, "U2").passed is None    # not judged
    assert [tolerance_label(*a) for a in ((-5, 5, True), (-3, 5, True), (None, 5, True),
                                          (3.1, 3.5, False), (3.1, None, False),
                                          (None, None, True))] == [
        "±5 %", "-3 / +5 %", "max +5 %", "3.1 to 3.5 V", "min 3.1 V", ""]
    u2.tol_min, u2.tol_max = 1.0, -1.0
    assert any("tolerance minimum is above its maximum" in e for e in validate(s))


def test_old_symmetric_tolerances_still_load():
    import pickle
    from kipi.dc.pi.results import LoadReport
    d = setup("ldo").to_dict()
    for ld in d["loads"]:
        for k in ("tol_min", "tol_max", "tol_pct"):
            del ld[k]
        ld["tolerance_pct"] = 2.0
    u2 = PISetup.from_dict(d).loads[0]
    assert (u2.tol_min, u2.tol_max, u2.tol_pct) == (-2.0, 2.0, True)
    old = Load("U2", PinRef("U2", "+3V3", ["1"]), 1.0)          # pickled before: one %
    state = {k: v for k, v in old.__dict__.items() if not k.startswith("tol_")}
    state["tolerance_pct"] = 1.5
    old.__setstate__(state)
    assert (old.tol_min, old.tol_max, old.tol_pct) == (-1.5, 1.5, True)
    rep = by_name(solve_tree(board(), setup("ldo")).loads, "U2")
    state = dict(rep.__dict__)
    for k in ("tolerance", "limit_low", "limit_high"):
        del state[k]
    state["tolerance_pct"] = 1.0
    rep2 = LoadReport.__new__(LoadReport)
    rep2.__setstate__(state)
    assert rep2.tolerance == "±1 %"
    assert (rep2.limit_low, rep2.limit_high) == pytest.approx((3.267, 3.333))
    assert pickle.loads(pickle.dumps(rep)).tolerance == rep.tolerance


def test_input_range_in_percent_needs_a_vrm_on_the_input_rail():
    s = setup("smps", eff=0.9)
    s.vrms[1].vin_pct, s.vrms[1].vin_max = True, 10.0
    del s.vrms[0]
    assert vin_range(s, s.vrms[0]) == (None, None)
    assert any("in % needs a VRM" in e for e in validate(s))


def test_unconverged_group_leaves_its_loads_unjudged(monkeypatch):
    import kipi.dc.pi.tree as tree
    monkeypatch.setattr(tree, "MAX_TREE_ITERATIONS", 1)
    s = setup("smps", eff=0.9)
    s.loads.append(Load("U1in", PinRef("U1", "VIN", ["1"]), 0.0, tol_min=-50.0, tol_max=50.0))
    res = solve_tree(board(), s)
    ld = by_name(res.loads, "U1in")
    assert ld.passed is None and ld.verdict == NOT_CONVERGED
    assert res.groups["VIN"].converged is False
    assert by_name(res.loads, "U2").passed is True     # its own rail converged


def test_ranges_in_volts_from_nominal():
    """A third unit beside V and % (#131): volts below and above nominal."""
    from kipi.dc.pi.model import DELTA_VOLTS, PERCENT, VOLTS, range_volts, tolerance_label, volts_in
    assert range_volts(3.3, -0.1, 0.2, DELTA_VOLTS) == pytest.approx((3.2, 3.5))
    assert range_volts(-5.0, -0.1, 0.2, DELTA_VOLTS) == pytest.approx((-5.1, -4.8))
    assert range_volts(None, -0.1, 0.2, DELTA_VOLTS) == (None, None)
    assert range_volts(None, 3.0, None, VOLTS) == (3.0, None)
    for unit in (VOLTS, DELTA_VOLTS, PERCENT):
        lo, hi = range_volts(3.3, -1.0, None, unit)
        assert volts_in(3.3, lo, hi, unit) == (pytest.approx(-1.0), None)
    assert [tolerance_label(*a) for a in ((-0.1, 0.1, False, True), (-0.05, 0.1, False, True),
                                          (None, 0.1, False, True))] == [
        "±0.1 V", "-0.05 / +0.1 V", "max +0.1 V"]

    s = setup("ldo")
    u2 = s.loads[0]
    v = by_name(solve_tree(board(), s).loads, "U2").v_low
    for lo, passed in ((v - 3.3 - 1e-4, True), (v - 3.3 + 1e-4, False)):
        u2.tol_min, u2.tol_max, u2.tol_pct, u2.tol_dv = lo, 0.1, False, True
        ld = by_name(solve_tree(board(), s).loads, "U2")
        assert ld.passed is passed
        assert ld.limit_high == pytest.approx(3.4)
    loaded = PISetup.from_dict(s.to_dict()).loads[0]
    assert (loaded.tol_pct, loaded.tol_dv) == (False, True)

    s = setup("smps", eff=0.9)
    u1 = s.vrms[1]
    u1.vin_dv, u1.vin_min, u1.vin_max = True, -1.0, 0.5                # of J1's 12 V
    assert vin_range(s, u1) == pytest.approx((11.0, 12.5))
    u1.acc_pct, u1.acc_dv, u1.acc_min, u1.acc_max = False, True, -0.02, 0.05
    assert u1.accuracy_v() == pytest.approx(0.05)
    assert u1.accuracy_label() == "-0.02 / +0.05 V"
    back = PISetup.from_dict(s.to_dict()).vrms[1]
    assert (back.vin_dv, back.acc_dv) == (True, True)
    del s.vrms[0]
    assert any("in volts from nominal needs a VRM" in e for e in validate(s))
