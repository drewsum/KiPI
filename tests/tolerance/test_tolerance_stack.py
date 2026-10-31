"""The tolerance stack-up (#167): what the setup keeps, the values gathered from results,
and the sums against the load's tolerances."""

import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from kipi.ac.solver.sweep import ACResult, ProbeCurve
from kipi.ac.model import Probe
from kipi.dc.pi.model import PISetup, PinRef, Vrm
from kipi.noise.model import Aggressor
from kipi.noise.waveforms import Spectrum
from kipi.tolerance.evaluate import deviation_limits, pair, stack
from kipi.tolerance.gather import Gatherer, Loaded, probe_for_load, ripple_peaks
from kipi.tolerance.model import Contribution, LoadEntry, ToleranceTable
from tree_boards import board, setup as tree_setup

def approx(x, rel=1e-5):
    return pytest.approx(x, rel=rel)            # values are kept to six figures


F = np.logspace(3, math.log10(300e6), 1500)
W = 2 * np.pi * F


def _setup(**tol):
    s = tree_setup()
    ld = s.loads[0]
    for k, v in tol.items():
        setattr(ld, k, v)
    return s


def _probe(s, name="P1", ref="U2", pins=("1",)):
    p = Probe(name, PinRef(ref, "+3V3", list(pins)), PinRef(ref, "GND", ["2"]),
              step_a=1.0, slew_a_per_us=1.0)
    s.probes.append(p)
    return p


def _ac(name="P1"):
    z = 2e-3 + 1j * W * 1e-9 + 1 / (1j * W * 100e-6)
    return ACResult(rail="U1", rail_nets=["+3V3"], ground="GND", freqs=F,
                    probes=[ProbeCurve(name, z, step_a=1.0, slew_a_per_us=1.0)],
                    field_freqs=F[::100], bandwidth_hz=50e3)


def _noise(agg, probe, amp=0.005):
    c = np.zeros(32, complex)
    c[0] = amp                                   # 2|c| = 10 mV at the switching frequency
    wf = SimpleNamespace(samples_per_period=lambda: 256, fsw_hz=1e6,
                         probe_ripple={probe: Spectrum(f"V({probe})", "V", 0.0, c)})
    return SimpleNamespace(aggressor=agg, waveforms=wf)


# ------------------------------------------------------------------ the setup keeps it
def test_the_table_saves_with_the_setup():
    s = _setup()
    s.tolerance.accept("U2", "dc", Contribution(0.01, 0.0, None, None, "DC solve", "now", "x"))
    s.tolerance.bw_limit = True
    back = PISetup.from_dict(json.loads(json.dumps(s.to_dict())))
    assert back.tolerance == s.tolerance
    assert PISetup.from_dict({}).tolerance == ToleranceTable()


def test_entries_of_loads_gone_and_unknown_keys_are_left_out():
    d = _setup().to_dict()
    d["tolerance"] = {"entries": {"U2": {"dc": {"below_v": 0.01, "spare": 1}},
                                  "GONE": {"dc": {"below_v": 0.02}}}}
    t = PISetup.from_dict(d).tolerance
    assert list(t.entries) == ["U2"] and t.entries["U2"].dc.below_v == 0.01


# ------------------------------------------------------------------ limits and sums
@pytest.mark.parametrize("lo, hi, pct, dv, want", [
    (-5, 5, True, False, (0.165, 0.165)),            # % of 3.3 V
    (-0.1, 0.05, False, True, (0.1, 0.05)),          # volts from nominal
    (3.2, 3.4, False, False, (0.1, 0.1)),            # absolute volts
    (None, 5, True, False, (None, 0.165)),
])
def test_tolerances_become_how_far_below_and_above(lo, hi, pct, dv, want):
    got = deviation_limits(3.3, lo, hi, pct, dv)
    assert [g is None for g in got] == [w is None for w in want]
    assert [g for g in got if g is not None] == approx([w for w in want if w is not None])


def test_the_sides_add_against_each_tolerance():
    s = _setup(tol_min=-3, tol_max=3, dc_tol_min=-2, dc_tol_max=2, ac_tol_min=-1, ac_tol_max=1)
    vrm = s.vrms[1]
    vrm.acc_min, vrm.acc_max = -1.0, 1.0                 # ±33 mV
    e = LoadEntry(dc=Contribution(0.020, 0.0), ripple=Contribution(0.005, 0.006),
                  transient=Contribution(0.015, 0.010, 0.012, 0.008))
    st = stack(s.loads[0], e, vrm)
    dc, ac, total = st.checks["dc"], st.checks["ac"], st.checks["total"]
    assert (dc.below_v, dc.above_v) == approx((0.053, 0.033))
    assert dc.passed is True                              # 53 mV of 66
    assert (ac.below_v, ac.above_v) == approx((0.020, 0.016)) and ac.passed is True
    assert (total.below_v, total.above_v) == approx((0.073, 0.049))
    assert total.passed is True and st.verdict is True
    # Through the 20 MHz limit the transient's smaller values are taken.
    assert stack(s.loads[0], e, vrm, bw=True).checks["ac"].below_v == approx(0.017)
    # Tighter AC: FAIL.
    s.loads[0].ac_tol_min = -0.5
    assert stack(s.loads[0], e, vrm).verdict is False


def test_a_missing_part_can_fail_but_not_pass():
    s = _setup(tol_min=-1, tol_max=1)                     # ±33 mV
    vrm = s.vrms[1]
    ok = stack(s.loads[0], LoadEntry(dc=Contribution(0.01, 0.0)), vrm)
    assert ok.checks["total"].passed is None and ok.verdict is None
    bad = stack(s.loads[0], LoadEntry(dc=Contribution(0.05, 0.0)), vrm)
    assert bad.checks["total"].passed is False and bad.verdict is False
    # No switcher on the rail: ripple counts as nothing; the transient is still needed.
    na = stack(s.loads[0], LoadEntry(dc=Contribution(0.01, 0.0),
                                     transient=Contribution(0.01, 0.01)), vrm,
               not_applicable=("ripple",))
    assert na.verdict is True


def test_values_show_in_mv_percent_or_volts():
    assert pair((0.0123, 0.004), 3.3, "mV") == "−12.30 / +4.00"
    assert pair((0.033, 0.0), 3.3, "%") == "−1.000 / 0"
    assert pair((0.1, 0.05), 3.3, "V") == "3.2000 / 3.3500"


# ------------------------------------------------------------------ gathering
def test_dc_drop_is_the_mean_of_the_pins():
    from kipi.dc.pi.tree import solve_tree
    s = _setup()
    tree = solve_tree(board(), s)
    rep = tree.loads[0]
    fresh = Gatherer().fresh(s, s.loads[0], Loaded(dc=(tree, "DC solve", "t")))
    c = fresh.values["dc"]
    assert c.below_v == approx(rep.nominal - rep.v_effective) and c.above_v == 0
    assert c.source == "DC solve"


def test_a_load_finds_its_probe():
    s = _setup()
    assert probe_for_load(s, s.loads[0]) is None
    loose = _probe(s, "loose", pins=())                   # same part and net, all pins
    assert probe_for_load(s, s.loads[0]) is loose
    exact = _probe(s, "exact")
    assert probe_for_load(s, s.loads[0]) is exact
    s.probes = [Probe("other", PinRef("U2", "VIN", ["1"]), PinRef("U2", "GND", ["2"]))]
    assert probe_for_load(s, s.loads[0]) is None


def test_hints_say_what_to_run():
    s = _setup()
    g = Gatherer()
    fresh = g.fresh(s, s.loads[0], Loaded())
    assert fresh.short["dc"] == "Run DC" and fresh.short["transient"] == "Add AC probe"
    assert fresh.not_applicable == {"ripple": fresh.not_applicable["ripple"]}  # LDO rail
    _probe(s)
    s.aggressors.append(Aggressor("BUCK", vrm="U1"))
    fresh = g.fresh(s, s.loads[0], Loaded())
    assert fresh.short["transient"] == "Run AC" and fresh.short["ripple"] == "Add ripple probe"
    s.aggressors[0].ripple_probes = ["P1"]
    assert g.fresh(s, s.loads[0], Loaded()).short["ripple"] == "Run Noise"


def test_the_transient_comes_from_the_rails_ac_result():
    from kipi.ac.transient import bandwidth_limited, transients
    s = _setup()
    _probe(s)
    r = _ac()
    fresh = Gatherer().fresh(s, s.loads[0], Loaded(ac={"U1": (r, "AC solve", "t")}))
    c = fresh.values["transient"]
    tr = transients(r)["P1"]
    assert (c.below_v, c.above_v) == approx((tr.undershoot_v, tr.overshoot_v))
    assert c.below_bw_v == approx(bandwidth_limited(tr).undershoot_v)
    assert c.below_v > 0 and "P1" in c.detail


def test_ripple_from_several_switchers_adds_as_rss():
    s = _setup()
    _probe(s)
    s.aggressors = [Aggressor("A", vrm="U1", ripple_probes=["P1"]),
                    Aggressor("B", vrm="U1", ripple_probes=["P1"])]
    a, b = _noise("A", "P1"), _noise("B", "P1", amp=0.0025)
    pa, pb = ripple_peaks(a)["P1"], ripple_peaks(b)["P1"]
    assert pa[0] == approx(0.01, 1e-3) and pa[1] == approx(0.01, 1e-3)
    g = Gatherer()
    one = g.fresh(s, s.loads[0], Loaded(noise={"A": (a, "a.kipi-noise-results", "t1")}))
    assert one.values["ripple"].below_v == approx(pa[0])
    assert one.short["ripple"] == "Run Noise"             # B not solved yet
    both = g.fresh(s, s.loads[0], Loaded(noise={"A": (a, "x", "t1"), "B": (b, "y", "t2")}))
    c = both.values["ripple"]
    assert c.below_v == approx(math.hypot(pa[0], pb[0]))
    assert c.above_v == approx(math.hypot(pa[1], pb[1]))
    assert "root sum of squares" in c.detail and "ripple" not in both.hints


def test_a_negative_rail_turns_the_transient_over():
    s = _setup()
    _probe(s)
    s.vrms[1].voltage = -3.3
    r = _ac()
    c = Gatherer().fresh(s, s.loads[0], Loaded(ac={"U1": (r, "", "")})).values["transient"]
    from kipi.ac.transient import transients
    tr = transients(r)["P1"]
    assert (c.below_v, c.above_v) == approx((tr.overshoot_v, tr.undershoot_v))


def test_the_report_writes_one_loads_stack(tmp_path):
    from kipi.tolerance.report import report_name, write_load_report
    s = _setup(tol_min=-3, tol_max=3)
    e = LoadEntry(dc=Contribution(0.02, 0.0, None, None, "DC solve", "2026-10-31 10:00"))
    st = stack(s.loads[0], e, s.vrms[1])
    path = tmp_path / report_name("tree", "U2 / core", fmt="html")
    assert "U2_core" in path.name
    out = write_load_report(path, "tree", s.loads[0], st, e, False,
                            {"transient": "Add an AC probe on U2"})
    text = out.read_text(encoding="utf-8")
    assert "Tolerance stack-up: U2" in text
    assert "DC solve" in text and "Add an AC probe on U2" in text and "20.000" in text


def test_vrm_with_accuracy_needs_no_result():
    s = _setup(dc_tol_min=-2, dc_tol_max=2)
    v: Vrm = s.vrms[1]
    v.acc_min, v.acc_max, v.acc_pct = -0.05, 0.05, False
    v.acc_dv = True
    st = stack(s.loads[0], None, v)
    assert st.parts["accuracy"] == approx((0.05, 0.05)) and st.accuracy_set
    assert st.parts["dc"] is None and st.checks["dc"].passed is None


def test_kept_values_ride_along_in_a_results_file(tmp_path):
    from kipi.core import results_io
    s = _setup()
    s.tolerance.accept("U2", "ripple", Contribution(0.004, 0.005, 0.003, 0.004, "n", "t", "d"))
    path = results_io.write(tmp_path / "r.kipires", {}, {"setup": s})
    assert results_io.read(path)["data"]["setup"].tolerance == s.tolerance
